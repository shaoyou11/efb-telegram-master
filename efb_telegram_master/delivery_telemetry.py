import json
import os
import re
import tempfile
import threading
import time
from urllib import request
from pathlib import Path


TOKEN = re.compile(r"bot\d+:[^/\s]+")
URL = re.compile(r"https?://[^\s]+")
PATH = re.compile(r"(?:/[\w .-]+){2,}")


def sanitize_failure(value: str) -> str:
    text = TOKEN.sub("bot<redacted>", str(value))
    text = URL.sub("<endpoint>", text)
    return PATH.sub("<path>", text)[:200]


def recovery_action(state: dict, logged_in: bool, now: float, last_restart_at: float,
                    stall_seconds: int = 600, cooldown_seconds: int = 3600) -> str:
    pending = state.get("pending") or {}
    started = pending.get("at")
    if not isinstance(started, (int, float)) or now - started < stall_seconds:
        return "none"
    if not logged_in:
        return "alert"
    if last_restart_at and now - last_restart_at < cooldown_seconds:
        return "alert"
    return "restart"


class DeliveryTelemetry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.state = self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError, TypeError):
            pass
        return {"pending": None, "last_inbound_at": None, "last_delivered_at": None,
                "last_filtered_at": None, "last_failure": None}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(self.path.parent),
                                         prefix=".delivery.", delete=False) as handle:
            json.dump(self.state, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, self.path)

    def inbound(self, uid: str, message_type: str, size: int = 0):
        with self.lock:
            now = time.time()
            self.state["last_inbound_at"] = now
            self.state["pending"] = {"uid": str(uid), "type": str(message_type),
                                     "size": int(size), "at": now}
            self._save()

    def delivered(self, uid: str):
        with self.lock:
            self.state["last_delivered_at"] = time.time()
            if (self.state.get("pending") or {}).get("uid") == str(uid):
                self.state["pending"] = None
            self.state["last_failure"] = None
            self._save()

    def filtered(self, uid: str):
        with self.lock:
            self.state["last_filtered_at"] = time.time()
            if (self.state.get("pending") or {}).get("uid") == str(uid):
                self.state["pending"] = None
            self._save()

    def failed(self, uid: str, reason: str):
        with self.lock:
            self.state["last_failure"] = {"uid": str(uid), "reason": sanitize_failure(reason),
                                          "at": time.time()}
            self._save()


class DeliveryGuard:
    def __init__(self, telemetry: DeliveryTelemetry, channel,
                 state_path: Path = Path("/data/operations/state/recovery.json")):
        self.telemetry = telemetry
        self.channel = channel
        self.state_path = Path(state_path)
        self.last_alert_key = None

    def _recovery_state(self):
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"last_restart_at": 0}

    def _save_recovery_state(self, state):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    @staticmethod
    def _logged_in() -> bool:
        try:
            req = request.Request("http://127.0.0.1:18888/api/?type=0", data=b"{}",
                                  headers={"Content-Type": "application/json"})
            with request.urlopen(req, timeout=5) as response:
                return json.loads(response.read().decode("utf-8")).get("is_login") == 1
        except Exception:
            return False

    def _alert(self, text):
        for admin in self.channel.config["admins"]:
            self.channel.bot_manager.send_message(admin, text)

    def check_once(self, now=None):
        now = now or time.time()
        recovery = self._recovery_state()
        action = recovery_action(self.telemetry.state, self._logged_in(), now,
                                 recovery.get("last_restart_at", 0))
        pending = self.telemetry.state.get("pending") or {}
        key = (pending.get("uid"), action)
        if action == "none":
            self.last_alert_key = None
            return action
        if key != self.last_alert_key:
            if action == "restart":
                self._alert("EFB 检测到消息链路卡住超过10分钟，将只重启一次 EFB；微信容器不会重启。")
            else:
                suffix = "微信已退出，因此不会重启 EFB。" if not self._logged_in() else "处于1小时冷却期，不会重复重启。"
                self._alert("EFB 检测到消息链路异常。" + suffix)
            self.last_alert_key = key
        if action == "restart":
            recovery["last_restart_at"] = now
            self._save_recovery_state(recovery)
        return action

    def run(self):
        while True:
            time.sleep(60)
            if self.check_once() == "restart":
                os._exit(75)

    def start(self):
        threading.Thread(target=self.run, name="efb-delivery-guard", daemon=True).start()
