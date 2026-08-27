import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[2] / "efb_telegram_master" / "chat_title_sync.py"
SPEC = importlib.util.spec_from_file_location("chat_title_sync", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_technical_wechat_title_is_safe_to_rename():
    assert MODULE.should_auto_rename("💻👤 notifymessage", "notifymessage")
    assert MODULE.should_auto_rename("💻👤 gh_366bf6794a09", "gh_366bf6794a09")
    assert MODULE.should_auto_rename("💻👤 wxid_demo", "wxid_demo")
    assert MODULE.should_auto_rename("💻👥 20577460305@chatroom", "20577460305@chatroom")
    assert MODULE.should_auto_rename(
        "💻👤 25984993499793938@kefu.openim",
        "25984993499793938@kefu.openim",
    )


def test_user_customized_title_is_preserved():
    assert not MODULE.should_auto_rename("我的工作群", "gh_366bf6794a09")
    assert not MODULE.should_auto_rename("岁月观收藏", "gh_366bf6794a09")


def test_plain_nontechnical_contact_id_is_not_renamed():
    assert not MODULE.should_auto_rename("💻👤 JERRYgu", "JERRYgu")


def test_extract_service_chat_name_from_historical_customer_service_message():
    assert MODULE.extract_service_chat_name("[Anker售后为你服务]") == "Anker售后"
    assert MODULE.extract_service_chat_name("普通客服消息") is None


def test_build_private_chat_title_for_historical_customer_service_name():
    assert MODULE.build_private_chat_title("💻", "Anker售后") == "💻👤 Anker售后"


def test_forced_topic_sync_is_reserved_for_name_recovery():
    assert MODULE.should_sync_topic_title("", "gh_366bf6794a09", force=True)
    assert not MODULE.should_sync_topic_title("我的工作群", "gh_366bf6794a09")
