"""Find audio in the inbox and watch for new arrivals (cloud-sync friendly)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from eclipse.ingest import AUDIO_EXTS
from eclipse.log import get_logger

log = get_logger("watcher")


def scan_inbox(inbox_dir: Path) -> list[Path]:
    """Return all audio files currently in the inbox, sorted oldest-first."""
    files = [
        p
        for p in inbox_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith(".")
    ]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def wait_until_stable(path: Path, settle_seconds: float = 2.0, timeout: float = 600.0) -> bool:
    """Block until a file stops growing (cloud download finished). False on timeout."""
    deadline = time.monotonic() + timeout
    last_size = -1
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last_size and size > 0:
            return True
        last_size = size
        time.sleep(settle_seconds)
    return False


class _Handler(FileSystemEventHandler):
    def __init__(self, on_audio: Callable[[Path], None]) -> None:
        self._on_audio = on_audio

    def _maybe(self, raw_path: str) -> None:
        path = Path(raw_path)
        if path.suffix.lower() in AUDIO_EXTS and not path.name.startswith("."):
            if wait_until_stable(path):
                self._on_audio(path)
            else:
                log.warning("file_never_settled", path=str(path))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        # cloud clients often write to a temp name then rename into place
        dest = getattr(event, "dest_path", "")
        if not event.is_directory and dest:
            self._maybe(str(dest))


def watch(inbox_dir: Path, on_audio: Callable[[Path], None]) -> Iterator[None]:
    """Run a blocking watch loop, calling ``on_audio`` for each settled new file."""
    observer = Observer()
    observer.schedule(_Handler(on_audio), str(inbox_dir), recursive=True)
    observer.start()
    log.info("watching", inbox=str(inbox_dir))
    try:
        while True:
            time.sleep(1)
            yield
    finally:
        observer.stop()
        observer.join()
