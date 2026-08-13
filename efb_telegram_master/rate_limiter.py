import threading
import time


class TelegramRateLimiter:
    """Thread-safe token bucket that preserves synchronous delivery semantics."""

    def __init__(self, rate: float, burst: int, now=time.monotonic, sleep=time.sleep):
        self.rate = max(0.1, float(rate))
        self.capacity = max(1, int(burst))
        self.tokens = float(self.capacity)
        self.updated_at = now()
        self.now = now
        self.sleep = sleep
        self.lock = threading.Lock()

    def wait(self):
        while True:
            with self.lock:
                current = self.now()
                elapsed = max(0.0, current - self.updated_at)
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.updated_at = current
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                delay = (1 - self.tokens) / self.rate
            self.sleep(delay)
