from __future__ import annotations

import threading
import time

from .adapters import Source


class HomeFeedWorker:
    def __init__(self, sources: list[Source], interval_seconds: float = 5 * 60,
                 stop_timeout_seconds: float = 2):
        self.sources = [source for source in sources
                        if callable(getattr(source, "refresh_home_feed", None))]
        self.interval_seconds = interval_seconds
        self.stop_timeout_seconds = stop_timeout_seconds
        self.condition = threading.Condition()
        self.thread = None
        self.stop_event = threading.Event()
        self._requested = False
        self._refreshing = False
        self._completed = 0

    @property
    def source_count(self) -> int:
        return len(self.sources)

    def start(self) -> None:
        with self.condition:
            if self.thread is not None and self.thread.is_alive():
                if self.stop_event.is_set():
                    raise RuntimeError("Previous home feed worker is still stopping")
                return
            self.stop_event = threading.Event()
            self._requested = True
            self._refreshing = False
            self.thread = threading.Thread(
                target=self.run, args=(self.stop_event,), daemon=True,
                name="quietleaf-home-feed-worker"
            )
            self.thread.start()

    def stop(self) -> None:
        with self.condition:
            self.stop_event.set()
            self.condition.notify_all()
            thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=self.stop_timeout_seconds)

    def request_refresh(self) -> int:
        with self.condition:
            if (self.thread is None or not self.thread.is_alive()
                    or self.stop_event.is_set()):
                raise RuntimeError("Home feed worker is not running")
            target = self._completed + 1
            if not self._refreshing and not self._requested:
                self._requested = True
                self.condition.notify()
            return target

    def wait_for(self, target: int) -> None:
        with self.condition:
            stop_event = self.stop_event
            while self._completed < target:
                if stop_event.is_set():
                    raise RuntimeError("Home feed worker is stopping")
                self.condition.wait()

    def run(self, stop_event: threading.Event) -> None:
        next_refresh = time.monotonic()
        while not stop_event.is_set():
            with self.condition:
                while not stop_event.is_set() and not self._requested:
                    remaining = next_refresh - time.monotonic()
                    if remaining <= 0:
                        self._requested = True
                        break
                    self.condition.wait(remaining)
                if stop_event.is_set():
                    return
                self._requested = False
                self._refreshing = True
                next_refresh = time.monotonic() + self.interval_seconds

            for source in self.sources:
                if stop_event.is_set():
                    break
                try:
                    source.refresh_home_feed(stop_event)
                except Exception:
                    pass

            with self.condition:
                self._refreshing = False
                self._completed += 1
                self.condition.notify_all()
