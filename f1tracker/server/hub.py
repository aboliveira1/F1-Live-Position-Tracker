"""Fan-out of JSON messages to every connected browser."""

from __future__ import annotations

import asyncio
import logging

import orjson
from fastapi import WebSocket

log = logging.getLogger(__name__)

_JSON_OPTIONS = orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS


def encode(message: dict) -> str:
    return orjson.dumps(message, option=_JSON_OPTIONS).decode()


class ClientHub:
    SEND_TIMEOUT_S = 2.0

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._closing: set[asyncio.Task] = set()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def send(self, websocket: WebSocket, message: dict) -> None:
        await websocket.send_text(encode(message))

    async def broadcast(self, message: dict) -> None:
        if not self._clients:
            return
        data = encode(message)
        for websocket in list(self._clients):
            try:
                await asyncio.wait_for(websocket.send_text(data), self.SEND_TIMEOUT_S)
            except Exception:  # closed or stalled socket: drop it
                self._clients.discard(websocket)
                # Close it too, so a stalled browser notices and reconnects.
                task = asyncio.create_task(self._close_quietly(websocket))
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)

    @staticmethod
    async def _close_quietly(websocket: WebSocket) -> None:
        try:
            await websocket.close()
        except Exception:
            pass
