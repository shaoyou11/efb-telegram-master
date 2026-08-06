import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class DeliveryTraceStore:
    """Small atomic JSON trace store for diagnosing one message end to end."""

    VERSION = 1
    MAX_MESSAGES = 512
    MAX_EVENTS = 24

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.records: Dict[str, List[dict]] = self._load()

    def _load(self) -> Dict[str, List[dict]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            records = data.get("records", {}) if isinstance(data, dict) else {}
            if isinstance(records, dict):
                return {
                    str(uid): list(events)[-self.MAX_EVENTS:]
                    for uid, events in records.items()
                    if isinstance(events, list)
                }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": self.VERSION, "records": self.records}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent),
                prefix=f".{self.path.name}.", delete=False,
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary = handle.name
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def record(self, uid: str, stage: str, **fields: Any) -> dict:
        event = {
            "stage": str(stage),
            "at": time.time(),
        }
        for key, value in fields.items():
            if value not in (None, ""):
                event[str(key)] = str(value)[:300]
        with self.lock:
            key = str(uid)
            events = self.records.setdefault(key, [])
            events.append(event)
            self.records[key] = events[-self.MAX_EVENTS:]
            if len(self.records) > self.MAX_MESSAGES:
                oldest = sorted(
                    self.records,
                    key=lambda item: self.records[item][-1].get("at", 0),
                )[:-self.MAX_MESSAGES]
                for old_uid in oldest:
                    self.records.pop(old_uid, None)
            self._save()
        return dict(event)

    def get(self, uid: str) -> List[dict]:
        with self.lock:
            return [dict(item) for item in self.records.get(str(uid), [])]

    def latest(self) -> Optional[dict]:
        with self.lock:
            candidates = [
                (events[-1].get("at", 0), uid, events[-1])
                for uid, events in self.records.items()
                if events
            ]
            if not candidates:
                return None
            _, uid, event = max(candidates)
            result = dict(event)
            result["uid"] = uid
            return result
