import os
from pathlib import Path

from efb_telegram_master.operations_ui import (
    _human_size,
    backup_summary,
    redact_error,
    scan_sensitive_keys,
)


def test_backup_summary_reports_count_and_latest_without_file_content(tmp_path: Path):
    first = tmp_path / "config-20260718-010000"
    second = tmp_path / "config-20260718-020000"
    first.mkdir()
    second.mkdir()
    os.utime(first, (2000, 2000))
    os.utime(second, (1000, 1000))

    result = backup_summary(tmp_path)

    assert result["count"] == 2
    assert result["latest"] == first.name


def test_human_size_uses_complete_unit_sequence():
    assert _human_size(int(1.5 * 1024**3)) == "1.50 GB"


def test_redact_error_removes_bot_tokens_and_urls():
    text = "request https://host/bot123456:ABC_secret/sendMessage failed"

    assert "ABC_secret" not in redact_error(text)
    assert "https://" not in redact_error(text)


def test_security_scan_returns_key_names_without_values(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text("token: very-secret\nadmins: [1]\n", encoding="utf-8")

    findings = scan_sensitive_keys(tmp_path)

    assert findings == [{"file": "config.yaml", "keys": ["token"]}]
    assert "very-secret" not in str(findings)
