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
                    last_restart_uid: str = "", stall_seconds: int = 600,
                    cooldown_seconds: int = 3600) -> str:
    pending = state.get("pending") or {}
    started = pending.get("at")
    if not isinstance(started, (int, float)) or now - started < stall_seconds:
        return "none"
    if not logged_in:
        return "alert"
    if str(pending.get("uid") or "") == str(last_restart_uid or ""):
        return "alert"
    if last_restart_at and now - last_restart_at < cooldown_seconds:
        return "alert"
    return "restart"


class DeliveryTelemetry:
    STATS_WINDOW_SECONDS = 24 * 60 * 60
    MAX_STATS_EVENTS = 4096

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

    @staticmethod
    def _event_time(item):
        if not isinstance(item, dict):
            return None
        try:
            return float(item.get("at"))
        except (TypeError, ValueError):
            return None

    def _record_stat(self, kind, at=None, delay_ms=None):
        now = time.time() if at is None else float(at)
        events = self.state.setdefault("stats", [])
        if not isinstance(events, list):
            events = []
        cutoff = now - self.STATS_WINDOW_SECONDS
        events = [
            item for item in events
            for event_time in (self._event_time(item),)
            if event_time is not None and event_time >= cutoff
        ]
        event = {"at": now, "kind": str(kind)}
        if delay_ms is not None:
            event["delay_ms"] = max(0, int(delay_ms))
        events.append(event)
        self.state["stats"] = events[-self.MAX_STATS_EVENTS:]

    def stats_snapshot(self, now=None):
        now = time.time() if now is None else float(now)
        with self.lock:
            events = self.state.get("stats", [])
            if not isinstance(events, list):
                events = []
            cutoff = now - self.STATS_WINDOW_SECONDS
            recent = [
                item for item in events
                for event_time in (self._event_time(item),)
                if event_time is not None and event_time >= cutoff
            ]
            delays = [
                max(0, int(item["delay_ms"]))
                for item in recent
                if item.get("delay_ms") is not None
                and str(item.get("delay_ms")).lstrip("-").isdigit()
            ]
            counts = {"received": 0, "delivered": 0, "filtered": 0, "failed": 0}
            for item in recent:
                kind = item.get("kind")
                if kind in counts:
                    counts[kind] += 1
            counts["average_delay_ms"] = int(sum(delays) / len(delays)) if delays else 0
            counts["window_seconds"] = self.STATS_WINDOW_SECONDS
            return counts

    def inbound(self, uid: str, message_type: str, size: int = 0):
        with self.lock:
            now = time.time()
            self._record_stat("received", now)
            self.state["last_inbound_at"] = now
            self.state["pending"] = {"uid": str(uid), "type": str(message_type),
                                     "size": int(size), "at": now}
            self._save()

    def delivered(self, uid: str):
        with self.lock:
            delivered_at = time.time()
            self.state["last_delivered_at"] = delivered_at
            pending = self.state.get("pending") or {}
            delay_ms = None
            if str(pending.get("uid") or "") == str(uid) and isinstance(pending.get("at"), (int, float)):
                delay_ms = (delivered_at - pending["at"]) * 1000
            self._record_stat("delivered", delivered_at, delay_ms)
            if (self.state.get("pending") or {}).get("uid") == str(uid):
                self.state["pending"] = None
            self.state["last_failure"] = None
            self._save()

    def filtered(self, uid: str):
        with self.lock:
            self.state["last_filtered_at"] = time.time()
            self._record_stat("filtered", self.state["last_filtered_at"])
            if (self.state.get("pending") or {}).get("uid") == str(uid):
                self.state["pending"] = None
            self._save()

    def failed(self, uid: str, reason: str):
        with self.lock:
            now = time.time()
            self._record_stat("failed", now)
            self.state["last_failure"] = {"uid": str(uid), "reason": sanitize_failure(reason),
                                          "at": now}
            if (self.state.get("pending") or {}).get("uid") == str(uid):
                self.state["pending"] = None
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
            return {"last_restart_at": 0, "last_restart_uid": ""}

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
        pending = self.telemetry.state.get("pending") or {}
        logged_in = self._logged_in()
        action = recovery_action(
            self.telemetry.state,
            logged_in,
            now,
            recovery.get("last_restart_at", 0),
            recovery.get("last_restart_uid", ""),
        )
        key = (pending.get("uid"), action)
        if action == "none":
            self.last_alert_key = None
            return action
        if key != self.last_alert_key:
            if action == "restart":
                self._alert("EFB 检测到消息链路卡住超过10分钟；本消息最多只重启一次 EFB，微信容器不会重启。")
            else:
                if not logged_in:
                    suffix = "微信已退出，因此不会重启 EFB。"
                elif str(pending.get("uid") or "") == str(recovery.get("last_restart_uid") or ""):
                    suffix = "本消息已经尝试过一次恢复，不会再次重启。"
                else:
                    suffix = "处于1小时冷却期，不会重复重启。"
                self._alert("EFB 检测到消息链路异常。" + suffix)
            self.last_alert_key = key
        if action == "restart":
            recovery["last_restart_at"] = now
            recovery["last_restart_uid"] = str(pending.get("uid") or "")
            self._save_recovery_state(recovery)
        return action

    def run(self):
        while True:
            time.sleep(60)
            if self.check_once() == "restart":
                os._exit(75)

    def start(self):
        threading.Thread(target=self.run, name="efb-delivery-guard", daemon=True).start()
