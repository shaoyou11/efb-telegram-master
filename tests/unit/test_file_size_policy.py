import importlib.util
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[2]
    / "efb_telegram_master"
    / "file_size_policy.py"
)
SPEC = importlib.util.spec_from_file_location("file_size_policy", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
exceeds_bot_api_limit = MODULE.exceeds_bot_api_limit


def test_remote_bot_api_rejects_file_over_limit():
    assert exceeds_bot_api_limit(
        file_size=21,
        limit=20,
        local_bot_api=False,
    ) is True


def test_local_bot_api_bypasses_remote_size_limit():
    assert exceeds_bot_api_limit(
        file_size=2000,
        limit=20,
        local_bot_api=True,
    ) is False
