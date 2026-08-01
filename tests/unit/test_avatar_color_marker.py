import importlib.util
import unittest
from types import SimpleNamespace
from pathlib import Path


MODULE_PATH = Path(__file__).parents[2] / "efb_telegram_master" / "avatar_marker.py"
SPEC = importlib.util.spec_from_file_location("avatar_marker", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
member_name_with_avatar_marker = MODULE.member_name_with_avatar_marker


class AvatarColorMarkerTest(unittest.TestCase):
    def test_avatar_marker_enables_stable_personal_icon_before_member_name(self):
        author = SimpleNamespace(
            uid="wxid_sue",
            long_name="苏晶晶 (Sue)",
            vendor_specific={"avatar_color_marker": "🟢"},
        )
        rendered = member_name_with_avatar_marker(author)
        self.assertTrue(rendered.endswith(" 苏晶晶 (Sue)"))
        self.assertNotIn("🟢", rendered)
        self.assertEqual(member_name_with_avatar_marker(author), rendered)


    def test_missing_marker_keeps_original_member_name(self):
        author = SimpleNamespace(long_name="苏晶晶 (Sue)", vendor_specific={})
        self.assertEqual(member_name_with_avatar_marker(author), "苏晶晶 (Sue)")


if __name__ == "__main__":
    unittest.main()
