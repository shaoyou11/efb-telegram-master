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
STATS_RETENTION_SECONDS = 3 * 24 * 60 * 60


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
    def __init__(self, path: Path, stats_path: Path = None):
        self.path = Path(path)
        self.stats_path = Path(stats_path) if stats_path else self.path.with_name("delivery-stats.json")
        self.lock = threading.Lock()
        self.state = self._load()
        self.stats = self._load_stats()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("last_latency_ms", None)
                return data
        except (OSError, ValueError, TypeError):
            pass
        return {
            "pending": None,
            "last_inbound_at": None,
            "last_delivered_at": None,
            "last_filtered_at": None,
            "last_failure": None,
            "last_latency_ms": None,
        }

    def _load_stats(self):
        try:
            data = json.loads(self.stats_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("buckets"), dict):
                buckets = {
                    str(key): value
                    for key, value in data["buckets"].items()
                    if _valid_bucket_key(key) and isinstance(value, dict)
                }
                return {"version": 1, "buckets": buckets}
        except (OSError, ValueError, TypeError):
            pass
        return {"version": 1, "buckets": {}}

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

    def _save_stats(self):
        self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(self.stats_path.parent),
            prefix=".delivery-stats.", delete=False,
        ) as handle:
            json.dump(self.stats, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, self.stats_path)

    @staticmethod
    def _bucket_key(now: float) -> str:
        return str(int(float(now) // 3600) * 3600)

    def _record_stat(self, field: str, now: float, latency_ms=None):
        buckets = self.stats.setdefault("buckets", {})
        key = self._bucket_key(now)
        bucket = buckets.get(key)
        if not isinstance(bucket, dict):
            bucket = {
                "inbound": 0,
                "delivered": 0,
                "filtered": 0,
                "failed": 0,
                "latency_ms_total": 0,
                "latency_count": 0,
            }
            buckets[key] = bucket
        try:
            current = int(bucket.get(field, 0) or 0)
        except (TypeError, ValueError):
            current = 0
        bucket[field] = current + 1
        if latency_ms is not None:
            try:
                latency_total = float(bucket.get("latency_ms_total", 0) or 0)
            except (TypeError, ValueError):
                latency_total = 0.0
            try:
                latency_count = int(bucket.get("latency_count", 0) or 0)
            except (TypeError, ValueError):
                latency_count = 0
            bucket["latency_ms_total"] = latency_total + latency_ms
            bucket["latency_count"] = latency_count + 1
        cutoff = float(now) - STATS_RETENTION_SECONDS
        self.stats["buckets"] = {
            bucket_key: value
            for bucket_key, value in buckets.items()
            if _valid_bucket_key(bucket_key) and float(bucket_key) >= cutoff
        }

    def inbound(self, uid: str, message_type: str, size: int = 0):
        with self.lock:
            now = time.time()
            self.state["last_inbound_at"] = now
            self.state["pending"] = {"uid": str(uid), "type": str(message_type),
                                     "size": int(size), "at": now}
            self._record_stat("inbound", now)
            self._save()
            self._save_stats()

    def _finish(self, uid: str, now: float):
        pending = self.state.get("pending") or {}
        if str(pending.get("uid") or "") != str(uid):
            return None
        try:
            latency_ms = max(0.0, (now - float(pending["at"])) * 1000)
        except (KeyError, TypeError, ValueError):
            latency_ms = None
        if latency_ms is not None:
            self.state["last_latency_ms"] = round(latency_ms)
        self.state["pending"] = None
        return latency_ms

    def delivered(self, uid: str):
        with self.lock:
            now = time.time()
            self.state["last_delivered_at"] = now
            latency_ms = self._finish(uid, now)
            self._record_stat("delivered", now, latency_ms)
            self.state["last_failure"] = None
            self._save()
            self._save_stats()

    def filtered(self, uid: str):
        with self.lock:
            now = time.time()
            self.state["last_filtered_at"] = now
            latency_ms = self._finish(uid, now)
            self._record_stat("filtered", now, latency_ms)
            self._save()
            self._save_stats()

    def failed(self, uid: str, reason: str):
        with self.lock:
            now = time.time()
            self.state["last_failure"] = {
                "uid": str(uid),
                "reason": sanitize_failure(reason),
                "at": now,
            }
            latency_ms = self._finish(uid, now)
            self._record_stat("failed", now, latency_ms)
            self._save()
            self._save_stats()


def _valid_bucket_key(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def delivery_stats_summary(state_root: Path, now=None) -> dict:
    path = Path(state_root) / "delivery-stats.json"
    data = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError, TypeError):
        pass

    now = time.time() if now is None else float(now)
    cutoff = now - 24 * 60 * 60
    result = {
        "inbound": 0,
        "delivered": 0,
        "filtered": 0,
        "failed": 0,
        "average_latency_ms": None,
    }
    latency_total = 0.0
    latency_count = 0
    for key, bucket in (data.get("buckets") or {}).items():
        if not _valid_bucket_key(key) or not isinstance(bucket, dict):
            continue
        bucket_time = float(key)
        if bucket_time < cutoff or bucket_time > now:
            continue
        for field in ("inbound", "delivered", "filtered", "failed"):
            try:
                result[field] += max(0, int(bucket.get(field, 0) or 0))
            except (TypeError, ValueError):
                continue
        try:
            latency_total += max(0.0, float(bucket.get("latency_ms_total", 0) or 0))
            latency_count += max(0, int(bucket.get("latency_count", 0) or 0))
        except (TypeError, ValueError):
            continue
    if latency_count:
        result["average_latency_ms"] = round(latency_total / latency_count)
    return result


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
