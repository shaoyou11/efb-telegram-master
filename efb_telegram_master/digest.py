import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional


class DigestStore:
    VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.rules = self._load()

    def _load(self) -> Dict[str, bool]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            rules = data.get("rules", {}) if isinstance(data, dict) else {}
            return {str(key): bool(value) for key, value in rules.items()}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent),
                prefix=f".{self.path.name}.", delete=False,
            ) as handle:
                json.dump({"version": self.VERSION, "rules": self.rules}, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary = handle.name
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def enabled(self, chat_key: str) -> bool:
        with self.lock:
            return bool(self.rules.get(str(chat_key), False))

    def set_enabled(self, chat_key: str, enabled: bool) -> None:
        with self.lock:
            key = str(chat_key)
            if enabled:
                self.rules[key] = True
            else:
                self.rules.pop(key, None)
            self._save()

    def list_rules(self) -> Dict[str, bool]:
        with self.lock:
            return dict(self.rules)


class DigestManager:
    """Collect explicitly enabled silent chats into hourly Telegram summaries."""

    def __init__(self, store: DigestStore, send: Callable[..., object], interval: int = 3600):
        self.store = store
        self.send = send
        self.interval = max(60, int(interval))
        self.lock = threading.RLock()
        self.entries: Dict[str, list] = {}
        self.targets: Dict[str, tuple] = {}
        self.next_flush = time.time() + self.interval
        self.stopping = False
        self.thread = threading.Thread(target=self._run, name="efb-digest", daemon=True)
        self.thread.start()

    def enabled(self, key: str) -> bool:
        return self.store.enabled(key)

    def add(self, key: str, chat_id, thread_id, msg) -> None:
        if not self.enabled(key):
            return
        text = getattr(msg, "text", None) or getattr(msg, "filename", None) or str(getattr(msg, "type", "消息"))
        with self.lock:
            self.entries.setdefault(key, []).append(str(text)[:120])
            self.entries[key] = self.entries[key][-100:]
            self.targets[key] = (chat_id, thread_id)

    def flush_due(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else float(now)
        with self.lock:
            if now < self.next_flush:
                return 0
            batches = list(self.entries.items())
            self.entries.clear()
            self.next_flush = now + self.interval
        sent = 0
        for key, entries in batches:
            target = self.targets.get(key)
            if not target or not entries:
                continue
            chat_id, thread_id = target
            text = "EFB 静默摘要\n\n" + "\n".join(
                f"{index}. {item}" for index, item in enumerate(entries, 1)
            )
            kwargs = {}
            if thread_id is not None:
                kwargs["message_thread_id"] = thread_id
            try:
                self.send(chat_id, text, **kwargs)
                sent += 1
            except Exception:
                # Keep the original Telegram delivery path independent of digest failures.
                with self.lock:
                    self.entries.setdefault(key, []).extend(entries)
        return sent

    def _run(self) -> None:
        while not self.stopping:
            self.flush_due()
            time.sleep(1)

    def close(self) -> None:
        self.stopping = True
        self.thread.join(timeout=2)
