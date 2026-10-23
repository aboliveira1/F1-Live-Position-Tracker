"""Build a clean copy of the project to share, as a zip and as a plain folder.

    python tools/make_zip.py
        -> dist/F1-Live-Track-Positions.zip   (send to friends)
        -> dist/F1-Live-Track-Positions/      (drag its contents into a GitHub repo)

Both leave out everything that is personal or gets recreated on first run:
your .env (OpenF1 password), .venv, .cache, __pycache__, and so on. In the zip,
launcher scripts keep their executable bit so they run on macOS/Linux.
"""

from __future__ import annotations

import fnmatch
import shutil
import zipfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
FOLDER_NAME = "F1-Live-Track-Positions"
DIST_DIR = PROJECT_DIR / "dist"
ZIP_PATH = DIST_DIR / f"{FOLDER_NAME}.zip"
FOLDER_PATH = DIST_DIR / FOLDER_NAME

EXCLUDED_DIRS = {".venv", "venv", ".cache", "__pycache__", ".pytest_cache", "dist", ".git", ".idea", ".mypy_cache"}
EXCLUDED_FILES = [".env", "*.pyc", "*.pyo", ".DS_Store", "Thumbs.db", "*.tmp"]
EXECUTABLE = ["*.sh", "*.command", "run_f1_tracker.py"]


def project_files() -> list[Path]:
    files = []
    for path in sorted(PROJECT_DIR.rglob("*")):
        relative = path.relative_to(PROJECT_DIR)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.is_file() and not any(fnmatch.fnmatch(path.name, p) for p in EXCLUDED_FILES):
            files.append(path)
    return files


def write_zip(files: list[Path]) -> None:
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(PROJECT_DIR).as_posix()
            info = zipfile.ZipInfo.from_file(path, f"{FOLDER_NAME}/{relative}")
            executable = any(fnmatch.fnmatch(path.name, p) for p in EXECUTABLE)
            info.external_attr = (0o755 if executable else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    print(f"Wrote {ZIP_PATH} ({len(files)} files, {ZIP_PATH.stat().st_size / 1e6:.1f} MB)")


def write_folder(files: list[Path]) -> None:
    if FOLDER_PATH.exists():
        shutil.rmtree(FOLDER_PATH)  # rebuilt from scratch so nothing stale lingers
    for path in files:
        target = FOLDER_PATH / path.relative_to(PROJECT_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    print(f"Wrote {FOLDER_PATH} ({len(files)} files)")


def main() -> None:
    DIST_DIR.mkdir(exist_ok=True)
    files = project_files()
    write_zip(files)
    write_folder(files)


if __name__ == "__main__":
    main()
