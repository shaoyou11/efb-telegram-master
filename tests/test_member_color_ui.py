from efb_telegram_master.member_color_ui import panel_keyboard, panel_text


def test_panel_has_independent_toggle_and_close_buttons():
    keyboard = panel_keyboard(True).inline_keyboard
    assert keyboard[0][0].text == "关闭个性图标"
    assert keyboard[0][0].callback_data == "membercolor:set:off"
    assert keyboard[0][1].text == "关闭页面"
    assert keyboard[0][1].callback_data == "membercolor:close"


def test_panel_text_reports_persistent_cache_counts():
    text = panel_text(True, avatar_count=3, total_count=5)
    assert "已开启" in text
    assert "已记录：5 人" in text
    assert "固定的小图标" in text
