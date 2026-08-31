import hashlib
import sqlite3
from pathlib import Path
from typing import List


def _directory_bytes(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            total += item.stat().st_size
    return total


def _manifest_status(backup: Path) -> str:
    manifest = backup / "SHA256SUMS"
    if not manifest.is_file() or manifest.is_symlink():
        return "缺失"
    root = backup.resolve()
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
        if not lines:
            return "空清单"
        for line in lines:
            digest, relative = line.split("  ", 1)
            target = (root / relative).resolve()
            if target.parent != root and root not in target.parents:
                return "路径异常"
            if not target.is_file() or target.is_symlink():
                return "文件缺失"
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != digest:
                return "校验失败"
    except (OSError, UnicodeError, ValueError):
        return "校验失败"
    return "正常"


def _sqlite_status(backup: Path) -> str:
    databases = sorted(
        item for item in backup.rglob("*")
        if item.is_file()
        and not item.is_symlink()
        and item.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
    )
    if not databases:
        return "无数据库"
    try:
        for database in databases:
            uri = f"file:{database.resolve()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=3) as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    return "校验失败"
    except (OSError, sqlite3.Error):
        return "校验失败"
    return "正常"


def list_backups(root: Path, restore_source: str = "") -> List[dict]:
    root = Path(root)
    directories = sorted(
        (
            item for item in root.iterdir()
            if item.is_dir()
            and not item.is_symlink()
            and item.name.startswith("config-")
        ),
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    ) if root.is_dir() else []
    latest = directories[0].name if directories else ""
    records = []
    for item in directories:
        protected = []
        if item.name == latest:
            protected.append("最新备份")
        if item.name == str(restore_source or ""):
            protected.append("恢复演练来源")
        manifest = _manifest_status(item)
        sqlite = _sqlite_status(item)
        if manifest != "正常" or sqlite == "校验失败":
            protected.append("校验未通过")
        records.append({
            "name": item.name,
            "created_at": item.stat().st_mtime,
            "bytes": _directory_bytes(item),
            "manifest": manifest,
            "sqlite": sqlite,
            "protected": protected,
            "deletable": not protected,
        })
    return records


def find_backup(records: List[dict], name: str) -> dict:
    return next((item for item in records if item.get("name") == name), {})
