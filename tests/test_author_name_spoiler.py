from efb_telegram_master.author_name_spoiler import (
    AuthorNameSpoilerStore,
    panel_keyboard,
    panel_text,
)


def test_author_name_spoiler_defaults_off_and_persists(tmp_path):
    path = tmp_path / "author-name-spoiler.json"

    store = AuthorNameSpoilerStore(path)
    assert store.enabled is False

    store.set_enabled(True)

    reloaded = AuthorNameSpoilerStore(path)
    assert reloaded.enabled is True


def test_author_name_spoiler_panel_has_toggle_and_close_buttons():
    keyboard = panel_keyboard(False).inline_keyboard

    assert keyboard[0][0].text == "开启姓名隐藏"
    assert keyboard[0][0].callback_data == "namespoiler:set:on"
    assert keyboard[0][1].text == "关闭页面"
    assert keyboard[0][1].callback_data == "namespoiler:close"
    assert "已关闭" in panel_text(False)
