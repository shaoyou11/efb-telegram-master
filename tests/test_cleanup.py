import json
from unittest.mock import patch

from efb_telegram_master.cleanup import build_cleanup_text, run_cleanup


def test_build_cleanup_text_explains_single_file_deletion():
    items = [{"name": "old.tmp", "category": "cache", "bytes": 2048, "token": "abc"}]

    text = build_cleanup_text(items)

    assert "逐个删除" in text
    assert "old.tmp" in text
    assert "聊天附件" in text


@patch("efb_telegram_master.cleanup.subprocess.check_output")
def test_run_cleanup_passes_only_valid_token_to_script(check_output):
    check_output.return_value = json.dumps({"name": "old.tmp"}).encode()

    token = "a" * 20
    result = run_cleanup("delete", token)

    assert result["name"] == "old.tmp"
    assert check_output.call_args.args[0][-1] == token


def test_run_cleanup_rejects_unsafe_token():
    try:
        run_cleanup("delete", "../file")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe token was accepted")
