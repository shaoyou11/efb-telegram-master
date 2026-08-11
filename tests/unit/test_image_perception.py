from types import SimpleNamespace

import telegram
from PIL import Image

from efb_telegram_master.image_perception import (
    Fingerprint,
    ImagePerception,
    fingerprints_similar,
    hamming_distance,
    image_fingerprint,
    panel_text,
)
from efb_telegram_master.slave_message import SlaveMessageProcessor


class FakeDatabase:
    def __init__(self):
        self.rows = []
        self.remembered = []

    def image_fingerprint_candidates(self, media_type):
        return [row for row in self.rows if row.tg_media_type == media_type]

    def remember_image_fingerprint(self, *values):
        self.remembered.append(values)

    def image_fingerprint_count(self):
        return len(self.rows)


def make_image(path, color=(20, 80, 160)):
    image = Image.new("RGB", (32, 24), color)
    for offset in range(8):
        image.putpixel((offset, offset), (255, 255, 255))
    image.save(path)


def test_default_is_disabled_and_state_is_persistent(tmp_path):
    state_path = tmp_path / "image-perception.json"
    perception = ImagePerception(FakeDatabase(), state_path)

    assert perception.enabled is False
    assert perception.find(str(tmp_path / "missing.jpg"), "photo") == (None, None)

    perception.set_enabled(True)
    assert ImagePerception(FakeDatabase(), state_path).enabled is True


def test_similar_image_reuses_matching_media_type(tmp_path):
    image_path = tmp_path / "sample.jpg"
    make_image(image_path)
    fingerprint = image_fingerprint(str(image_path))
    db = FakeDatabase()
    db.rows = [SimpleNamespace(
        fingerprint=fingerprint.value,
        tg_media_type="photo",
        tg_file_id="telegram-file-id",
    )]
    perception = ImagePerception(db, tmp_path / "state.json")
    perception.set_enabled(True)

    prepared, file_id = perception.find(str(image_path), "photo")

    assert prepared == fingerprint
    assert file_id == "telegram-file-id"
    assert perception.session_hits == 1
    assert perception.find(str(image_path), "document")[1] is None


def test_index_error_never_escapes(tmp_path):
    image_path = tmp_path / "sample.png"
    make_image(image_path)

    class BrokenDatabase(FakeDatabase):
        def remember_image_fingerprint(self, *values):
            raise RuntimeError("database unavailable")

    perception = ImagePerception(BrokenDatabase(), tmp_path / "state.json")
    perception.set_enabled(True)
    fingerprint, _ = perception.find(str(image_path), "photo")

    perception.remember(fingerprint, "photo", "file-id", "unique-id", "image/png")


def test_hash_distance_and_panel_do_not_expose_paths(tmp_path):
    db = FakeDatabase()
    perception = ImagePerception(db, tmp_path / "state.json")
    assert hamming_distance("0000000000000000", "0000000000000001") == 1
    assert "状态：关闭" in panel_text(perception)
    assert str(tmp_path) not in panel_text(perception)


def test_uniform_images_with_different_colors_are_not_similar(tmp_path):
    red_path = tmp_path / "red.png"
    green_path = tmp_path / "green.png"
    Image.new("RGB", (32, 24), (220, 20, 20)).save(red_path)
    Image.new("RGB", (32, 24), (20, 220, 20)).save(green_path)

    red = image_fingerprint(str(red_path))
    green = image_fingerprint(str(green_path))

    assert hamming_distance(red.value, green.value) == 0
    assert not fingerprints_similar(red.value, green.value, 6)


def test_database_index_roundtrip(channel):
    channel.db.remember_image_fingerprint(
        "1234567890abcdef", "photo", "file-id", "unique-id",
        "image/jpeg", 32, 24, 1024,
    )
    channel.db._wait_for_write_queue()

    rows = channel.db.image_fingerprint_candidates("photo")

    assert any(row.fingerprint == "1234567890abcdef" for row in rows)
    assert channel.db.image_fingerprint_count() >= 1


def test_cached_file_failure_retries_original_image(tmp_path):
    image_path = tmp_path / "sample.png"
    make_image(image_path)
    calls = []
    remembered = []

    class FakeBot:
        def send_chat_action(self, *args, **kwargs):
            pass

        def send_photo(self, _destination, source, **kwargs):
            calls.append(source)
            if source == "stale-file-id":
                raise telegram.error.BadRequest("stale file id")
            return SimpleNamespace(
                photo=[SimpleNamespace(file_id="fresh-file-id", file_unique_id="fresh-unique-id")],
                document=None,
            )

        def send_document(self, *args, **kwargs):
            raise AssertionError("valid source photo must not fall back to document")

    perception = SimpleNamespace(
        find=lambda path, media_type: (
            Fingerprint("0" * 16, 32, 24, image_path.stat().st_size),
            "stale-file-id",
        ),
        remember=lambda *values: remembered.append(values),
    )
    processor = SlaveMessageProcessor.__new__(SlaveMessageProcessor)
    processor.bot = FakeBot()
    processor.channel = SimpleNamespace(image_perception=perception)
    processor.logger = SimpleNamespace(warning=lambda *args: None, error=lambda *args: None,
                                       debug=lambda *args: None)
    processor.flag = lambda _name: "emoji"
    processor.format_text_message_template = lambda *args: ""
    processor.check_file_size = lambda _file: False
    processor.process_file_obj = lambda file_obj, _path: file_obj
    processor.build_reply_target_kwargs = lambda *args: {}
    message = SimpleNamespace(
        file=image_path.open("rb"), path=str(image_path), text="", uid="image-1",
        type="image", mime="image/png", edit_media=False, filename="sample.png",
        author=None, vendor_specific={},
    )

    result = processor.slave_message_image(message, 1, None, "", "")

    assert result.photo[-1].file_id == "fresh-file-id"
    assert calls[0] == "stale-file-id"
    assert hasattr(calls[1], "read")
    assert remembered[-1][2] == "fresh-file-id"
