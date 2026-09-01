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

    expected = {
        "Please choose the chat you want to link with...": "请选择要绑定的会话…",
        "\n\nLegend:\n": "\n\n图例：\n",
        "{0}: Linked": "{0}: 绑定",
        "{0}: User": "{0}：用户",
        "{0}: Group": "{0}：群组",
        "ComWechatChannel": "微信",
        "< Prev": "< 上一页",
        "Next >": "下一页 >",
        "You've selected chat {0}.": "你选择了会话 {0} 。",
        "\nWhat would you like to do?\n\n<i>* If the link button doesn't work for you, please try to link manually.</i>":
            "\n接下来要做什么？\n\n<i>* 如果你无法使用绑定按钮，请尝试手动绑定。</i>",
        "Link": "绑定",
        "Manual {link_or_relink}": "手动{link_or_relink}",
        "Cancel": "取消",
    }

    for source, translated in expected.items():
        assert translator.gettext(source) == translated
