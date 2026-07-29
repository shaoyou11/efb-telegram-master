import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


class FailedDeliveryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.records = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    str(token): record
                    for token, record in data.items()
                    if isinstance(record, dict)
                }
        except (OSError, TypeError, ValueError):
            pass
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=".failed-delivery.",
            delete=False,
        ) as handle:
            json.dump(self.records, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, self.path)

    def put(self, token: str, record: Dict[str, Any]) -> None:
        with self.lock:
            self.records[str(token)] = dict(record)
            self._save()

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            self._prune_locked()
            record = self.records.get(str(token))
            return dict(record) if record else None

    def remove(self, token: str) -> None:
        with self.lock:
            if self.records.pop(str(token), None) is not None:
                self._save()

    def _prune_locked(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        expired = [
            token
            for token, record in self.records.items()
            if float(record.get("expires", 0)) <= now
        ]
        if expired:
            for token in expired:
                self.records.pop(token, None)
            self._save()

    def prune(self, now: Optional[float] = None) -> None:
        with self.lock:
            self._prune_locked(now)
