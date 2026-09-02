"""Bounded, allowlisted history for administrator preference changes."""

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path


LABELS = {
    "wechat-read": "微信自动已读", "digest": "静默投递摘要",
    "quiet-hours": "夜间静默", "policy": "会话接收策略",
}


def validate(key, target, value):
    if key not in LABELS or not isinstance(target, str) or len(target) > 300:
        raise ValueError("不支持记录此项设置")
    if key in {"wechat-read", "digest"}:
        valid = type(value) is bool and not target
    elif key == "policy":
        valid = bool(target) and value in {"normal", "silent", "filtered"}
    else:
        from datetime import time as clock
        valid = (isinstance(value, dict) and set(value) == {"enabled", "start", "end"}
                 and type(value["enabled"]) is bool and not target)
        if valid:
            clock.fromisoformat(value["start"])
            clock.fromisoformat(value["end"])
    if not valid:
        raise ValueError("设置值格式无效")


class SettingsHistory:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def entries(self):
        with self.lock:
            if not self.path.exists():
                return []
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("entries"), list):
                raise ValueError("配置变更记录无法读取，未修改设置")
            for item in data["entries"]:
                validate(item["key"], item["target"], item["before"])
                validate(item["key"], item["target"], item["after"])
            return data["entries"]

    def _save(self, entries):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".settings-history-", delete=False) as handle:
                temporary = handle.name
                json.dump({"version": 1, "entries": entries[-100:]}, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def apply(self, key, target, getter, setter, value, actor=None):
        with self.lock:
            before = getter()
            validate(key, target, before)
            validate(key, target, value)
            if before == value:
                return False
            entries = self.entries()
            entry = {"id": uuid.uuid4().hex[:16], "at": int(time.time()),
                     "key": key, "target": target, "before": before, "after": value,
                     "actor": actor if type(actor) is int else None}
            try:
                setter(value)
                self._save(entries + [entry])
            except Exception:
                setter(before)
                raise
            return True

    def undo(self, identity, adapter, actor=None):
        with self.lock:
            entries = self.entries()
            if not entries or entries[-1]["id"] != identity or entries[-1].get("undone_at"):
                raise ValueError("记录已变化或已撤销，请刷新后重试")
            item = entries[-1]
            getter, setter = adapter(item["key"], item["target"])
            if getter() != item["after"]:
                raise ValueError("当前设置已被其他操作修改，未覆盖现有值")
            try:
                setter(item["before"])
                item["undone_at"] = int(time.time())
                item["undone_by"] = actor if type(actor) is int else None
                self._save(entries)
            except Exception:
                setter(item["after"])
                raise


def apply_setting(channel, key, target, getter, setter, value, actor=None):
    history = getattr(channel, "settings_history", None)
    if history is None:
        setter(value)
        return
    history.apply(key, target, getter, setter, value, actor)
