import gettext
from importlib.resources import files

from efb_telegram_master.locale_handler import normalize_locale


def test_simplified_chinese_language_tags_use_existing_catalog():
    assert normalize_locale("zh-hans") == "zh_CN"
    assert normalize_locale("zh-CN") == "zh_CN"
    assert normalize_locale("zh") == "zh_CN"


def test_traditional_chinese_language_tags_use_existing_catalog():
    assert normalize_locale("zh-hant") == "zh_TW"
    assert normalize_locale("zh-TW") == "zh_TW"


def test_non_chinese_language_tags_keep_standard_mapping():
    assert normalize_locale("en-US") == "en_US"


def test_simplified_chinese_catalog_translates_link_interface():
    translator = gettext.translation(
        "efb_telegram_master",
        str(files("efb_telegram_master").joinpath("locale")),
        languages=[normalize_locale("zh-hans"), "C"],
        fallback=True,
    )

    assert translator.gettext("You've selected chat {0}.") == "你选择了会话 {0} 。"
    assert translator.gettext("Link") == "绑定"
    assert translator.gettext("Cancel") == "取消"
