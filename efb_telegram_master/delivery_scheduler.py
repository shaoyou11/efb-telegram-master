import heapq
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional, Tuple


@dataclass
class _Job:
    future: Future
    callback: Callable[[], object]


class DeliveryScheduler:
    """FIFO per source chat, with contact queues ahead of group queues."""

    def __init__(self, worker_count: int = 4, autostart: bool = True):
        self.worker_count = max(1, int(worker_count))
        self.condition = threading.Condition(threading.RLock())
        self.queues: Dict[str, Deque[_Job]] = {}
        self.priorities: Dict[str, int] = {}
        self.ready: list[Tuple[int, int, str]] = []
        self.active = set()
        self.sequence = 0
        self.stopping = False
        self.threads = []
        if autostart:
            self.start()

    def start(self) -> None:
        with self.condition:
            if self.threads:
                return
            self.stopping = False
            for index in range(self.worker_count):
                thread = threading.Thread(
                    target=self._worker,
                    name=f"efb-delivery-{index + 1}",
                    daemon=True,
                )
                thread.start()
                self.threads.append(thread)

    def submit(self, chat_key: str, is_contact: bool, callback: Callable[[], object]) -> Future:
        future = Future()
        key = str(chat_key or "unknown")
        with self.condition:
            if self.stopping:
                future.set_exception(RuntimeError("delivery scheduler is closed"))
                return future
            queue = self.queues.setdefault(key, deque())
            queue.append(_Job(future, callback))
            self.priorities[key] = 0 if is_contact else 10
            if key not in self.active:
                self.active.add(key)
                heapq.heappush(self.ready, (self.priorities[key], self.sequence, key))
                self.sequence += 1
            self.condition.notify()
        return future

    def _worker(self) -> None:
        while True:
            with self.condition:
                while not self.ready and not self.stopping:
                    self.condition.wait()
                if self.stopping and not self.ready:
                    return
                _, _, key = heapq.heappop(self.ready)
                queue = self.queues.get(key)
                if not queue:
                    self.active.discard(key)
                    continue
                job = queue.popleft()
            try:
                result = job.callback()
            except BaseException as error:
                job.future.set_exception(error)
            else:
                job.future.set_result(result)
            with self.condition:
                queue = self.queues.get(key)
                if queue:
                    heapq.heappush(self.ready, (self.priorities.get(key, 10), self.sequence, key))
                    self.sequence += 1
                else:
                    self.queues.pop(key, None)
                    self.priorities.pop(key, None)
                    self.active.discard(key)

    def snapshot(self) -> dict:
        with self.condition:
            return {
                "active_chats": len(self.active),
                "queued_messages": sum(len(queue) for queue in self.queues.values()),
                "contact_chats": sum(1 for key in self.active if self.priorities.get(key) == 0),
                "group_chats": sum(1 for key in self.active if self.priorities.get(key) == 10),
            }

    def close(self) -> None:
        with self.condition:
            self.stopping = True
            for queue in self.queues.values():
                for job in queue:
                    job.future.cancel()
            self.queues.clear()
            self.ready.clear()
            self.condition.notify_all()
        for thread in self.threads:
            thread.join(timeout=2)
        self.threads = []


class TelegramRateLimiter:
    def __init__(self, rate_per_second: float = 4.0, burst: int = 4):
        self.rate = max(0.1, float(rate_per_second))
        self.capacity = max(1, int(burst))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
                wait = (1 - self.tokens) / self.rate
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            time.sleep(max(0.001, wait))
