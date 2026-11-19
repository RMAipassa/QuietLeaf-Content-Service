"""Bounded, process-local login admission; proxy identity comes from ASGI, not headers."""
import math
import time
from collections import deque
from threading import Lock


class LoginLimits:
    def __init__(self, *, clock=time.monotonic, window=300, per_client=5, globally=100,
                 client_inflight=2, global_inflight=8, max_clients=4096):
        self.clock = clock
        self.window = window
        self.per_client = per_client
        self.globally = globally
        self.client_inflight = client_inflight
        self.global_inflight = global_inflight
        self.max_clients = max_clients
        self.attempts = {}
        self.global_attempts = deque()
        self.active = {}
        self.inflight = 0
        self.lock = Lock()

    def reserve(self, address: str) -> int:
        """Atomically consume a slot/attempt before any await; return retry seconds or 0."""
        now = self.clock()
        with self.lock:
            for key, attempts in list(self.attempts.items()):
                while attempts and attempts[0] <= now - self.window:
                    attempts.popleft()
                if not attempts and not self.active.get(key):
                    del self.attempts[key]
            while self.global_attempts and self.global_attempts[0] <= now - self.window:
                self.global_attempts.popleft()
            attempts = self.attempts.get(address, ())
            if len(attempts) >= self.per_client:
                return max(1, math.ceil(attempts[0] + self.window - now))
            if len(self.global_attempts) >= self.globally:
                return max(1, math.ceil(self.global_attempts[0] + self.window - now))
            if (self.inflight >= self.global_inflight or
                    self.active.get(address, 0) >= self.client_inflight or
                    address not in self.attempts and len(self.attempts) >= self.max_clients):
                return 1
            self.attempts.setdefault(address, deque()).append(now)
            self.global_attempts.append(now)
            self.active[address] = self.active.get(address, 0) + 1
            self.inflight += 1
            return 0

    def release(self, address: str, *, success=False):
        with self.lock:
            self.inflight -= 1
            remaining = self.active[address] - 1
            if remaining:
                self.active[address] = remaining
            else:
                del self.active[address]
            if success:
                # Owner success clears their history, not the global anti-saturation budget.
                self.attempts.pop(address, None)
