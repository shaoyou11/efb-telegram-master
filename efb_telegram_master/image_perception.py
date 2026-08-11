import json
import logging
import os
import tempfile
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler


LOGGER = logging.getLogger(__name__)


class Fingerprint(NamedTuple):
    value: str
    width: int
    height: int
    file_size: int


def image_fingerprint(path: str) -> Fingerprint:
    with Image.open(path) as image:
        width, height = image.size
        sample = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        average_rgb = image.convert("RGB").resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
        pixels = [sample.getpixel((column, row)) for row in range(8) for column in range(9)]
    bits = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            bits = (bits << 1) | int(pixels[offset + column] > pixels[offset + column + 1])
    color = "".join(f"{channel:02x}" for channel in average_rgb)
    return Fingerprint(f"{bits:016x}{color}", width, height, Path(path).stat().st_size)


def hamming_distance(first: str, second: str) -> int:
    return bin(int(first[:16], 16) ^ int(second[:16], 16)).count("1")


def fingerprints_similar(first: str, second: str, max_distance: int) -> bool:
    if len(first) < 22 or len(second) < 22:
        return False
    first_color = tuple(int(first[offset:offset + 2], 16) for offset in (16, 18, 20))
    second_color = tuple(int(second[offset:offset + 2], 16) for offset in (16, 18, 20))
    color_delta = tuple(abs(a - b) for a, b in zip(first_color, second_color))
    return (
        hamming_distance(first, second) <= max_distance
        and max(color_delta) <= 32
        and sum(color_delta) <= 72
    )


class ImagePerception:
    VERSION = 1

    def __init__(self, db, state_path: Path, max_distance: int = 6):
        self.db = db
        self.state_path = Path(state_path)
        self.max_distance = max_distance
        self.enabled = False
        self.session_hits = 0
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != self.VERSION:
                raise ValueError("unsupported image perception state")
            self.enabled = bool(data.get("enabled", False))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            LOGGER.exception("Unable to load image perception state: %s", self.state_path)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=str(self.state_path.parent),
                    prefix=f".{self.state_path.name}.", delete=False) as temp_file:
                json.dump({"version": self.VERSION, "enabled": self.enabled}, temp_file,
                          ensure_ascii=False, indent=2)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            os.replace(str(temp_path), str(self.state_path))
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()

    def find(self, path: str, media_type: str) -> Tuple[Optional[Fingerprint], Optional[str]]:
        if not self.enabled:
            return None, None
        try:
            fingerprint = image_fingerprint(path)
            for row in self.db.image_fingerprint_candidates(media_type):
                if fingerprints_similar(fingerprint.value, row.fingerprint, self.max_distance):
                    self.session_hits += 1
                    return fingerprint, row.tg_file_id
            return fingerprint, None
        except Exception:
            LOGGER.exception("Image perception lookup failed; using normal upload: %s", path)
            return None, None

    def remember(self, fingerprint: Optional[Fingerprint], media_type: str,
                 file_id: Optional[str], file_unique_id: Optional[str],
                 mime: Optional[str]) -> None:
        if not self.enabled or fingerprint is None or not file_id:
            return
        try:
            self.db.remember_image_fingerprint(
                fingerprint.value, media_type, file_id, file_unique_id, mime,
                fingerprint.width, fingerprint.height, fingerprint.file_size,
            )
        except Exception:
            LOGGER.exception("Image perception indexing failed; delivery is unaffected")

    def summary(self) -> str:
        if not self.enabled:
            return "关闭"
        try:
            count = self.db.image_fingerprint_count()
        except Exception:
            count = 0
        return f"开启（索引 {count}，本次复用 {self.session_hits}）"


def panel_text(perception: ImagePerception) -> str:
    return (
        "图片感知\n\n"
        f"状态：{perception.summary()}\n"
        "开启后，相似图片可复用 Telegram 云端文件，减少重复上传。\n"
        "仅保存感知哈希和 Telegram 文件标识，不保存额外图片；异常时自动改用正常上传。"
    )


class ImagePerceptionUI:
    def __init__(self, channel):
        self.channel = channel
        dispatcher = channel.bot_manager.dispatcher
        dispatcher.add_handler(CommandHandler(["imageperception", "image_dedupe"], self.show))
        dispatcher.add_handler(CallbackQueryHandler(self.callback, pattern=r"^imageperception:"))

    def is_admin(self, update: Update) -> bool:
        return bool(update.effective_user and update.effective_user.id in self.channel.config["admins"])

    def render(self, update: Update) -> None:
        perception = self.channel.image_perception
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "关闭图片感知" if perception.enabled else "开启图片感知",
                callback_data=f"imageperception:set:{'off' if perception.enabled else 'on'}",
            ),
            InlineKeyboardButton("关闭页面", callback_data="imageperception:close"),
        ]])
        if update.callback_query:
            update.callback_query.edit_message_text(panel_text(perception), reply_markup=markup)
        else:
            update.effective_message.reply_text(panel_text(perception), reply_markup=markup)

    def show(self, update: Update, _context: CallbackContext) -> None:
        if self.is_admin(update):
            self.render(update)

    def callback(self, update: Update, _context: CallbackContext) -> None:
        query = update.callback_query
        if not query:
            return
        if not self.is_admin(update):
            query.answer("无权执行", show_alert=True)
            return
        action = query.data or ""
        if action == "imageperception:close":
            query.answer()
            query.message.delete()
            return
        if action == "imageperception:set:on":
            self.channel.image_perception.set_enabled(True)
        elif action == "imageperception:set:off":
            self.channel.image_perception.set_enabled(False)
        else:
            query.answer("无效操作", show_alert=True)
            return
        query.answer("设置已更新")
        self.render(update)
