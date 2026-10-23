"""Start the F1 Live Track Positions app and open it in your browser.

    python run_f1_tracker.py                  # live if a session is on, else the latest race
    python run_f1_tracker.py --session 11388  # replay a specific OpenF1 session_key
    python run_f1_tracker.py --live           # go straight to live mode

All you need is Python 3.12 or newer. The first time it runs, the script
creates a private environment in a ``.venv`` folder next to it and installs
its dependencies there (one time, about a minute, needs internet). After that
it starts straight away. In VS Code: open this file and press Run (▷).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

MIN_PYTHON = (3, 12)
PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / ".venv"
REQUIREMENTS = PROJECT_DIR / "requirements.txt"
REQUIRED_MODULES = ("fastapi", "uvicorn", "websockets", "httpx", "numpy", "orjson", "paho", "certifi")
# Longest .venv path that still fits Windows' 260-character limit for package files.
MAX_WINDOWS_VENV_PATH = 140


# --------------------------------------------------------------------------- #
# First-run setup: a private virtual environment with the dependencies
# --------------------------------------------------------------------------- #
def _check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        sys.exit(
            f"F1 Live Track Positions needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer "
            f"(this is Python {platform.python_version()}).\n"
            "Download it from https://www.python.org/downloads/"
        )


def _missing_modules() -> list[str]:
    return [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]


def _venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _requirements_fingerprint() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def _windows_long_paths_enabled() -> bool:
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem")
        return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def _check_path_length() -> None:
    """Windows limits paths to 260 characters unless long paths are switched on.

    Package files sit up to ~105 characters below .venv, so a very deep project
    folder makes the install fail with a confusing error. Say so up front.
    """
    if os.name != "nt" or len(str(VENV_DIR)) <= MAX_WINDOWS_VENV_PATH or _windows_long_paths_enabled():
        return
    sys.exit(
        f"\nThis folder's location is too long for Windows ({len(str(PROJECT_DIR))} characters):\n"
        f"  {PROJECT_DIR}\n\n"
        "Move the F1-Live-Track-Positions folder somewhere shorter, for example\n"
        "C:\\F1-Live-Track-Positions or your Downloads folder, and run it again."
    )


def _setup_venv(force_install: bool) -> Path:
    """Create .venv and install requirements.txt into it (only when needed)."""
    python = _venv_python()
    stamp = VENV_DIR / ".installed-requirements"
    if not python.exists():
        _check_path_length()
        print("\n  First run: setting up a private Python environment in .venv")
        print("  (one time only, takes about a minute, needs internet)\n", flush=True)
        try:
            subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
        except (subprocess.CalledProcessError, OSError):
            shutil.rmtree(VENV_DIR, ignore_errors=True)  # don't leave a half-made environment behind
            raise
    fingerprint = _requirements_fingerprint()
    if force_install or not stamp.exists() or stamp.read_text().strip() != fingerprint:
        pip = [str(python), "-m", "pip", "--disable-pip-version-check", "--no-input"]
        subprocess.run([*pip, "install", "--quiet", "--upgrade", "pip"], check=True)
        print("  Installing dependencies ...", flush=True)
        subprocess.run([*pip, "install", "-r", str(REQUIREMENTS)], check=True)
        stamp.write_text(fingerprint)
        print("  Done. Starting the tracker ...\n", flush=True)
    return python


def _ensure_environment() -> None:
    """Make sure the dependencies are importable; otherwise run inside .venv."""
    _check_python_version()
    if not _missing_modules():
        return
    if os.environ.get("F1TRACKER_BOOTSTRAPPED"):
        sys.exit(
            f"Still missing packages after setup: {', '.join(_missing_modules())}.\n"
            f"Delete the folder {VENV_DIR} and run again to reinstall."
        )
    running_in_venv = Path(sys.executable).resolve() == _venv_python().resolve()
    try:
        python = _setup_venv(force_install=running_in_venv)
    except (subprocess.CalledProcessError, OSError) as exc:
        hint = "\nOn Debian/Ubuntu you may first need: sudo apt install python3-venv" if os.name != "nt" else ""
        sys.exit(
            f"\nCouldn't set up the Python environment ({exc}).\n"
            f"Check your internet connection, delete the folder {VENV_DIR}, and try again.{hint}"
        )
    env = {**os.environ, "F1TRACKER_BOOTSTRAPPED": "1"}
    app = subprocess.Popen([str(python), str(PROJECT_DIR / Path(__file__).name), *sys.argv[1:]], env=env)
    while True:
        try:
            sys.exit(app.wait())
        except KeyboardInterrupt:
            continue  # Ctrl+C reaches the app too; let it shut down cleanly


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real-time and replay F1 car positions on a track map.")
    parser.add_argument("--session", type=int, help="OpenF1 session_key to replay on startup")
    parser.add_argument("--live", action="store_true", help="start in live mode")
    parser.add_argument("--port", type=int, help="port for the web UI (default 8050)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    parser.add_argument(
        "--keep-running", action="store_true", help="don't quit when the browser tab is closed"
    )
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    return parser.parse_args()


def _free_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port found between {preferred} and {preferred + 19}")


def _open_browser_when_ready(server, url: str) -> None:
    for _ in range(200):
        if server.started:
            webbrowser.open(url)
            return
        time.sleep(0.05)


def main() -> None:
    # Windows consoles default to a legacy code page; don't let log text crash the app.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    _ensure_environment()
    sys.path.insert(0, str(PROJECT_DIR))

    import uvicorn

    from f1tracker import __version__
    from f1tracker.config import load_settings
    from f1tracker.server.app import create_app

    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = load_settings(port=args.port)
    port = _free_port(settings.host, settings.port)
    url = f"http://{settings.host}:{port}"

    server: uvicorn.Server | None = None

    def quit_app() -> None:
        if server is not None:
            server.should_exit = True  # uvicorn shuts down cleanly and server.run() returns

    app = create_app(
        settings,
        startup_session=args.session,
        startup_live=args.live,
        on_last_tab_closed=None if args.keep_running else quit_app,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.host, port=port, log_level="warning", ws_ping_interval=20)
    )

    live_note = (
        "live feed enabled (OpenF1 account configured)"
        if settings.has_openf1_credentials
        else "replays only - add OpenF1 credentials to .env for live data"
    )
    data_note = (
        "session data is kept in .cache/"
        if settings.keep_data
        else "session data stays in memory; nothing is saved to disk"
    )
    stop_note = "Ctrl+C" if args.keep_running else "close the browser tab (or Ctrl+C)"
    print(
        f"\n  F1 Live Track Positions v{__version__}\n"
        f"  ---------------------------------\n"
        f"  UI:    {url}\n"
        f"  Mode:  {live_note}\n"
        f"  Data:  {data_note}\n"
        f"  Quit:  {stop_note}\n",
        flush=True,
    )
    if not args.no_browser:
        threading.Thread(target=_open_browser_when_ready, args=(server, url), daemon=True).start()
    server.run()
    print("\n  F1 Live Track Positions stopped.", flush=True)


if __name__ == "__main__":
    main()
