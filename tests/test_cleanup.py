from pathlib import Path
from tempfile import TemporaryDirectory

from efb_telegram_master.cleanup import build_storage_text, load_storage_report


def test_build_storage_text_shows_usage_policy_and_host_paths():
    report = {
        "cache": {"bytes": 10 * 1024**2},
        "sns_cache": {"bytes": 20 * 1024**2},
        "attachments": {"bytes": 3 * 1024**3},
        "backups": {"bytes": 2 * 1024**3},
        "backup_count": 26,
    }

    text = build_storage_text(report, "/vol4/1000/docker/efb")

    assert "普通缓存：10.00 MB" in text
    assert "朋友圈缓存：20.00 MB" in text
    assert "聊天附件：3.00 GB" in text
    assert "配置备份：2.00 GB（26 份）" in text
    assert "/vol4/1000/docker/efb/comwechat/Files/shaoyou11/FileStorage/Cache" in text
    assert "可以删除" in text
    assert "谨慎删除" in text


def test_load_storage_report_reads_json_file():
    with TemporaryDirectory() as directory:
        report = Path(directory) / "report.json"
        report.write_text('{"backup_count": 3}', encoding="utf-8")

        assert load_storage_report(report)["backup_count"] == 3
