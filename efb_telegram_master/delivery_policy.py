import json
import logging
import os
import tempfile
from datetime import datetime, time
from enum import Enum
from pathlib import Path
from typing import Dict, Optional


class DeliveryPolicy(str, Enum):
    NORMAL = "normal"
    SILENT = "silent"
    FILTERED = "filtered"


class DeliveryPolicyStore:
    VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.logger = logging.getLogger(__name__)
        self._settings = {"quiet_hours": {"enabled": False, "start": "23:00", "end": "07:00"}}
        self._rules = self._load()

    def _load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != self.VERSION or not isinstance(data.get("rules"), dict):
                raise ValueError("unsupported delivery policy format")
            quiet = data.get("settings", {}).get("quiet_hours", {})
            if isinstance(quiet, dict):
                self._settings["quiet_hours"].update({
                    key: quiet[key] for key in ("enabled", "start", "end") if key in quiet
                })
            return data["rules"]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.logger.exception("Unable to load delivery policy file: %s", self.path)
            return {}

    def get(self, chat_key: str, now: Optional[datetime] = None) -> DeliveryPolicy:
        try:
            policy = DeliveryPolicy(self._rules.get(chat_key, {}).get("policy", DeliveryPolicy.NORMAL.value))
        except (TypeError, ValueError):
            self.logger.warning("Invalid delivery policy for chat %s", chat_key)
            return DeliveryPolicy.NORMAL
        if policy is not DeliveryPolicy.FILTERED and self._quiet_active(now or datetime.now()):
            return DeliveryPolicy.SILENT
        return policy

    def _quiet_active(self, now: datetime) -> bool:
        quiet = self._settings["quiet_hours"]
        if not quiet.get("enabled"):
            return False
        try:
            start = time.fromisoformat(quiet["start"])
            end = time.fromisoformat(quiet["end"])
        except (TypeError, ValueError):
            return False
        current = now.time().replace(second=0, microsecond=0)
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    def quiet_hours(self) -> dict:
        return self._settings["quiet_hours"].copy()

    def set_quiet_hours(self, start: str, end: str, enabled: bool) -> None:
        time.fromisoformat(start)
        time.fromisoformat(end)
        self._settings["quiet_hours"] = {"enabled": bool(enabled), "start": start, "end": end}
        self._save()

    def set(self, chat_key: str, policy: DeliveryPolicy, name: str = "",
            chat_type: str = "") -> None:
        policy = DeliveryPolicy(policy)
        if policy is DeliveryPolicy.NORMAL:
            self.reset(chat_key)
            return
        self._rules[chat_key] = {
            "policy": policy.value,
            "name": name,
            "type": chat_type,
        }
        self._save()

    def reset(self, chat_key: str) -> None:
        if self._rules.pop(chat_key, None) is not None:
            self._save()

    def list_rules(self) -> Dict[str, dict]:
        return {key: value.copy() for key, value in self._rules.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": self.VERSION, "rules": self._rules, "settings": self._settings}
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=str(self.path.parent),
                    prefix=f".{self.path.name}.", delete=False) as temp_file:
                json.dump(payload, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            os.replace(str(temp_path), str(self.path))
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()
