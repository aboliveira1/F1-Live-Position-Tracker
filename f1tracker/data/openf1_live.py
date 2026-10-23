"""Real-time OpenF1 feed over MQTT (needs an OpenF1 subscription).

Messages have the same schema as the REST rows, so they go through the same
parsers as replays. paho-mqtt runs its network loop on a background thread;
messages are handed to asyncio through a queue. (paho is used directly
because asyncio MQTT wrappers need a selector event loop, which Windows
doesn't use by default.)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import certifi
import orjson
import paho.mqtt.client as mqtt

from ..config import Settings

log = logging.getLogger(__name__)

LIVE_TOPICS = (
    "v1/location",
    "v1/position",
    "v1/intervals",
    "v1/laps",
    "v1/stints",
    "v1/pit",
    "v1/race_control",
    "v1/weather",
    "v1/drivers",
)
STATUS_TOPIC = "$status"  # internal pseudo-topic for connection status updates
# How often to check the token. The client hands back its cached token until
# five minutes before expiry, so checking often is cheap and keeps the MQTT
# password fresh for reconnects.
TOKEN_REFRESH_S = 4 * 60
MAX_REFUSALS = 3  # give up after this many refused connection attempts in a row


class OpenF1LiveFeed:
    def __init__(self, settings: Settings, token_provider: Callable[[], Awaitable[str | None]]) -> None:
        self._settings = settings
        self._token_provider = token_provider
        self._queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: mqtt.Client | None = None
        self._refresh_task: asyncio.Task | None = None
        self._connected = False
        self._refusals = 0  # consecutive refused connection attempts

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        token = await self._token_provider()
        if not token:
            raise RuntimeError("An OpenF1 account is required for the live feed")
        self._loop = asyncio.get_running_loop()

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"f1tracker-{uuid.uuid4().hex[:12]}",
        )
        client.username_pw_set(self._settings.openf1_username, token)
        # certifi's CA bundle: python.org builds on macOS ship without system certificates.
        client.tls_set(ca_certs=certifi.where())
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_async(self._settings.openf1_mqtt_host, self._settings.openf1_mqtt_port, keepalive=30)
        client.loop_start()
        self._client = client
        self._refresh_task = asyncio.create_task(self._refresh_token_forever())
        self._emit_status("connecting", "Connecting to the OpenF1 live feed…")

    async def stop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
        if self._client:
            client, self._client = self._client, None
            await asyncio.to_thread(self._shutdown_client, client)

    @staticmethod
    def _shutdown_client(client: mqtt.Client) -> None:
        client.disconnect()
        client.loop_stop()

    async def messages(self) -> AsyncIterator[tuple[str, dict]]:
        """Yields ``(topic, row)``; ``topic`` is ``"$status"`` for connection updates."""
        while True:
            yield await self._queue.get()

    # ------------------------------------------------------------------ #
    async def _refresh_token_forever(self) -> None:
        """Tokens last an hour; keep a valid one in place for reconnects."""
        while True:
            await asyncio.sleep(TOKEN_REFRESH_S)
            await self._refresh_token()

    async def _refresh_token(self) -> None:
        try:
            token = await self._token_provider()
            if self._client and token:
                self._client.username_pw_set(self._settings.openf1_username, token)
        except Exception as exc:  # keep the feed alive; the next attempt may succeed
            log.warning("Could not refresh OpenF1 token: %s", exc)

    def _emit(self, topic: str, row: dict) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (topic, row))

    def _emit_status(self, state: str, message: str) -> None:
        self._emit(STATUS_TOPIC, {"state": state, "message": message})

    # paho callbacks — these run on paho's network thread.
    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            self._refusals += 1
            log.error("OpenF1 MQTT connection refused (%s), attempt %d", reason_code, self._refusals)
            if self._refusals >= MAX_REFUSALS:
                client.disconnect()  # stop paho's reconnect loop
                self._emit_status(
                    "error",
                    f"OpenF1 refused the live connection ({reason_code}). "
                    "Check that your OpenF1 subscription is active.",
                )
            elif self._loop is not None:
                # The token may have expired; fetch a new one before paho retries.
                asyncio.run_coroutine_threadsafe(self._refresh_token(), self._loop)
            return
        self._refusals = 0
        self._connected = True
        client.subscribe([(topic, 0) for topic in LIVE_TOPICS])
        log.info("Connected to OpenF1 live feed")
        self._emit_status("connected", "Connected to the OpenF1 live feed")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        if self._client is not None and self._connected:
            self._connected = False
            log.warning("OpenF1 live feed disconnected (%s); reconnecting", reason_code)
            self._emit_status("reconnecting", "Live feed disconnected — reconnecting…")

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        try:
            row = orjson.loads(msg.payload)
        except orjson.JSONDecodeError:
            return
        if isinstance(row, dict):
            self._emit(msg.topic, row)
