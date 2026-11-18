"""Filesystem watcher with debounce, event-storm protection, and polling fallback."""

from __future__ import annotations

import fnmatch
import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

logger = logging.getLogger("openagent_bridge.watcher")

DEFAULT_IGNORE_PATTERNS = [
    "*.git*",
    "*/.git/*",
    "*.openagent*",
    "*/.openagent/*",
    "*node_modules*",
    "*/node_modules/*",
    "*dist*",
    "*build*",
    "*coverage*",
    "*.pytest_cache*",
    "*__pycache__*",
    "*.swp",
    "*.tmp",
    "*~",
    "*.log",
]


class ProjectWatcher:
    """Monitors repository changes with debounce and storm protection."""

    def __init__(
        self,
        repo_path: Path | str,
        on_change: Callable[[list[str]], None],
        debounce_ms: int = 1000,
        ignore_patterns: list[str] | None = None,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.on_change = on_change
        self.debounce_seconds = debounce_ms / 1000.0
        self.ignore_patterns = ignore_patterns or DEFAULT_IGNORE_PATTERNS

        self._pending_files: set[str] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._running = False
        self._observer = None

    def is_ignored(self, file_path: str) -> bool:
        """Check if path matches any ignore pattern."""
        norm = file_path.replace("\\", "/")
        name = Path(norm).name

        for pat in self.ignore_patterns:
            pat_norm = pat.replace("\\", "/")
            if fnmatch.fnmatch(norm, pat_norm) or fnmatch.fnmatch(name, pat_norm):
                return True
        return False

    def notify_change(self, file_path: str) -> None:
        """Receive a raw file change notification and debounce it."""
        if self.is_ignored(file_path):
            return

        with self._lock:
            self._pending_files.add(file_path)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        """Flush debounced file change events."""
        with self._lock:
            files = list(self._pending_files)
            self._pending_files.clear()
            self._timer = None

        if files and self._running:
            logger.info("Debounced project change triggered with %d files", len(files))
            try:
                self.on_change(files)
            except Exception as exc:
                logger.error("Error executing on_change handler: %s", exc, exc_info=True)

    def start(self) -> None:
        """Start monitoring using watchdog (native or polling)."""
        self._running = True

        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
            from watchdog.observers.polling import PollingObserver

            watcher_self = self

            class _Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    if not event.is_directory:
                        watcher_self.notify_change(event.src_path)

            handler = _Handler()

            # Attempt native observer first
            try:
                self._observer = Observer()
                self._observer.schedule(handler, str(self.repo_path), recursive=True)
                self._observer.start()
                logger.info("Native filesystem watcher started on %s", self.repo_path)
                return
            except Exception as native_err:
                logger.warning("Native observer failed (%s); falling back to PollingObserver", native_err)

            # Fallback to PollingObserver
            self._observer = PollingObserver(timeout=1.0)
            self._observer.schedule(handler, str(self.repo_path), recursive=True)
            self._observer.start()
            logger.info("Polling filesystem watcher started on %s", self.repo_path)

        except ImportError:
            logger.warning("Watchdog not installed; running in manual/polling fallback mode.")
            self._start_custom_polling()

    def _start_custom_polling(self) -> None:
        """Lightweight manual fallback if watchdog is completely missing."""
        def _poll():
            mtimes: dict[str, float] = {}
            while self._running:
                try:
                    for p in self.repo_path.rglob("*"):
                        if p.is_file() and not self.is_ignored(str(p)):
                            try:
                                mtime = p.stat().st_mtime
                                old = mtimes.get(str(p))
                                if old is not None and mtime > old:
                                    self.notify_change(str(p))
                                mtimes[str(p)] = mtime
                            except OSError:
                                pass
                except Exception:
                    pass
                time.sleep(1.0)

        t = threading.Thread(target=_poll, daemon=True)
        t.start()

    def stop(self) -> None:
        """Stop file system observer."""
        self._running = False
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None

        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                pass
            self._observer = None
