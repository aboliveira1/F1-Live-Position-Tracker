"""Tiny on-disk cache of gzip-compressed JSON documents."""

from __future__ import annotations

import gzip
import hashlib
import logging
import shutil
import time
from pathlib import Path
from typing import Any

import orjson

log = logging.getLogger(__name__)

# Bump when the on-disk layout changes; older caches are then discarded once.
LAYOUT_VERSION = "2"


class JsonCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._discard_old_layout()

    def _discard_old_layout(self) -> None:
        marker = self.root / "LAYOUT"
        try:
            if marker.read_text().strip() == LAYOUT_VERSION:
                return
        except FileNotFoundError:
            pass
        except OSError:
            return
        if self.root.exists():
            log.info("Clearing cache in an old format: %s", self.root)
            shutil.rmtree(self.root, ignore_errors=True)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            marker.write_text(LAYOUT_VERSION)
        except OSError as exc:
            log.warning("Could not prepare cache folder %s (%s)", self.root, exc)

    def clear(self, namespace: str) -> int:
        """Delete everything under ``namespace``; returns the bytes freed."""
        folder = self.root / namespace
        if not folder.exists():
            return 0
        freed = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        shutil.rmtree(folder, ignore_errors=True)
        return freed

    def _path(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha1(key.encode()).hexdigest()[:24]
        return self.root / namespace / f"{digest}.json.gz"

    def get(self, namespace: str, key: str, *, max_age: float | None = None) -> Any | None:
        path = self._path(namespace, key)
        try:
            if max_age is not None and time.time() - path.stat().st_mtime > max_age:
                return None
            with gzip.open(path, "rb") as fh:
                return orjson.loads(fh.read())
        except FileNotFoundError:
            return None
        except (OSError, orjson.JSONDecodeError, EOFError) as exc:
            log.warning("Ignoring unreadable cache file %s (%s)", path, exc)
            return None

    def has(self, namespace: str, key: str) -> bool:
        return self._path(namespace, key).is_file()

    def age(self, namespace: str, key: str) -> float | None:
        """Seconds since the entry was written, or None if there is no entry."""
        try:
            return time.time() - self._path(namespace, key).stat().st_mtime
        except FileNotFoundError:
            return None

    def put(self, namespace: str, key: str, value: Any) -> None:
        path = self._path(namespace, key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with gzip.open(tmp, "wb", compresslevel=5) as fh:
                fh.write(orjson.dumps(value))
            tmp.replace(path)
        except OSError as exc:
            log.warning("Could not write cache file %s (%s)", path, exc)
