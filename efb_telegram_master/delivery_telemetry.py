import json
import hashlib
import math
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
TRACE_RETENTION_SECONDS = 24 * 60 * 60
TRACE_LIMIT = 100
LATENCY_SAMPLE_LIMIT = 1000
EVENT_LOG_MAX_BYTES = 64 * 1024 * 1024
MESSAGE_TYPE_KEYS = (
    "text", "image", "video", "file", "public_account", "finder", "other",
)


def normalize_message_type(value: str) -> str:
    text = str(value or "").strip().lower().replace("msgtype.", "")
    if text in MESSAGE_TYPE_KEYS:
        return text
    if "text" in text:
        return "text"
    if any(token in text for token in ("image", "photo", "sticker")):
        return "image"
    if "video" in text:
        return "video"
    if any(token in text for token in ("file", "document", "audio", "voice")):
        return "file"
    return "other"


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
    def __init__(self, path: Path, stats_path: Path = None, trace_path: Path = None):
        self.path = Path(path)
        self.stats_path = Path(stats_path) if stats_path else self.path.with_name("delivery-stats.json")
        self.events_path = self.stats_path.with_name("delivery-events.jsonl")
        self.lock = threading.Lock()
        self.state = self._load()
        if self.state.get("pending") is not None:
            self.state["pending"] = None
            self._save()
        self.stats = self._load_stats()
        self.trace_path = Path(trace_path) if trace_path else self.path.with_name("delivery-trace.json")
        self.traces = self._load_traces()

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
                return {
                    "version": 3,
                    "buckets": buckets,
                    "events_complete_from": data.get("events_complete_from"),
                    "last_event_compaction_hour": data.get("last_event_compaction_hour"),
                }
        except (OSError, ValueError, TypeError):
            pass
        return {
            "version": 3,
            "buckets": {},
            "events_complete_from": None,
            "last_event_compaction_hour": None,
        }

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

    def _load_traces(self):
        try:
            data = json.loads(self.trace_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)][-TRACE_LIMIT:]
        except (OSError, ValueError, TypeError):
            pass
        return []

    @staticmethod
    def _trace_id(uid: str, trace_id: str = "") -> str:
        value = str(trace_id or "").strip()
        if re.fullmatch(r"[0-9a-fA-F]{12}", value):
            return value.lower()
        return hashlib.sha256(str(uid).encode("utf-8")).hexdigest()[:12]

    def _record_trace(self, uid: str, stage: str, now: float,
                      message_type: str = "", size: int = 0,
                      reason: str = "", trace_id: str = ""):
        identity = self._trace_id(uid, trace_id)
        previous = next(
            (item for item in reversed(self.traces) if item.get("trace_id") == identity),
            {},
        )
        timestamps = dict(previous.get("trace_timestamps") or {})
        stage_timestamp_names = {
            "received": "efb_received",
            "sending": "telegram_sent",
            "delivered": "telegram_ack",
        }
        timestamp_name = stage_timestamp_names.get(str(stage))
        if timestamp_name:
            timestamps[timestamp_name] = float(now)
        record = {
            "trace_id": identity,
            "stage": str(stage),
            "at": float(now),
            "type": str(message_type or previous.get("type") or ""),
            "size": max(0, int(size or previous.get("size") or 0)),
            "trace_timestamps": timestamps,
        }
        if reason:
            record["error"] = sanitize_failure(reason)
        cutoff = float(now) - TRACE_RETENTION_SECONDS
        self.traces = [
            item for item in self.traces
            if item.get("trace_id") != identity
            and isinstance(item.get("at"), (int, float))
            and float(item["at"]) >= cutoff
        ]
        self.traces.append(record)
        self.traces = self.traces[-TRACE_LIMIT:]
        self._atomic_json(self.trace_path, self.traces, ".delivery-trace.")

    @staticmethod
    def _atomic_json(path: Path, data, prefix: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), prefix=prefix, delete=False,
        ) as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, path)

    @staticmethod
    def _bucket_key(now: float) -> str:
        return str(int(float(now) // 3600) * 3600)

    @staticmethod
    def _increment(target: dict, field: str) -> None:
        try:
            current = int(target.get(field, 0) or 0)
        except (TypeError, ValueError):
            current = 0
        target[field] = current + 1

    @staticmethod
    def _record_latency(target: dict, latency_ms) -> None:
        if latency_ms is None:
            return
        try:
            latency = max(0.0, float(latency_ms))
            total = float(target.get("latency_ms_total", 0) or 0)
            count = int(target.get("latency_count", 0) or 0)
        except (TypeError, ValueError):
            return
        target["latency_ms_total"] = total + latency
        target["latency_count"] = count + 1
        samples = target.get("latency_samples_ms")
        if not isinstance(samples, list):
            samples = []
        samples.append(round(latency))
        target["latency_samples_ms"] = samples[-LATENCY_SAMPLE_LIMIT:]

    def _append_event(self, event: dict) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")

        event_at = float(event["at"])
        if self.stats.get("events_complete_from") is None:
            self.stats["events_complete_from"] = event_at
        hour = int(event_at // 3600)
        previous_hour = self.stats.get("last_event_compaction_hour")
        self.stats["last_event_compaction_hour"] = hour
        if previous_hour is not None and previous_hour != hour:
            self._compact_events(event_at)

    def _compact_events(self, now: float) -> None:
        cutoff = float(now) - STATS_RETENTION_SECONDS
        retained = []
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
                event_at = float(event.get("at"))
            except (ValueError, TypeError):
                continue
            if event_at >= cutoff:
                retained.append((event_at, event))
        retained.sort(key=lambda item: item[0])

        encoded = [
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            for _, event in retained
        ]
        total_size = sum(len(item.encode("utf-8")) for item in encoded)
        removed_until = cutoff
        trimmed_for_size = False
        while encoded and total_size > EVENT_LOG_MAX_BYTES:
            trimmed_for_size = True
            removed_until = max(removed_until, retained[0][0])
            total_size -= len(encoded[0].encode("utf-8"))
            encoded.pop(0)
            retained.pop(0)
        if trimmed_for_size:
            removed_until = retained[0][0] if retained else float(now)

        current_coverage = self.stats.get("events_complete_from")
        try:
            current_coverage = float(current_coverage)
        except (TypeError, ValueError):
            current_coverage = float(now)
        self.stats["events_complete_from"] = max(current_coverage, removed_until)

        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.events_path.parent),
            prefix=".delivery-events.",
            delete=False,
        ) as handle:
            handle.writelines(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, self.events_path)

    def _record_stat(self, field: str, now: float, latency_ms=None,
                     message_type: str = "other"):
        buckets = self.stats.setdefault("buckets", {})
        key = self._bucket_key(now)
        bucket = buckets.get(key)
        if not isinstance(bucket, dict):
            bucket = {
                "inbound": 0,
                "delivered": 0,
                "filtered": 0,
                "failed": 0,
                "silent": 0,
                "latency_ms_total": 0,
                "latency_count": 0,
                "latency_samples_ms": [],
                "last_success_at": None,
                "by_type": {},
            }
            buckets[key] = bucket
        self._increment(bucket, field)
        self._record_latency(bucket, latency_ms)

        category = normalize_message_type(message_type)
        by_type = bucket.setdefault("by_type", {})
        typed = by_type.get(category)
        if not isinstance(typed, dict):
            typed = {}
            by_type[category] = typed
        self._increment(typed, field)
        self._record_latency(typed, latency_ms)
        if field == "delivered":
            bucket["last_success_at"] = float(now)
            typed["last_success_at"] = float(now)
        event = {
            "at": float(now),
            "event": field,
            "type": category,
        }
        if latency_ms is not None:
            try:
                event["latency_ms"] = round(max(0.0, float(latency_ms)))
            except (TypeError, ValueError):
                pass
        self._append_event(event)
        cutoff = float(now) - STATS_RETENTION_SECONDS
        self.stats["buckets"] = {
            bucket_key: value
            for bucket_key, value in buckets.items()
            if _valid_bucket_key(bucket_key) and float(bucket_key) + 3600 >= cutoff
        }

    def record_event(self, event: str, message_type: str,
                     latency_ms=None, now: float = None) -> None:
        if event not in {"inbound", "delivered", "filtered", "failed", "silent"}:
            raise ValueError("unsupported delivery event")
        with self.lock:
            timestamp = time.time() if now is None else float(now)
            self._record_stat(event, timestamp, latency_ms, message_type)
            self._save_stats()

    def inbound(self, uid: str, message_type: str, size: int = 0, trace_id: str = ""):
        with self.lock:
            now = time.time()
            self.state["last_inbound_at"] = now
            message_type = normalize_message_type(message_type)
            self.state["pending"] = {"uid": str(uid), "type": message_type,
                                     "size": int(size), "at": now}
            self._record_stat("inbound", now, message_type=message_type)
            self._record_trace(uid, "received", now, message_type, size, trace_id=trace_id)
            self._save()
            self._save_stats()

    def _finish(self, uid: str, now: float):
        pending = self.state.get("pending") or {}
        if str(pending.get("uid") or "") != str(uid):
            return None, "other"
        try:
            latency_ms = max(0.0, (now - float(pending["at"])) * 1000)
        except (KeyError, TypeError, ValueError):
            latency_ms = None
        if latency_ms is not None:
            self.state["last_latency_ms"] = round(latency_ms)
        self.state["pending"] = None
        return latency_ms, normalize_message_type(pending.get("type"))

    def delivered(self, uid: str, trace_id: str = "", silent: bool = False):
        with self.lock:
            now = time.time()
            self.state["last_delivered_at"] = now
            latency_ms, message_type = self._finish(uid, now)
            self._record_stat("delivered", now, latency_ms, message_type)
            if silent:
                self._record_stat("silent", now, message_type=message_type)
            self._record_trace(uid, "delivered", now, trace_id=trace_id)
            self.state["last_failure"] = None
            self._save()
            self._save_stats()

    def sending(self, uid: str, trace_id: str = ""):
        with self.lock:
            self._record_trace(uid, "sending", time.time(), trace_id=trace_id)

    def filtered(self, uid: str, trace_id: str = ""):
        with self.lock:
            now = time.time()
            self.state["last_filtered_at"] = now
            latency_ms, message_type = self._finish(uid, now)
            self._record_stat("filtered", now, latency_ms, message_type)
            self._record_trace(uid, "filtered", now, trace_id=trace_id)
            self._save()
            self._save_stats()

    def failed(self, uid: str, reason: str, trace_id: str = ""):
        with self.lock:
            now = time.time()
            self.state["last_failure"] = {
                "uid": str(uid),
                "reason": sanitize_failure(reason),
                "at": now,
            }
            latency_ms, message_type = self._finish(uid, now)
            self._record_stat("failed", now, latency_ms, message_type)
            self._record_trace(uid, "failed", now, reason=reason, trace_id=trace_id)
            self._save()
            self._save_stats()


def _valid_bucket_key(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _percentile_95(values: list):
    valid = []
    for value in values:
        try:
            valid.append(max(0.0, float(value)))
        except (TypeError, ValueError):
            continue
    if not valid:
        return None
    valid.sort()
    return round(valid[max(0, math.ceil(len(valid) * 0.95) - 1)])


def _empty_type_summary() -> dict:
    return {
        "inbound": 0,
        "delivered": 0,
        "filtered": 0,
        "failed": 0,
        "silent": 0,
        "average_latency_ms": None,
        "p95_latency_ms": None,
        "last_success_at": None,
    }


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
        "silent": 0,
        "average_latency_ms": None,
        "p95_latency_ms": None,
        "last_success_at": None,
        "by_type": {},
    }
    latency_total = 0.0
    latency_count = 0
    latency_samples = []
    typed_totals = {}

    def apply_event(event: dict) -> None:
        nonlocal latency_total, latency_count
        if not isinstance(event, dict):
            return
        try:
            event_at = float(event.get("at"))
        except (TypeError, ValueError):
            return
        if event_at < cutoff or event_at > now:
            return
        field = str(event.get("event") or "")
        if field not in {"inbound", "delivered", "filtered", "failed", "silent"}:
            return
        category = normalize_message_type(event.get("type"))
        aggregate = typed_totals.setdefault(category, {
            **_empty_type_summary(),
            "latency_ms_total": 0.0,
            "latency_count": 0,
            "latency_samples_ms": [],
        })
        result[field] += 1
        aggregate[field] += 1
        try:
            latency = max(0.0, float(event.get("latency_ms")))
        except (TypeError, ValueError):
            latency = None
        if latency is not None:
            latency_total += latency
            latency_count += 1
            latency_samples.append(latency)
            aggregate["latency_ms_total"] += latency
            aggregate["latency_count"] += 1
            aggregate["latency_samples_ms"].append(latency)
        if field == "delivered":
            result["last_success_at"] = max(
                float(result.get("last_success_at") or 0), event_at
            )
            aggregate["last_success_at"] = max(
                float(aggregate.get("last_success_at") or 0), event_at
            )

    events_path = Path(state_root) / "delivery-events.jsonl"
    try:
        coverage = float(data.get("events_complete_from"))
    except (TypeError, ValueError):
        coverage = None
    use_event_log = bool(coverage is not None and coverage <= cutoff and events_path.exists())
    if use_event_log:
        try:
            for line in events_path.read_text(encoding="utf-8").splitlines():
                try:
                    apply_event(json.loads(line))
                except (ValueError, TypeError):
                    continue
        except OSError:
            use_event_log = False

    buckets = {} if use_event_log else (data.get("buckets") or {})
    for key, bucket in buckets.items():
        if not _valid_bucket_key(key) or not isinstance(bucket, dict):
            continue
        bucket_time = float(key)
        if bucket_time + 3600 < cutoff or bucket_time > now:
            continue
        for field in ("inbound", "delivered", "filtered", "failed", "silent"):
            try:
                result[field] += max(0, int(bucket.get(field, 0) or 0))
            except (TypeError, ValueError):
                continue
        try:
            latency_total += max(0.0, float(bucket.get("latency_ms_total", 0) or 0))
            latency_count += max(0, int(bucket.get("latency_count", 0) or 0))
        except (TypeError, ValueError):
            pass
        if isinstance(bucket.get("latency_samples_ms"), list):
            latency_samples.extend(bucket["latency_samples_ms"])
        try:
            success_at = float(bucket.get("last_success_at") or 0)
            if cutoff <= success_at <= now:
                result["last_success_at"] = max(
                    float(result.get("last_success_at") or 0),
                    success_at,
                )
        except (TypeError, ValueError):
            pass

        for category, typed in (bucket.get("by_type") or {}).items():
            if not isinstance(typed, dict):
                continue
            category = normalize_message_type(category)
            aggregate = typed_totals.setdefault(category, {
                **_empty_type_summary(),
                "latency_ms_total": 0.0,
                "latency_count": 0,
                "latency_samples_ms": [],
            })
            for field in ("inbound", "delivered", "filtered", "failed", "silent"):
                try:
                    aggregate[field] += max(0, int(typed.get(field, 0) or 0))
                except (TypeError, ValueError):
                    continue
            try:
                aggregate["latency_ms_total"] += max(
                    0.0, float(typed.get("latency_ms_total", 0) or 0)
                )
                aggregate["latency_count"] += max(
                    0, int(typed.get("latency_count", 0) or 0)
                )
            except (TypeError, ValueError):
                pass
            if isinstance(typed.get("latency_samples_ms"), list):
                aggregate["latency_samples_ms"].extend(typed["latency_samples_ms"])
            try:
                typed_success = float(typed.get("last_success_at") or 0)
                if cutoff <= typed_success <= now:
                    aggregate["last_success_at"] = max(
                        float(aggregate.get("last_success_at") or 0),
                        typed_success,
                    )
            except (TypeError, ValueError):
                pass
    if latency_count:
        result["average_latency_ms"] = round(latency_total / latency_count)
    result["p95_latency_ms"] = _percentile_95(latency_samples)
    for category in MESSAGE_TYPE_KEYS:
        aggregate = typed_totals.get(category)
        if not aggregate:
            continue
        if aggregate["latency_count"]:
            aggregate["average_latency_ms"] = round(
                aggregate["latency_ms_total"] / aggregate["latency_count"]
            )
        aggregate["p95_latency_ms"] = _percentile_95(
            aggregate.pop("latency_samples_ms")
        )
        aggregate.pop("latency_ms_total")
        aggregate.pop("latency_count")
        result["by_type"][category] = aggregate
    return result


def digest_delta(current: dict, previous: dict) -> dict:
    result = {}
    for field in ("silent", "filtered", "failed"):
        try:
            value = int(current.get(field, 0) or 0) - int(previous.get(field, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        result[field] = max(0, value)
    return result


def delivery_trace_summary(state_root: Path, now=None) -> list:
    path = Path(state_root) / "delivery-trace.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    now = time.time() if now is None else float(now)
    cutoff = now - TRACE_RETENTION_SECONDS
    return [
        item for item in data
        if isinstance(item, dict)
        and isinstance(item.get("at"), (int, float))
        and cutoff <= float(item["at"]) <= now
    ][-TRACE_LIMIT:]


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


class DigestGuard:
    """Send aggregate delivery changes without retaining message content."""

    def __init__(self, channel, state_path: Path, stats_loader, interval: int = 3600):
        self.channel = channel
        self.state_path = Path(state_path)
        self.stats_loader = stats_loader
        self.interval = max(300, int(interval))

    def _load(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self, data):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        DeliveryTelemetry._atomic_json(self.state_path, data, ".digest.")

    def check_once(self, now=None):
        now = time.time() if now is None else float(now)
        current = self.stats_loader()
        previous = self._load()
        if not previous:
            self._save({**current, "checked_at": now})
            return "baseline"
        if now - float(previous.get("checked_at", 0) or 0) < self.interval:
            return "wait"
        delta = digest_delta(current, previous)
        self._save({**current, "checked_at": now})
        if not any(delta.values()):
            return "empty"
        text = (
            "EFB 静默投递摘要\n\n"
            f"静默接收：{delta['silent']} 条\n"
            f"完全过滤：{delta['filtered']} 条\n"
            f"失败：{delta['failed']} 条\n\n"
            "仅统计数量，不保存消息正文。"
        )
        for admin in self.channel.config["admins"]:
            self.channel.bot_manager.send_message(admin, text)
        return "sent"

    def run(self):
        while True:
            time.sleep(60)
            self.check_once()

    def start(self):
        threading.Thread(target=self.run, name="efb-digest-guard", daemon=True).start()
