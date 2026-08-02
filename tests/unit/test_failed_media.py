import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[2]
    / "efb_telegram_master"
    / "failed_media.py"
)
SPEC = importlib.util.spec_from_file_location("failed_media", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FailedMediaTests(unittest.TestCase):
    def test_persist_failed_media_copies_attachment_to_durable_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "failed-media"
            source = Path(directory) / "source.jpg"
            source.write_bytes(b"attachment")

            target = MODULE.persist_failed_media(source, "token-1", root)

            self.assertEqual(target.read_bytes(), b"attachment")
            self.assertEqual(target.parent, root / "token-1")
            self.assertNotEqual(target, source)

    def test_cleanup_failed_media_removes_only_persisted_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "failed-media"
            source = Path(directory) / "source.jpg"
            source.write_bytes(b"attachment")
            target = MODULE.persist_failed_media(source, "token-1", root)

            removed = MODULE.cleanup_failed_media(target, root)

            self.assertTrue(removed)
            self.assertFalse(target.exists())
            self.assertFalse((root / "token-1").exists())


if __name__ == "__main__":
    unittest.main()
