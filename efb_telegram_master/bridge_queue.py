import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import request
from urllib.error import HTTPError, URLError


BOT_TOKEN = re.compile(r"(?i)bot\d+:[^/\s]+")
URL = re.compile(r"https?://[^\s]+")


class BridgeQueueError(RuntimeError):
    """Bounded, user-safe Bridge queue error."""


def _safe_error(value: Any) -> str:
    text = BOT_TOKEN.sub("bot<redacted>", str(value))
    text = URL.sub("<endpoint>", text)
    return text[:160]


class BridgeQueueSettings:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._enabled = self._load()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        if not isinstance(value, bool):
            raise ValueError("management_enabled must be a boolean")
        self.save(value)
        self._enabled = value

    def _load(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        value = payload.get("management_enabled", False) if isinstance(payload, dict) else False
        return value is True

    def save(self, enabled: Optional[bool] = None) -> None:
        value = self._enabled if enabled is None else enabled
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(self.path.parent),
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                json.dump({"management_enabled": value}, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary = handle.name
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


class BridgeQueueClient:
    def __init__(self, base_url: str, timeout: int = 5):
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = max(1, int(timeout))

    def _request(self, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="GET" if data is None else "POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise BridgeQueueError("Bridge 接口返回异常") from error
        except (URLError, TimeoutError, OSError) as error:
            raise BridgeQueueError("Bridge 接口暂时不可用") from error
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise BridgeQueueError("Bridge 接口返回格式异常") from error
        except Exception as error:
            raise BridgeQueueError(f"Bridge 请求失败：{_safe_error(error)}") from error
        if not isinstance(result, dict):
            raise BridgeQueueError("Bridge 接口返回格式异常")
        if result.get("ok") is False:
            raise BridgeQueueError("Bridge 接口拒绝操作")
        return result

    def health(self) -> Dict[str, Any]:
        return self._request("/healthz")

    def trace(self, limit: int = 20) -> List[Dict[str, Any]]:
        result = self._request(
            f"/v1/messages/trace?limit={min(100, max(1, int(limit)))}"
        )
        messages = result.get("messages", [])
        return messages if isinstance(messages, list) else []

    def active(self, limit: int = 10) -> List[Dict[str, Any]]:
        messages, _ = self.active_page(limit)
        return messages

    def active_page(self, limit: int = 10, offset: int = 0):
        result = self._request(
            f"/v1/messages/active?limit={min(100, max(1, int(limit)))}"
            f"&offset={max(0, int(offset))}"
        )
        messages = result.get("messages", [])
        total = result.get("total", len(messages))
        try:
            total = max(0, int(total))
        except (TypeError, ValueError):
            total = len(messages)
        return (messages if isinstance(messages, list) else [], total)

    def dead(self, limit: int = 10) -> List[Dict[str, Any]]:
        messages, _ = self.dead_page(limit)
        return messages

    def dead_page(self, limit: int = 10, offset: int = 0):
        result = self._request(
            f"/v1/messages/dead?limit={min(100, max(1, int(limit)))}"
            f"&offset={max(0, int(offset))}"
        )
        messages = result.get("messages", [])
        total = result.get("total", len(messages))
        try:
            total = max(0, int(total))
        except (TypeError, ValueError):
            total = len(messages)
        return (messages if isinstance(messages, list) else [], total)

    def retry_active(self, message_id: str) -> str:
        result = self._request(
            "/v1/messages/retry-active", {"message_id": str(message_id)}
        )
        return str(result.get("result") or "not_found")

    def requeue_dead(self, message_id: str) -> bool:
        result = self._request("/v1/messages/requeue", {"message_id": str(message_id)})
        return result.get("requeued") == 1

    def discard(self, message_id: str, reason: str = "admin") -> str:
        result = self._request(
            "/v1/messages/discard",
            {"message_id": str(message_id), "reason": str(reason)},
        )
        return str(result.get("result") or "not_found")

    def requeue_all_dead(self) -> int:
        return len(self.requeue_all_dead_ids())

    def requeue_all_dead_ids(self) -> List[str]:
        result = self._request("/v1/messages/requeue-all-dead", {})
        message_ids = result.get("message_ids", [])
        if not isinstance(message_ids, list):
            return []
        return [str(item) for item in message_ids if isinstance(item, str) and item]

    def discard_all_dead(self, reason: str = "admin") -> int:
        return len(self.discard_all_dead_ids(reason))

    def discard_all_dead_ids(self, reason: str = "admin") -> List[str]:
        result = self._request(
            "/v1/messages/discard-all-dead", {"reason": str(reason)}
        )
        message_ids = result.get("message_ids", [])
        if not isinstance(message_ids, list):
            return []
        return [str(item) for item in message_ids if isinstance(item, str) and item]

    def retry_all_active(self) -> int:
        result = self._request("/v1/messages/retry-all-active", {})
        try:
            return int(result.get("retried", 0))
        except (TypeError, ValueError):
            return 0

    def discard_all_active(self, reason: str = "admin") -> int:
        result = self._request(
            "/v1/messages/discard-all-active", {"reason": str(reason)}
        )
        try:
            return int(result.get("discarded", 0))
        except (TypeError, ValueError):
            return 0
