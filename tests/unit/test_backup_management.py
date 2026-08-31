import hashlib
import os
import sqlite3

from efb_telegram_master.backup_management import find_backup, list_backups


def create_backup(root, name, valid=True):
    backup = root / name
    backup.mkdir()
    database = backup / "mapping.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE item (id INTEGER PRIMARY KEY)")
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    if not valid:
        digest = "0" * 64
    (backup / "SHA256SUMS").write_text(
        f"{digest}  mapping.db\n", encoding="utf-8"
    )
    return backup


def test_backup_list_protects_latest_and_restore_source(tmp_path):
    first = create_backup(tmp_path, "config-20260831-010000")
    second = create_backup(tmp_path, "config-20260831-020000")
    os.utime(first, (1000, 1000))
    os.utime(second, (2000, 2000))

    records = list_backups(tmp_path, restore_source=first.name)

    assert records[0]["name"] == second.name
    assert records[0]["protected"] == ["最新备份"]
    assert "恢复演练来源" in records[1]["protected"]


def test_backup_with_invalid_manifest_cannot_be_deleted(tmp_path):
    create_backup(tmp_path, "config-20260831-010000")
    invalid = create_backup(tmp_path, "config-20260830-010000", valid=False)

    record = find_backup(list_backups(tmp_path), invalid.name)

    assert record["manifest"] == "校验失败"
    assert record["deletable"] is False
    assert "校验未通过" in record["protected"]


def test_backup_list_ignores_symlink_and_non_backup_directory(tmp_path):
    source = create_backup(tmp_path, "config-20260831-010000")
    (tmp_path / "notes").mkdir()
    (tmp_path / "config-link").symlink_to(source, target_is_directory=True)

    records = list_backups(tmp_path)

    assert [item["name"] for item in records] == [source.name]
