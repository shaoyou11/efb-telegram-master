import json
from pathlib import Path
from typing import Any, Dict, List


def _load(path: Path, default: Any):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return default


def _recipient(record: dict) -> str:
    destination = record.get("tg_dest")
    thread = record.get("thread_id")
    if destination in (None, ""):
        return "未知"
    return f"{destination}/{thread}" if thread not in (None, "") else str(destination)


def _valid_assoc_id(value: Any) -> bool:
    parts = str(value or "").split(" ", 2)
    return len(parts) >= 2 and bool(parts[0].strip()) and bool(parts[1].strip())


def audit_chat_mappings(db) -> List[Dict[str, Any]]:
    """Find structural mapping problems without changing the mapping database."""
    findings = []
    try:
        associations = db.get_all_chat_assocs()
        topic_associations = db.get_all_topic_assocs()
    except Exception as error:
        return [{
            "id": "mapping-database",
            "severity": "高",
            "kind": "映射数据库",
            "recipient": "管理员",
            "action": "view",
            "detail": f"读取映射失败：{error}"[:200],
        }]

    for master_uid, slave_uids in (associations or {}).items():
        if not _valid_assoc_id(master_uid):
            findings.append({
                "id": f"master:{master_uid}",
                "severity": "高",
                "kind": "聊天映射",
                "recipient": str(master_uid),
                "action": "repair",
                "detail": "主会话标识格式异常",
            })
        seen = set()
        for slave_uid in slave_uids or []:
            if slave_uid in seen:
                findings.append({
                    "id": f"duplicate:{master_uid}:{slave_uid}",
                    "severity": "中",
                    "kind": "聊天映射",
                    "recipient": str(slave_uid),
                    "action": "repair",
                    "detail": "同一主会话存在重复从会话映射",
                })
            seen.add(slave_uid)
            if not _valid_assoc_id(slave_uid):
                findings.append({
                    "id": f"slave:{slave_uid}",
                    "severity": "高",
                    "kind": "聊天映射",
                    "recipient": str(slave_uid),
                    "action": "repair",
                    "detail": "从会话标识格式异常",
                })

    for topic_chat_id, slave_uids in (topic_associations or {}).items():
        try:
            topic_rows = db.get_topic_slaves(topic_chat_id) or []
        except Exception:
            topic_rows = []
        seen_topics = set()
        for slave_uid, thread_id in topic_rows:
            key = (str(slave_uid), str(thread_id))
            if key in seen_topics:
                findings.append({
                    "id": f"topic-duplicate:{topic_chat_id}:{thread_id}:{slave_uid}",
                    "severity": "中",
                    "kind": "话题映射",
                    "recipient": str(slave_uid),
                    "action": "repair",
                    "detail": f"话题 {thread_id} 存在重复映射",
                })
            seen_topics.add(key)
            try:
                valid_thread = int(thread_id) > 0
            except (TypeError, ValueError):
                valid_thread = False
            if not _valid_assoc_id(slave_uid) or not valid_thread:
                findings.append({
                    "id": f"topic:{topic_chat_id}:{thread_id}:{slave_uid}",
                    "severity": "高",
                    "kind": "话题映射",
                    "recipient": str(slave_uid),
                    "action": "repair",
                    "detail": "话题或从会话标识格式异常",
                })
    return findings


def build_issues(data_root: Path, db=None) -> List[Dict[str, Any]]:
    root = Path(data_root)
    state = root / "operations" / "state"
    issues = []
    if db is not None:
        issues.extend(audit_chat_mappings(db))
    failed = _load(state / "failed-deliveries.json", {})
    if isinstance(failed, dict):
        records = failed.get("items", failed)
        if isinstance(records, dict):
            for token, record in records.items():
                if not isinstance(record, dict):
                    continue
                issues.append({
                    "id": str(token),
                    "severity": "高",
                    "kind": "投递失败",
                    "recipient": _recipient(record),
                    "action": "retry" if record.get("path") else "view",
                    "detail": str(record.get("error") or "未知投递错误")[:200],
                })

    health = _load(state / "health-guard.json", {})
    if isinstance(health, dict) and health.get("healthy") is False:
        issues.append({
            "id": "health-guard",
            "severity": "高",
            "kind": "运行健康",
            "recipient": "管理员",
            "action": "view",
            "detail": str(health.get("reason") or "健康检查未通过")[:200],
        })

    capacity = _load(root / "capacity-audit-latest.json", {})
    disk = capacity.get("disk", {}) if isinstance(capacity, dict) else {}
    free_percent = disk.get("free_percent") if isinstance(disk, dict) else None
    if isinstance(free_percent, (int, float)) and free_percent < 10:
        issues.append({
            "id": "capacity",
            "severity": "中",
            "kind": "磁盘空间",
            "recipient": "管理员",
            "action": "view",
            "detail": f"剩余 {free_percent:.2f}%",
        })
    rank = {"高": 0, "中": 1, "低": 2}
    return sorted(issues, key=lambda item: (rank.get(item["severity"], 9), item["id"]))
