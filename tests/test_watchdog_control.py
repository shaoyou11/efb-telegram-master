from efb_telegram_master.watchdog_control import format_status


def test_format_status_shows_master_and_independent_switches():
    text = format_status({
        "master_enabled": False,
        "event_enabled": True,
        "night_enabled": False,
    })

    assert "总开关：关闭" in text
    assert "全天事件恢复：开启" in text
    assert "凌晨自主检测：关闭" in text
