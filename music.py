# music.py
# Telegram Music Bot (Aiogram 3.x)

import asyncio
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from html import escape
from io import BytesIO
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramConflictError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from imageio_ffmpeg import get_ffmpeg_exe
from mutagen import File as MutagenFile
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TIT2, TPE1
from mutagen.mp3 import MP3
from PIL import Image, ImageDraw
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

TOKEN = "8307283859:AAFuR9_rKnAUnnbJseTTUb3oAKmIXu8dVxc"
ADMIN_ID = 5372929619

bot = Bot(TOKEN)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)
logging.disable(logging.CRITICAL)

DOWNLOADS = "downloads"
TEMP_DIR = os.path.join(DOWNLOADS, "temp")
DEFAULT_COVER_PATH = os.path.join(TEMP_DIR, "default_cover.jpg")
INSTAGRAM_COOKIES_FILE = os.path.join(os.getcwd(), "instagram_cookies.txt")
ADMIN_DATA_FILE = os.path.join(os.getcwd(), "admin_data.json")
USERS_DB_FILE = os.path.join(os.getcwd(), "users.db")
MAX_AUDIO_SIZE_MB = 20
MAX_AUDIO_SIZE_BYTES = MAX_AUDIO_SIZE_MB * 1024 * 1024
MAX_VIDEO_SIZE_MB = 49
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
SUPPORTED_VIDEO_HOSTS = (
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
)
os.makedirs(DOWNLOADS, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

FFMPEG_EXE = get_ffmpeg_exe()
BOT_LINK_CACHE: Optional[str] = None
SOCIAL_RESULT_CAPTION = "@MusicTagUzBot"
SOCIAL_RESULT_BUTTON_TEXT = "Скачать песню"
STARTED_AT = time.time()


@dataclass
class TrackSession:
    track_path: str
    original_file_name: str
    title: str
    performer: str
    duration_seconds: int
    size_mb: float
    user_id: int
    chat_id: int
    card_message_id: int
    cover_path: Optional[str] = None
    cover_source: str = "Не добавлено"
    bass_level: int = 0
    effect_8d: int = 0
    speed: float = 1.0
    bitrate: int = 192
    trim_start_ms: Optional[int] = None
    trim_end_ms: Optional[int] = None
    voice_mode: bool = False
    caption: str = ""
    pending_action: Optional[str] = None
    prompt_message_id: Optional[int] = None
    prompt_chat_id: Optional[int] = None


@dataclass
class QuickTagSettings:
    cover_path: Optional[str] = None
    title_mode: str = "keep"
    title_value: str = ""
    artist_mode: str = "keep"
    artist_value: str = ""
    bitrate: Optional[int] = None
    pending_action: Optional[str] = None


@dataclass
class VideoSession:
    video_path: str
    original_file_name: str
    duration_seconds: int
    size_mb: float
    user_id: int
    chat_id: int
    card_message_id: int
    pending_action: Optional[str] = None
    trim_start_ms: Optional[int] = None
    trim_end_ms: Optional[int] = None
    prompt_message_id: Optional[int] = None
    prompt_chat_id: Optional[int] = None


user_sessions: dict[int, TrackSession] = {}
video_sessions: dict[int, VideoSession] = {}
quick_tag_settings: dict[int, QuickTagSettings] = {}
user_languages: dict[int, str] = {}
user_social_captions: dict[int, str] = {}
admin_reply_keyboard_seeded: set[int] = set()
bot_stats = {
    "audio_edits": 0,
    "social_downloads": 0,
    "video_circles": 0,
    "circle_to_video": 0,
    "video_mp3": 0,
    "video_trims": 0,
}


def load_admin_data() -> dict:
    if not os.path.exists(ADMIN_DATA_FILE):
        return {"users": {}}
    try:
        with open(ADMIN_DATA_FILE, "r", encoding="utf-8") as source:
            data = json.load(source)
    except Exception:
        return {"users": {}}
    if not isinstance(data, dict):
        return {"users": {}}
    users = data.get("users")
    if not isinstance(users, dict):
        data["users"] = {}
    return data


def get_db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(USERS_DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def init_user_database() -> None:
    with get_db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                last_seen INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.commit()


def migrate_admin_data_to_db() -> None:
    legacy_data = load_admin_data()
    legacy_users = legacy_data.get("users", {})
    if not isinstance(legacy_users, dict) or not legacy_users:
        return
    with get_db_connection() as connection:
        for raw_user_id, info in legacy_users.items():
            if not isinstance(info, dict):
                continue
            try:
                user_id = int(raw_user_id)
            except Exception:
                continue
            last_seen = int(info.get("last_seen") or time.time())
            connection.execute(
                """
                INSERT INTO users (user_id, first_name, last_name, username, last_seen, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    username = excluded.username,
                    last_seen = MAX(users.last_seen, excluded.last_seen)
                """,
                (
                    user_id,
                    info.get("first_name", "") or "",
                    info.get("last_name", "") or "",
                    info.get("username", "") or "",
                    last_seen,
                    last_seen,
                ),
            )
        connection.commit()


def load_known_users() -> dict[int, dict]:
    with get_db_connection() as connection:
        rows = connection.execute(
            """
            SELECT user_id, first_name, last_name, username, last_seen, created_at
            FROM users
            """
        ).fetchall()
    return {
        int(row["user_id"]): {
            "first_name": row["first_name"] or "",
            "last_name": row["last_name"] or "",
            "username": row["username"] or "",
            "last_seen": int(row["last_seen"] or 0),
            "created_at": int(row["created_at"] or 0),
        }
        for row in rows
    }


init_user_database()
migrate_admin_data_to_db()
known_users: dict[int, dict] = load_known_users()


def save_admin_data() -> None:
    data = {"users": {str(user_id): info for user_id, info in sorted(known_users.items())}}
    with open(ADMIN_DATA_FILE, "w", encoding="utf-8") as target:
        json.dump(data, target, ensure_ascii=False, indent=2)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def register_user(user) -> None:
    if not user:
        return
    user_id = getattr(user, "id", None)
    if not isinstance(user_id, int):
        return
    now = int(time.time())
    user_info = {
        "first_name": getattr(user, "first_name", "") or "",
        "last_name": getattr(user, "last_name", "") or "",
        "username": getattr(user, "username", "") or "",
        "last_seen": now,
        "created_at": known_users.get(user_id, {}).get("created_at", now),
    }
    with get_db_connection() as connection:
        connection.execute(
            """
            INSERT INTO users (user_id, first_name, last_name, username, last_seen, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                username = excluded.username,
                last_seen = excluded.last_seen
            """,
            (
                user_id,
                user_info["first_name"],
                user_info["last_name"],
                user_info["username"],
                user_info["last_seen"],
                user_info["created_at"],
            ),
        )
        connection.commit()
    known_users[user_id] = user_info
    save_admin_data()


def format_uptime() -> str:
    total = int(time.time() - STARTED_AT)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class InstagramAuthRequiredError(RuntimeError):
    pass


class SilentYtDlpLogger:
    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass

TRANSLATIONS = {
    "ru": {
        "start_text": """🙋 Привет! Я ваш редактор музыки и видео.

🎶 Музыка:
» Я редактирую треки, метаданные и обложки
» Я добавляю бас, эффекты и обрезаю
» Я распознаю и нахожу песни

↻ Скачивание:
» Видео из Instagram и TikTok

▢ Видео:
» Я обрезаю и создаю кружки (видеосообщения)
» Я нахожу музыку из видео

🚀 Чтобы начать — отправьте аудио, видео, ссылку или название трека""",
        "quick_tags_intro": """🎛 <b>Теги по умолчанию</b>
⚙️ Вы можете один раз задать теги по умолчанию. После этого при загрузке музыки сможете применять их все одной кнопкой.

📄 При нажатии «⚙️ Теги по умолчанию» указанные данные будут автоматически подставляться в новые треки.

Если не хотите добавлять тег — оставьте «❌ Не добавлено».

«⚪ Пробел» означает пустое значение""",
        "how_to_use_text": """<b>Как пользоваться ботом !❓</b>

┏ 📝 Редактирование тегов музыки
┠ 📌 Гайд по тегам по умолчанию
┠ 🖼 Изменение обложки музыки
┠ 🎙 Конвертация музыки в голосовое сообщение
┠ ✂️ Обрезка музыки
┠ 💡 Другие возможности бота
┗ 📼 Скачивание из Instagram и TikTok""",
        "language_text": "🌐 Выберите язык интерфейса:",
        "weekly_hits_text": "❌ Топ-чарт временно недоступен. Пожалуйста, попробуйте позже.",
        "search_text": """📎 <b>Поиск музыки</b>
Просто введите название трека или имя исполнителя — бот сразу покажет варианты.
Вы также можете отправить ссылку на Instagram или TikTok.

💡 Инлайн-поиск
Нажмите кнопку ниже и введите запрос прямо в поле ввода —
вы сразу увидите результаты. Выберите нужный трек, и бот отправит музыку.""",
        "tags_prompt": """<b>❞ Отправьте новые теги в формате:</b>

<blockquote>✎ Название трека
🄯 Исполнитель</blockquote>

<b>Пример:</b>
<code>shivers
Ed Sheeran</code>

📎 Если отправите две строки — изменятся и название, и исполнитель.
Если одну строку — изменится только название трека.""",
        "photo_prompt": "Отправьте новую обложку 🖼",
        "quick_photo_text": """🖼 <b>Фото — автосохранение</b>
Отправьте одно изображение одним сообщением. Вы можете удалить текущее фото.""",
        "quick_title_text": """🎵 <b>Название трека — автосохранение</b>
«⚪ Пробел» — пустое название. «❌ Не изменять» — оставить тег без изменений.""",
        "quick_artist_text": """👤 <b>Исполнитель — автосохранение</b>
«⚪ Пробел» — пустое значение. «❌ Не изменять» — оставить тег без изменений.""",
        "quick_bitrate_text": """🎚 <b>Битрейт — автосохранение</b>
Выберите готовое значение. «❌ Не изменять» — оставить битрейт трека без изменений.""",
        "trim_prompt": """<b>Отправьте время начала и конца в формате:</b>

<blockquote>мм:сс - мм:сс</blockquote>

<b>Пример:</b>
<blockquote>00:20 - 02:30</blockquote>""",
        "caption_prompt": """📝 <b>Отправьте вашу подпись</b>

Этот текст будет прикреплён при отправке вашей музыки.

Вы можете использовать форматирование Telegram:
— Жирный
— Курсив
— Подчёркнутый
— Зачёркнутый
— Код
— Ссылка
— Цитата""",
        "guide_tags": "Раздел по редактированию тегов скоро добавлю.",
        "guide_quick_tags": "Гайд по тегам по умолчанию скоро добавлю.",
        "guide_cover": "Инструкция по изменению обложки скоро будет.",
        "guide_voice": "Инструкция по конвертации в голосовое скоро будет.",
        "guide_cut": "Инструкция по обрезке музыки скоро будет.",
        "guide_other": "Раздел с другими возможностями скоро будет.",
        "guide_search": "Гайд по скачиванию из Instagram и TikTok скоро будет.",
        "not_added": "Не добавлено",
        "added": "Добавлено",
        "space": "Пробел",
        "keep": "Не изменять",
        "default_cover_source": "По умолчанию",
        "music_cover_source": "Из музыки",
        "search_cover_source": "Из поиска по треку",
        "title": "Название",
        "artist": "Исполнитель",
        "size": "Размер",
        "duration": "Длительность",
        "effect_8d": "8D эффект",
        "bass": "Басс",
        "speed": "Скорость",
        "bitrate": "Битрейт",
        "trim": "Обрезка",
        "photo": "Фото",
        "voice": "В голосовое",
        "yes": "Да",
        "start_quick_tags": "⚙️ Быстрые теги",
        "how_to_use": "❓ Как пользоваться ботом",
        "change_language": "🌐 Изменить язык",
        "saved_items": "📂 Сохранённые",
        "weekly_hits": "🏆 Хиты этой недели",
        "search_music": "🔎 Поиск музыки",
        "main_menu": "🏠 Главное меню",
        "close": "❌ Закрыть",
        "back": "« Назад",
        "inline_search": "🔎 Инлайн-поиск",
        "cancel": "✖ Отмена",
        "no_caption": "❎ Без подписи",
        "edit_tags": "📝 Изменить теги",
        "edit_photo": "🖼 Изменить фото",
        "shazam": "🎧 Shazam",
        "effect_8d_btn": "🌪 8D эффект",
        "bass_btn": "🔉 Басс",
        "speed_btn": "⚡ Скорость",
        "bitrate_btn": "🎚 Битрейт",
        "cut_btn": "✂️ Обрезать",
        "voice_btn": "🎤 В голосовое",
        "caption_btn": "🏷 Подпись",
        "save_btn": "💾 Сохранить",
        "quick_photo_btn": "🖼 Фото: {status}",
        "quick_title_btn": "🎵 Название трека: {status}",
        "quick_artist_btn": "👤 Имя исполнителя: {status}",
        "quick_bitrate_btn": "🎚 Битрейт: {status}",
        "upload_or_change": "📸 Загрузить / Изменить",
        "delete_photo": "🗑 Удалить фото",
        "add_or_change": "✏️ Добавить / Изменить",
        "space_btn": "⚪ Пробел",
        "keep_btn": "❌ Не изменять",
        "saved_tracks_title": "📂 <b>Сохранённые треки</b>\n\n",
        "no_saved_tracks": "У вас пока нет сохранённых треков ❎",
        "file_too_large": "❌ Файл слишком большой: {size_mb:.2f} MB.\nМожно отправлять музыку только до {max_mb} MB.",
        "unknown_title": "Неизвестно",
        "send_at_least_title": "Введите хотя бы название трека.",
        "invalid_trim_format": "Неверный формат. Пример: 00:20 - 02:30",
        "send_one_photo_alert": "Отправьте одно фото одним сообщением.",
        "send_title_alert": "Отправьте название трека одним сообщением.",
        "send_artist_alert": "Отправьте имя исполнителя одним сообщением.",
        "russian_already_selected": "Русский язык уже выбран.",
        "language_changed_en": "Language changed to English.",
        "language_changed_uz": "Til o'zbek tiliga o'zgartirildi.",
        "language_changed_ru": "Язык изменён на русский.",
        "send_music_first": "Сначала отправьте музыку.",
        "shazam_cover_not_found": "Не удалось найти обложку по этому треку.",
        "action_cancelled": "Действие отменено.",
        "unknown_command": "❌ Неизвестная команда",
        "choose_bass": "🎧 Выберите уровень баса",
        "choose_8d": "🎧 Нажмите кнопку, чтобы изменить 8D эффект.",
        "choose_speed": "▶ Выберите скорость воспроизведения.",
        "choose_bitrate": "Выберите битрейт (кбит/с):",
        "video_download_started": "⏬ Скачиваю видео по ссылке...",
        "video_download_failed": "❌ Не удалось скачать видео по этой ссылке. Проверьте ссылку и попробуйте ещё раз.",
        "tiktok_short_link_failed": "❌ Короткая ссылка TikTok сейчас не открылась.\nОтправьте полную ссылку вида `https://www.tiktok.com/...` и попробуйте снова.",
        "instagram_auth_required": "❌ Instagram не отдал это видео без входа в аккаунт.\nЕсли ссылка не скачивается, отправьте в бот файл `instagram_cookies.txt` и попробуйте снова.",
        "instagram_cookies_saved": "✅ Файл `instagram_cookies.txt` сохранён. Теперь отправьте ссылку ещё раз.",
        "instagram_cookies_invalid": "❌ Отправьте файл именно с именем `instagram_cookies.txt`.",
        "video_too_large": "❌ Видео получилось слишком большим: {size_mb:.2f} MB.\nЯ могу отправлять только файлы до {max_mb} MB.",
        "circle_prompt": "📹 Отправьте обычное видео, и я сделаю из него кружочек.",
        "circle_started": "🔄 Делаю кружочек из видео...",
        "circle_failed": "❌ Не удалось сделать кружочек из этого видео. Попробуйте другое видео.",
        "uncircle_prompt": "🎥 Отправьте кружочек, и я превращу его в обычное видео.",
        "uncircle_started": "🔄 Превращаю кружочек в обычное видео...",
        "uncircle_failed": "❌ Не удалось превратить кружочек в обычное видео. Попробуйте ещё раз.",
        "video_editor_title": "🎬 <b>Редактор видео</b>",
        "video_circle_btn": "⭕ Кружочек",
        "video_mp3_btn": "🎵 Видео -> MP3",
        "video_trim_btn": "✂️ Обрезать видео",
        "video_send_btn": "📤 Отправить видео",
        "video_caption_btn": "🏷 Подпись соцвидео",
        "video_trim_prompt": "Отправьте время для видео в формате `мм:сс - мм:сс`",
        "video_caption_saved": "✅ Новая подпись для скачанных соцвидео сохранена.",
        "video_caption_current": "Текущая подпись: {caption}",
        "video_mp3_started": "🔄 Достаю музыку из видео...",
        "video_mp3_failed": "❌ Не удалось достать музыку из этого видео.",
        "video_trim_started": "🔄 Обрезаю видео...",
        "video_trim_failed": "❌ Не удалось обрезать это видео.",
        "video_ready": "✅ Готово.",
        "stats_text": "<b>Статистика бота</b>\n\n🎵 Музыка: {audio_edits}\n📥 Соцвидео: {social_downloads}\n⭕ Кружочки: {video_circles}\n🎥 Из кружочка в видео: {circle_to_video}\n🎶 Видео -> MP3: {video_mp3}\n✂️ Обрезка видео: {video_trims}",
        "admin_only": "⛔ Эта команда доступна только администратору.",
        "admin_panel_title": "<b>Админ-панель</b>\n\n👤 Пользователей: {user_count}\n⏱ Аптайм: {uptime}",
        "admin_stats_btn": "📊 Статистика",
        "admin_users_btn": "👥 Пользователи",
        "admin_files_btn": "🗂 Файлы",
        "admin_back_btn": "« Назад",
        "admin_reply_btn": "🛠 Админ-панель",
        "admin_reply_ready": "⬇️ Кнопка админ-панели закреплена снизу.",
        "admin_users_text": "<b>Пользователи бота</b>\n\nВсего: {user_count}\n\n{users}",
        "admin_files_text": "<b>Файлы бота</b>\n\n📁 Всего файлов: {file_count}\n💾 Общий размер: {total_size_mb:.2f} MB\n🗂 В downloads: {downloads_count}\n🧪 В temp: {temp_count}",
        "lang_ru": "Русский",
        "lang_en": "English",
        "lang_uz": "O'zbek",
    },
    "en": {
        "start_text": """🙋 Hi! I'm your music and video editor.

🎶 Music:
» I edit tracks, metadata, and covers
» I add bass, effects, and trimming
» I recognize and find songs

↻ Downloading:
» Video from Instagram and TikTok

▢ Video:
» I trim and create video circles
» I find music from videos

🚀 To start, send audio, video, a link, or a track name""",
        "quick_tags_intro": """🎛 <b>Default tags</b>
⚙️ You can set default tags once. After that, they can be applied to new tracks automatically.

📄 When you tap “⚙️ Default tags”, these values will be inserted into new tracks automatically.

If you don't want to add a tag, leave it as “❌ Not added”.

“⚪ Space” means an empty value""",
        "how_to_use_text": """<b>How to use the bot ❓</b>

┏ 📝 Editing music tags
┠ 📌 Guide to default tags
┠ 🖼 Changing music cover
┠ 🎙 Converting music to voice message
┠ ✂️ Trimming music
┠ 💡 Other bot features
┗ 📼 Downloading from Instagram and TikTok""",
        "language_text": "🌐 Choose interface language:",
        "weekly_hits_text": "❌ Weekly chart is temporarily unavailable. Please try again later.",
        "search_text": """📎 <b>Music search</b>
Just enter a track name or artist name and the bot will show options right away.
You can also send an Instagram or TikTok link.

💡 Inline search
Tap the button below and enter a query directly in the input field —
you will immediately see the results. Choose a track and the bot will send the music.""",
        "tags_prompt": """<b>❞ Send new tags in this format:</b>

<blockquote>✎ Track title
🄯 Artist</blockquote>

<b>Example:</b>
<code>shivers
Ed Sheeran</code>

📎 If you send two lines, both title and artist will change.
If you send one line, only the title will change.""",
        "photo_prompt": "Send a new cover 🖼",
        "quick_photo_text": """🖼 <b>Photo — auto save</b>
Send one image in a single message. You can delete the current photo.""",
        "quick_title_text": """🎵 <b>Track title — auto save</b>
“⚪ Space” means an empty title. “❌ Don't change” keeps the tag unchanged.""",
        "quick_artist_text": """👤 <b>Artist — auto save</b>
“⚪ Space” means an empty value. “❌ Don't change” keeps the tag unchanged.""",
        "quick_bitrate_text": """🎚 <b>Bitrate — auto save</b>
Choose a ready value. “❌ Don't change” keeps the track bitrate unchanged.""",
        "trim_prompt": """<b>Send start and end time in this format:</b>

<blockquote>mm:ss - mm:ss</blockquote>

<b>Example:</b>
<blockquote>00:20 - 02:30</blockquote>""",
        "caption_prompt": """📝 <b>Send your caption</b>

This text will be attached when sending your music.

You can use Telegram formatting:
— Bold
— Italic
— Underline
— Strikethrough
— Code
— Link
— Quote""",
        "guide_tags": "The music tag editing section will be added soon.",
        "guide_quick_tags": "The default tags guide will be added soon.",
        "guide_cover": "The cover editing guide will be added soon.",
        "guide_voice": "The voice conversion guide will be added soon.",
        "guide_cut": "The trimming guide will be added soon.",
        "guide_other": "The other features section will be added soon.",
        "guide_search": "The Instagram and TikTok download guide will be added soon.",
        "not_added": "Not added", "added": "Added", "space": "Space", "keep": "Don't change",
        "default_cover_source": "Default", "music_cover_source": "From music", "search_cover_source": "From track search",
        "title": "Title", "artist": "Artist", "size": "Size", "duration": "Duration", "effect_8d": "8D effect", "bass": "Bass", "speed": "Speed", "bitrate": "Bitrate", "trim": "Trim", "photo": "Photo", "voice": "Voice mode", "yes": "Yes",
        "start_quick_tags": "⚙️ Default tags", "how_to_use": "❓ How to use", "change_language": "🌐 Change language", "saved_items": "📂 Saved", "weekly_hits": "🏆 Weekly hits", "search_music": "🔎 Search music", "main_menu": "🏠 Main menu",
        "close": "❌ Close", "back": "« Back", "inline_search": "🔎 Inline search", "cancel": "✖ Cancel", "no_caption": "❎ No caption",
        "edit_tags": "📝 Edit tags", "edit_photo": "🖼 Change photo", "shazam": "🎧 Shazam", "effect_8d_btn": "🌪 8D effect", "bass_btn": "🔉 Bass", "speed_btn": "⚡ Speed", "bitrate_btn": "🎚 Bitrate", "cut_btn": "✂️ Trim", "voice_btn": "🎤 To voice", "caption_btn": "🏷 Caption", "save_btn": "💾 Save",
        "quick_photo_btn": "🖼 Photo: {status}", "quick_title_btn": "🎵 Track title: {status}", "quick_artist_btn": "👤 Artist name: {status}", "quick_bitrate_btn": "🎚 Bitrate: {status}",
        "upload_or_change": "📸 Upload / Change", "delete_photo": "🗑 Delete photo", "add_or_change": "✏️ Add / Change", "space_btn": "⚪ Space", "keep_btn": "❌ Don't change",
        "saved_tracks_title": "📂 <b>Saved tracks</b>\n\n", "no_saved_tracks": "You don't have any saved tracks yet ❎",
        "file_too_large": "❌ File is too large: {size_mb:.2f} MB.\nYou can send music only up to {max_mb} MB.",
        "unknown_title": "Unknown", "send_at_least_title": "Enter at least the track title.", "invalid_trim_format": "Invalid format. Example: 00:20 - 02:30",
        "send_one_photo_alert": "Send one photo in a single message.", "send_title_alert": "Send the track title in one message.", "send_artist_alert": "Send the artist name in one message.",
        "russian_already_selected": "Russian is already selected.", "language_changed_en": "Language changed to English.", "language_changed_uz": "Language changed to Uzbek.", "language_changed_ru": "Language changed to Russian.",
        "send_music_first": "Send music first.", "shazam_cover_not_found": "Couldn't find a cover for this track.", "action_cancelled": "Action cancelled.", "unknown_command": "❌ Unknown command",
        "choose_bass": "🎧 Choose bass level", "choose_8d": "🎧 Tap a button to change the 8D effect.", "choose_speed": "▶ Choose playback speed.", "choose_bitrate": "Choose bitrate (kbps):",
        "video_download_started": "⏬ Downloading video from the link...",
        "video_download_failed": "❌ Couldn't download the video from this link.",
        "tiktok_short_link_failed": "❌ The short TikTok link couldn't be opened right now.\nSend the full `https://www.tiktok.com/...` link and try again.",
        "instagram_auth_required": "❌ Instagram didn't return this video without login.\nIf this reel doesn't download, send an `instagram_cookies.txt` file to the bot and try again.",
        "instagram_cookies_saved": "✅ The `instagram_cookies.txt` file was saved. Now send the link again.",
        "instagram_cookies_invalid": "❌ Please send a file named exactly `instagram_cookies.txt`.",
        "video_too_large": "❌ The downloaded video is too large: {size_mb:.2f} MB.\nI can send files only up to {max_mb} MB.",
        "circle_prompt": "📹 Send a regular video and I will turn it into a video circle.",
        "circle_started": "🔄 Turning the video into a circle...",
        "circle_failed": "❌ Couldn't make a video circle from this video. Try another one.",
        "uncircle_prompt": "🎥 Send a video circle and I will turn it into a regular video.",
        "uncircle_started": "🔄 Turning the video circle into a regular video...",
        "uncircle_failed": "❌ Couldn't turn this video circle into a regular video. Try again.",
        "video_editor_title": "🎬 <b>Video editor</b>",
        "video_circle_btn": "⭕ Circle",
        "video_mp3_btn": "🎵 Video -> MP3",
        "video_trim_btn": "✂️ Trim video",
        "video_send_btn": "📤 Send video",
        "video_caption_btn": "🏷 Social video caption",
        "video_trim_prompt": "Send video timing in the format `mm:ss - mm:ss`",
        "video_caption_saved": "✅ New caption for downloaded social videos was saved.",
        "video_caption_current": "Current caption: {caption}",
        "video_mp3_started": "🔄 Extracting music from the video...",
        "video_mp3_failed": "❌ Couldn't extract music from this video.",
        "video_trim_started": "🔄 Trimming the video...",
        "video_trim_failed": "❌ Couldn't trim this video.",
        "video_ready": "✅ Done.",
        "stats_text": "<b>Bot stats</b>\n\n🎵 Music: {audio_edits}\n📥 Social videos: {social_downloads}\n⭕ Video circles: {video_circles}\n🎥 Circle to video: {circle_to_video}\n🎶 Video -> MP3: {video_mp3}\n✂️ Video trims: {video_trims}",
        "admin_only": "⛔ This command is available only to the administrator.",
        "admin_panel_title": "<b>Admin panel</b>\n\n👤 Users: {user_count}\n⏱ Uptime: {uptime}",
        "admin_stats_btn": "📊 Stats",
        "admin_users_btn": "👥 Users",
        "admin_files_btn": "🗂 Files",
        "admin_back_btn": "« Back",
        "admin_reply_btn": "🛠 Admin Panel",
        "admin_reply_ready": "⬇️ The admin panel button is pinned below.",
        "admin_users_text": "<b>Bot users</b>\n\nTotal: {user_count}\n\n{users}",
        "admin_files_text": "<b>Bot files</b>\n\n📁 Total files: {file_count}\n💾 Total size: {total_size_mb:.2f} MB\n🗂 In downloads: {downloads_count}\n🧪 In temp: {temp_count}",
        "lang_ru": "Russian", "lang_en": "English", "lang_uz": "Uzbek",
    },
    "uz": {
        "start_text": """🙋 Salom! Men sizning musiqa va video tahrirchingizman.

🎶 Musiqa:
» Treklar, metadata va coverlarni tahrirlayman
» Bass, effekt va kesishni qo'shaman
» Qo'shiqlarni taniyman va topaman

↻ Yuklab olish:
» Instagram va TikTok'dan video

▢ Video:
» Kesaman va video doiracha tayyorlayman
» Videodan musiqani topaman

🚀 Boshlash uchun audio, video, havola yoki trek nomini yuboring""",
        "quick_tags_intro": """🎛 <b>Standart teglar</b>
⚙️ Siz bir marta standart teglarni sozlashingiz mumkin. Keyin ular yangi treklarga avtomatik qo'llanadi.

📄 “⚙️ Standart teglar” bosilganda ko'rsatilgan qiymatlar yangi treklarga avtomatik qo'yiladi.

Agar teg qo'shmoqchi bo'lmasangiz, “❌ Qo'shilmagan” holatda qoldiring.

“⚪ Bo'sh joy” bo'sh qiymatni bildiradi""",
        "how_to_use_text": """<b>Botdan qanday foydalanish ❓</b>

┏ 📝 Musiqa teglarini tahrirlash
┠ 📌 Standart teglar bo'yicha qo'llanma
┠ 🖼 Musiqa coverini o'zgartirish
┠ 🎙 Musiqani voice xabarga aylantirish
┠ ✂️ Musiqani kesish
┠ 💡 Boshqa imkoniyatlar
┗ 📼 Instagram va TikTok'dan yuklab olish""",
        "language_text": "🌐 Interfeys tilini tanlang:",
        "weekly_hits_text": "❌ Haftalik chart vaqtincha mavjud emas. Keyinroq urinib ko'ring.",
        "search_text": """📎 <b>Musiqa qidirish</b>
Trek nomi yoki ijrochi nomini kiriting, bot darhol variantlarni ko'rsatadi.
Shuningdek Instagram yoki TikTok havolasini yuborishingiz mumkin.

💡 Inline qidiruv
Quyidagi tugmani bosing va so'rovni to'g'ridan-to'g'ri kiritish maydoniga yozing —
natijalar darhol chiqadi. Trekni tanlang va bot musiqani yuboradi.""",
        "tags_prompt": """<b>❞ Yangi teglarni shu formatda yuboring:</b>

<blockquote>✎ Trek nomi
🄯 Ijrochi</blockquote>

<b>Misol:</b>
<code>shivers
Ed Sheeran</code>

📎 Ikki qator yuborsangiz, nom va ijrochi o'zgaradi.
Bitta qator yuborsangiz, faqat trek nomi o'zgaradi.""",
        "photo_prompt": "Yangi cover yuboring 🖼",
        "quick_photo_text": """🖼 <b>Foto — avto saqlash</b>
Bitta rasmni bitta xabar bilan yuboring. Joriy fotoni o'chirishingiz mumkin.""",
        "quick_title_text": """🎵 <b>Trek nomi — avto saqlash</b>
“⚪ Bo'sh joy” — bo'sh nom. “❌ O'zgartirmaslik” tegni o'zgartirmaydi.""",
        "quick_artist_text": """👤 <b>Ijrochi — avto saqlash</b>
“⚪ Bo'sh joy” — bo'sh qiymat. “❌ O'zgartirmaslik” tegni o'zgartirmaydi.""",
        "quick_bitrate_text": """🎚 <b>Bitrate — avto saqlash</b>
Tayyor qiymatni tanlang. “❌ O'zgartirmaslik” trek bitrate'ini o'zgartirmaydi.""",
        "trim_prompt": """<b>Boshlanish va tugash vaqtini shu formatda yuboring:</b>

<blockquote>mm:ss - mm:ss</blockquote>

<b>Misol:</b>
<blockquote>00:20 - 02:30</blockquote>""",
        "caption_prompt": """📝 <b>Caption yuboring</b>

Bu matn musiqani yuborishda birga yuboriladi.

Telegram formatlashidan foydalanishingiz mumkin:
— Qalin
— Kursiv
— Tagi chizilgan
— Usti chizilgan
— Kod
— Havola
— Iqtibos""",
        "guide_tags": "Musiqa teglarini tahrirlash bo'limi tez orada qo'shiladi.",
        "guide_quick_tags": "Standart teglar qo'llanmasi tez orada qo'shiladi.",
        "guide_cover": "Coverni o'zgartirish qo'llanmasi tez orada qo'shiladi.",
        "guide_voice": "Voice'ga aylantirish qo'llanmasi tez orada qo'shiladi.",
        "guide_cut": "Kesish qo'llanmasi tez orada qo'shiladi.",
        "guide_other": "Boshqa imkoniyatlar bo'limi tez orada qo'shiladi.",
        "guide_search": "Instagram va TikTok'dan yuklab olish qo'llanmasi tez orada qo'shiladi.",
        "not_added": "Qo'shilmagan", "added": "Qo'shildi", "space": "Bo'sh joy", "keep": "O'zgartirmaslik",
        "default_cover_source": "Standart", "music_cover_source": "Musiqadan", "search_cover_source": "Trek qidiruvidan",
        "title": "Nomi", "artist": "Ijrochi", "size": "Hajmi", "duration": "Davomiyligi", "effect_8d": "8D effekti", "bass": "Bass", "speed": "Tezlik", "bitrate": "Bitrate", "trim": "Kesish", "photo": "Foto", "voice": "Voice rejimi", "yes": "Ha",
        "start_quick_tags": "⚙️ Standart teglar", "how_to_use": "❓ Qanday ishlatish", "change_language": "🌐 Tilni o'zgartirish", "saved_items": "📂 Saqlanganlar", "weekly_hits": "🏆 Haftalik hitlar", "search_music": "🔎 Musiqa qidirish", "main_menu": "🏠 Asosiy menyu",
        "close": "❌ Yopish", "back": "« Orqaga", "inline_search": "🔎 Inline qidiruv", "cancel": "✖ Bekor qilish", "no_caption": "❎ Captionsiz",
        "edit_tags": "📝 Teglarni o'zgartirish", "edit_photo": "🖼 Fotoni o'zgartirish", "shazam": "🎧 Shazam", "effect_8d_btn": "🌪 8D effekti", "bass_btn": "🔉 Bass", "speed_btn": "⚡ Tezlik", "bitrate_btn": "🎚 Bitrate", "cut_btn": "✂️ Kesish", "voice_btn": "🎤 Voice'ga", "caption_btn": "🏷 Caption", "save_btn": "💾 Saqlash",
        "quick_photo_btn": "🖼 Foto: {status}", "quick_title_btn": "🎵 Trek nomi: {status}", "quick_artist_btn": "👤 Ijrochi: {status}", "quick_bitrate_btn": "🎚 Bitrate: {status}",
        "upload_or_change": "📸 Yuklash / O'zgartirish", "delete_photo": "🗑 Fotoni o'chirish", "add_or_change": "✏️ Qo'shish / O'zgartirish", "space_btn": "⚪ Bo'sh joy", "keep_btn": "❌ O'zgartirmaslik",
        "saved_tracks_title": "📂 <b>Saqlangan treklar</b>\n\n", "no_saved_tracks": "Hozircha saqlangan treklaringiz yo'q ❎",
        "file_too_large": "❌ Fayl juda katta: {size_mb:.2f} MB.\nFaqat {max_mb} MB gacha musiqa yuborish mumkin.",
        "unknown_title": "Noma'lum", "send_at_least_title": "Hech bo'lmasa trek nomini kiriting.", "invalid_trim_format": "Noto'g'ri format. Misol: 00:20 - 02:30",
        "send_one_photo_alert": "Bitta fotoni bitta xabar bilan yuboring.", "send_title_alert": "Trek nomini bitta xabar bilan yuboring.", "send_artist_alert": "Ijrochi nomini bitta xabar bilan yuboring.",
        "russian_already_selected": "Rus tili allaqachon tanlangan.", "language_changed_en": "Til ingliz tiliga o'zgartirildi.", "language_changed_uz": "Til o'zbek tiliga o'zgartirildi.", "language_changed_ru": "Til rus tiliga o'zgartirildi.",
        "send_music_first": "Avval musiqani yuboring.", "shazam_cover_not_found": "Bu trek uchun cover topilmadi.", "action_cancelled": "Harakat bekor qilindi.", "unknown_command": "❌ Noma'lum buyruq",
        "choose_bass": "🎧 Bass darajasini tanlang", "choose_8d": "🎧 8D effektini o'zgartirish uchun tugmani bosing.", "choose_speed": "▶ O'ynatish tezligini tanlang.", "choose_bitrate": "Bitrate'ni tanlang (kbps):",
        "video_download_started": "⏬ Havola bo'yicha video yuklab olinmoqda...",
        "video_download_failed": "❌ Bu havola orqali videoni yuklab bo'lmadi.",
        "tiktok_short_link_failed": "❌ TikTok qisqa havolasi hozir ochilmadi.\n`https://www.tiktok.com/...` ko'rinishidagi to'liq havolani yuboring va yana urinib ko'ring.",
        "instagram_auth_required": "❌ Instagram bu videoni akkauntga kirmasdan bermadi.\nAgar havola yuklanmasa, botga `instagram_cookies.txt` faylini yuboring va yana urinib ko'ring.",
        "instagram_cookies_saved": "✅ `instagram_cookies.txt` fayli saqlandi. Endi havolani yana yuboring.",
        "instagram_cookies_invalid": "❌ Aynan `instagram_cookies.txt` nomli fayl yuboring.",
        "video_too_large": "❌ Yuklangan video juda katta: {size_mb:.2f} MB.\nMen faqat {max_mb} MB gacha fayl yubora olaman.",
        "circle_prompt": "📹 Oddiy video yuboring, men undan doiracha tayyorlayman.",
        "circle_started": "🔄 Videodan doiracha tayyorlayapman...",
        "circle_failed": "❌ Bu videodan doiracha tayyorlab bo'lmadi. Boshqa video yuboring.",
        "uncircle_prompt": "🎥 Doiracha yuboring, men uni oddiy videoga aylantiraman.",
        "uncircle_started": "🔄 Doirachani oddiy videoga aylantiryapman...",
        "uncircle_failed": "❌ Bu doirachani oddiy videoga aylantirib bo'lmadi. Yana urinib ko'ring.",
        "video_editor_title": "🎬 <b>Video tahrirlash</b>",
        "video_circle_btn": "⭕ Doiracha",
        "video_mp3_btn": "🎵 Video -> MP3",
        "video_trim_btn": "✂️ Videoni kesish",
        "video_send_btn": "📤 Videoni yuborish",
        "video_caption_btn": "🏷 Ijtimoiy video caption",
        "video_trim_prompt": "Video vaqtini `mm:ss - mm:ss` formatida yuboring",
        "video_caption_saved": "✅ Yuklangan ijtimoiy videolar uchun yangi caption saqlandi.",
        "video_caption_current": "Joriy caption: {caption}",
        "video_mp3_started": "🔄 Videodan musiqa ajratilmoqda...",
        "video_mp3_failed": "❌ Bu videodan musiqa ajratib bo'lmadi.",
        "video_trim_started": "🔄 Video kesilmoqda...",
        "video_trim_failed": "❌ Bu videoni kesib bo'lmadi.",
        "video_ready": "✅ Tayyor.",
        "stats_text": "<b>Bot statistikasi</b>\n\n🎵 Musiqa: {audio_edits}\n📥 Ijtimoiy videolar: {social_downloads}\n⭕ Doirachalar: {video_circles}\n🎥 Doirachadan video: {circle_to_video}\n🎶 Video -> MP3: {video_mp3}\n✂️ Video kesishlar: {video_trims}",
        "admin_only": "⛔ Bu buyruq faqat administrator uchun.",
        "admin_panel_title": "<b>Admin panel</b>\n\n👤 Foydalanuvchilar: {user_count}\n⏱ Uptime: {uptime}",
        "admin_stats_btn": "📊 Statistika",
        "admin_users_btn": "👥 Foydalanuvchilar",
        "admin_files_btn": "🗂 Fayllar",
        "admin_back_btn": "« Orqaga",
        "admin_reply_btn": "🛠 Admin panel",
        "admin_reply_ready": "⬇️ Admin panel tugmasi pastda biriktirildi.",
        "admin_users_text": "<b>Bot foydalanuvchilari</b>\n\nJami: {user_count}\n\n{users}",
        "admin_files_text": "<b>Bot fayllari</b>\n\n📁 Jami fayl: {file_count}\n💾 Umumiy hajm: {total_size_mb:.2f} MB\n🗂 Downloads ichida: {downloads_count}\n🧪 Temp ichida: {temp_count}",
        "lang_ru": "Ruscha", "lang_en": "English", "lang_uz": "O'zbek",
    },
}


def get_user_lang(user_id: int) -> str:
    return user_languages.get(user_id, "ru")


def tr(user_id: int, key: str, **kwargs) -> str:
    lang = get_user_lang(user_id)
    text = TRANSLATIONS.get(lang, TRANSLATIONS["ru"]).get(key, TRANSLATIONS["ru"].get(key, key))
    return text.format(**kwargs) if kwargs else text


def get_social_caption(user_id: int) -> str:
    return user_social_captions.get(user_id, SOCIAL_RESULT_CAPTION)


def record_stat(key: str) -> None:
    bot_stats[key] = bot_stats.get(key, 0) + 1


def start_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr(user_id, "start_quick_tags"), callback_data="quick_tags")],
            [InlineKeyboardButton(text=tr(user_id, "how_to_use"), callback_data="how_to_use")],
            [
                InlineKeyboardButton(text=tr(user_id, "change_language"), callback_data="change_language"),
                InlineKeyboardButton(text=tr(user_id, "saved_items"), callback_data="saved_items"),
            ],
            [InlineKeyboardButton(text=tr(user_id, "weekly_hits"), callback_data="weekly_hits")],
            [InlineKeyboardButton(text=tr(user_id, "search_music"), callback_data="search_music")],
        ]
    )


def back_to_menu_button(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=tr(user_id, "main_menu"), callback_data="main_menu")]]
    )


def get_quick_settings(user_id: int) -> QuickTagSettings:
    if user_id not in quick_tag_settings:
        quick_tag_settings[user_id] = QuickTagSettings()
    return quick_tag_settings[user_id]


def format_quick_title_value(settings: QuickTagSettings) -> str:
    user_id = next((uid for uid, value in quick_tag_settings.items() if value is settings), 0)
    if settings.title_mode == "blank":
        return tr(user_id, "space")
    if settings.title_mode == "value" and settings.title_value:
        return settings.title_value
    return tr(user_id, "not_added")


def format_quick_artist_value(settings: QuickTagSettings) -> str:
    user_id = next((uid for uid, value in quick_tag_settings.items() if value is settings), 0)
    if settings.artist_mode == "blank":
        return tr(user_id, "space")
    if settings.artist_mode == "value" and settings.artist_value:
        return settings.artist_value
    return tr(user_id, "not_added")


def format_quick_photo_value(settings: QuickTagSettings) -> str:
    user_id = next((uid for uid, value in quick_tag_settings.items() if value is settings), 0)
    return tr(user_id, "added") if settings.cover_path and os.path.exists(settings.cover_path) else tr(user_id, "not_added")


def format_quick_bitrate_value(settings: QuickTagSettings) -> str:
    user_id = next((uid for uid, value in quick_tag_settings.items() if value is settings), 0)
    return str(settings.bitrate) if settings.bitrate else tr(user_id, "not_added")


def quick_tags_text(user_id: int, settings: QuickTagSettings) -> str:
    return (
        f"{tr(user_id, 'quick_tags_intro')}\n\n"
        f"❌ {tr(user_id, 'photo')}: {escape(format_quick_photo_value(settings))}\n"
        f"❌ {tr(user_id, 'title')}: {escape(format_quick_title_value(settings))}\n"
        f"❌ {tr(user_id, 'artist')}: {escape(format_quick_artist_value(settings))}\n"
        f"❌ {tr(user_id, 'bitrate')}: {escape(format_quick_bitrate_value(settings))}"
    )


def quick_tags_menu(user_id: int, settings: QuickTagSettings) -> InlineKeyboardMarkup:
    missing = tr(user_id, "not_added")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr(user_id, "quick_photo_btn", status="❌" if format_quick_photo_value(settings) == missing else "✅"), callback_data="quick_photo")],
            [InlineKeyboardButton(text=tr(user_id, "quick_title_btn", status="❌" if format_quick_title_value(settings) == missing else "✅"), callback_data="quick_title")],
            [InlineKeyboardButton(text=tr(user_id, "quick_artist_btn", status="❌" if format_quick_artist_value(settings) == missing else "✅"), callback_data="quick_artist")],
            [InlineKeyboardButton(text=tr(user_id, "quick_bitrate_btn", status="❌" if format_quick_bitrate_value(settings) == missing else "✅"), callback_data="quick_bitrate")],
            [InlineKeyboardButton(text=tr(user_id, "close"), callback_data="main_menu")],
        ]
    )


def quick_photo_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr(user_id, "upload_or_change"), callback_data="quick_photo_set")],
            [InlineKeyboardButton(text=tr(user_id, "delete_photo"), callback_data="quick_photo_delete")],
            [InlineKeyboardButton(text=tr(user_id, "back"), callback_data="quick_tags")],
            [InlineKeyboardButton(text=tr(user_id, "close"), callback_data="main_menu")],
        ]
    )


def quick_title_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr(user_id, "add_or_change"), callback_data="quick_title_set")],
            [InlineKeyboardButton(text=tr(user_id, "space_btn"), callback_data="quick_title_blank")],
            [InlineKeyboardButton(text=tr(user_id, "keep_btn"), callback_data="quick_title_keep")],
            [InlineKeyboardButton(text=tr(user_id, "back"), callback_data="quick_tags")],
            [InlineKeyboardButton(text=tr(user_id, "close"), callback_data="main_menu")],
        ]
    )


def quick_artist_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr(user_id, "add_or_change"), callback_data="quick_artist_set")],
            [InlineKeyboardButton(text=tr(user_id, "space_btn"), callback_data="quick_artist_blank")],
            [InlineKeyboardButton(text=tr(user_id, "keep_btn"), callback_data="quick_artist_keep")],
            [InlineKeyboardButton(text=tr(user_id, "back"), callback_data="quick_tags")],
            [InlineKeyboardButton(text=tr(user_id, "close"), callback_data="main_menu")],
        ]
    )


def quick_bitrate_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="64", callback_data="quick_bitrate_value:64"),
                InlineKeyboardButton(text="96", callback_data="quick_bitrate_value:96"),
            ],
            [
                InlineKeyboardButton(text="128", callback_data="quick_bitrate_value:128"),
                InlineKeyboardButton(text="160", callback_data="quick_bitrate_value:160"),
            ],
            [
                InlineKeyboardButton(text="192", callback_data="quick_bitrate_value:192"),
                InlineKeyboardButton(text="256", callback_data="quick_bitrate_value:256"),
            ],
            [InlineKeyboardButton(text="320", callback_data="quick_bitrate_value:320")],
            [InlineKeyboardButton(text=tr(user_id, "keep_btn"), callback_data="quick_bitrate_keep")],
            [InlineKeyboardButton(text=tr(user_id, "back"), callback_data="quick_tags")],
            [InlineKeyboardButton(text=tr(user_id, "close"), callback_data="main_menu")],
        ]
    )


def how_to_use_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 " + tr(user_id, "guide_tags").split(" soon")[0].split(" скоро")[0], callback_data="guide_tags")],
            [InlineKeyboardButton(text="📌 " + ("Guide to default tags" if get_user_lang(user_id) == "en" else "Standart teglar bo'yicha qo'llanma" if get_user_lang(user_id) == "uz" else "Гайд по тегам по умолчанию"), callback_data="guide_quick_tags")],
            [InlineKeyboardButton(text="🖼 " + ("Changing music cover" if get_user_lang(user_id) == "en" else "Musiqa coverini o'zgartirish" if get_user_lang(user_id) == "uz" else "Изменение обложки музыки"), callback_data="guide_cover")],
            [InlineKeyboardButton(text="🎙 " + ("Converting music to voice message" if get_user_lang(user_id) == "en" else "Musiqani voice xabarga aylantirish" if get_user_lang(user_id) == "uz" else "Конвертация музыки в голосовое сообщение"), callback_data="guide_voice")],
            [InlineKeyboardButton(text="✂️ " + ("Trimming music" if get_user_lang(user_id) == "en" else "Musiqani kesish" if get_user_lang(user_id) == "uz" else "Обрезка музыки"), callback_data="guide_cut")],
            [InlineKeyboardButton(text="💡 " + ("Other bot features" if get_user_lang(user_id) == "en" else "Boshqa imkoniyatlar" if get_user_lang(user_id) == "uz" else "Другие возможности бота"), callback_data="guide_other")],
            [InlineKeyboardButton(text="📼 " + ("Download from Instagram/TikTok" if get_user_lang(user_id) == "en" else "Instagram va TikTok'dan yuklab olish" if get_user_lang(user_id) == "uz" else "Скачивание из Instagram и TikTok"), callback_data="guide_search")],
            [InlineKeyboardButton(text=tr(user_id, "back"), callback_data="main_menu")],
        ]
    )


async def get_bot_link() -> str:
    global BOT_LINK_CACHE
    if BOT_LINK_CACHE:
        return BOT_LINK_CACHE

    me = await bot.me()
    BOT_LINK_CACHE = f"https://t.me/{me.username}"
    return BOT_LINK_CACHE


async def social_result_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=SOCIAL_RESULT_BUTTON_TEXT, url=await get_bot_link())],
        ]
    )


def language_menu(user_id: int) -> InlineKeyboardMarkup:
    current = get_user_lang(user_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"[ {tr(user_id, 'lang_ru')} ]" if current == "ru" else tr(user_id, "lang_ru"), callback_data="lang_ru"),
                InlineKeyboardButton(text=f"[ {tr(user_id, 'lang_en')} ]" if current == "en" else tr(user_id, "lang_en"), callback_data="lang_en"),
            ],
            [InlineKeyboardButton(text=f"[ {tr(user_id, 'lang_uz')} ]" if current == "uz" else tr(user_id, "lang_uz"), callback_data="lang_uz")],
            [InlineKeyboardButton(text=tr(user_id, "back"), callback_data="main_menu")],
        ]
    )


def search_music_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr(user_id, "inline_search"), switch_inline_query_current_chat="")],
            [InlineKeyboardButton(text=tr(user_id, "main_menu"), callback_data="main_menu")],
        ]
    )


def cancel_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=tr(user_id, "cancel"), callback_data="cancel_action")]]
    )


def choice_menu(user_id: int, prefix: str, values: list[str]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for value in values:
        row.append(InlineKeyboardButton(text=value, callback_data=f"{prefix}:{value}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=tr(user_id, "cancel"), callback_data="cancel_action")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def caption_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr(user_id, "no_caption"), callback_data="caption:none")],
            [InlineKeyboardButton(text=tr(user_id, "cancel"), callback_data="cancel_action")],
        ]
    )


def music_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✨ " + tr(user_id, "start_quick_tags").replace("⚙️ ", ""), callback_data="quick_tags")],
            [
                InlineKeyboardButton(text=tr(user_id, "edit_tags"), callback_data="tags"),
                InlineKeyboardButton(text=tr(user_id, "edit_photo"), callback_data="photo"),
            ],
            [
                InlineKeyboardButton(text=tr(user_id, "shazam"), callback_data="shazam"),
                InlineKeyboardButton(text=tr(user_id, "effect_8d_btn"), callback_data="8d"),
            ],
            [
                InlineKeyboardButton(text=tr(user_id, "bass_btn"), callback_data="bass"),
                InlineKeyboardButton(text=tr(user_id, "speed_btn"), callback_data="speed"),
            ],
            [
                InlineKeyboardButton(text=tr(user_id, "bitrate_btn"), callback_data="bitrate"),
                InlineKeyboardButton(text=tr(user_id, "cut_btn"), callback_data="cut"),
            ],
            [
                InlineKeyboardButton(text=tr(user_id, "voice_btn"), callback_data="voice"),
                InlineKeyboardButton(text=tr(user_id, "caption_btn"), callback_data="caption"),
            ],
            [InlineKeyboardButton(text=tr(user_id, "save_btn"), callback_data="save_track")],
        ]
    )


def video_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=tr(user_id, "video_circle_btn"), callback_data="video_circle"),
                InlineKeyboardButton(text=tr(user_id, "video_mp3_btn"), callback_data="video_mp3"),
            ],
            [
                InlineKeyboardButton(text=tr(user_id, "video_trim_btn"), callback_data="video_trim"),
                InlineKeyboardButton(text=tr(user_id, "video_send_btn"), callback_data="video_send"),
            ],
            [InlineKeyboardButton(text=tr(user_id, "close"), callback_data="video_close")],
        ]
    )


def admin_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=tr(user_id, "admin_stats_btn"), callback_data="admin_stats"),
                InlineKeyboardButton(text=tr(user_id, "admin_users_btn"), callback_data="admin_users"),
            ],
            [InlineKeyboardButton(text=tr(user_id, "admin_files_btn"), callback_data="admin_files")],
            [InlineKeyboardButton(text=tr(user_id, "close"), callback_data="admin_close")],
        ]
    )


def admin_reply_keyboard(user_id: int) -> ReplyKeyboardMarkup | None:
    if not is_admin(user_id):
        return None
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=tr(user_id, "admin_stats_btn")),
                KeyboardButton(text=tr(user_id, "admin_users_btn")),
            ],
            [KeyboardButton(text=tr(user_id, "admin_files_btn"))],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def seconds_to_mmss(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def parse_mmss(value: str) -> int:
    minutes, seconds = value.strip().split(":")
    return int(minutes) * 60 + int(seconds)


def format_track_text(session: TrackSession) -> str:
    trim_value = "—"
    if session.trim_start_ms is not None and session.trim_end_ms is not None:
        trim_value = (
            f"{seconds_to_mmss(session.trim_start_ms // 1000)} - "
            f"{seconds_to_mmss(session.trim_end_ms // 1000)}"
        )

    voice_value = tr(session.user_id, "yes") if session.voice_mode else "—"

    return (
        f"📝 <b>{tr(session.user_id, 'title')}:</b> <code>{escape(session.title)}</code>\n"
        f"👤 <b>{tr(session.user_id, 'artist')}:</b> <code>{escape(session.performer)}</code>\n"
        f"💾 <b>{tr(session.user_id, 'size')}:</b> <code>{session.size_mb:.2f} MB</code>\n"
        f"⏱ <b>{tr(session.user_id, 'duration')}:</b> <code>{seconds_to_mmss(session.duration_seconds)}</code>\n"
        f"🌪 <b>{tr(session.user_id, 'effect_8d')}:</b> {session.effect_8d if session.effect_8d else '0'}\n"
        f"🔉 <b>{tr(session.user_id, 'bass')}:</b> {session.bass_level if session.bass_level else '0'}\n"
        f"⚡ <b>{tr(session.user_id, 'speed')}:</b> {session.speed:.1f}x\n"
        f"🎚 <b>{tr(session.user_id, 'bitrate')}:</b> {session.bitrate} kbps\n"
        f"✂️ <b>{tr(session.user_id, 'trim')}:</b> {trim_value}\n"
        f"🖼 <b>{tr(session.user_id, 'photo')}:</b> {escape(session.cover_source)}\n"
        f"🎤 <b>{tr(session.user_id, 'voice')}:</b> {voice_value}"
    )


def format_video_text(session: VideoSession) -> str:
    trim_value = "—"
    if session.trim_start_ms is not None and session.trim_end_ms is not None:
        trim_value = (
            f"{seconds_to_mmss(session.trim_start_ms // 1000)} - "
            f"{seconds_to_mmss(session.trim_end_ms // 1000)}"
        )

    return (
        f"{tr(session.user_id, 'video_editor_title')}\n\n"
        f"💾 <b>{tr(session.user_id, 'size')}:</b> <code>{session.size_mb:.2f} MB</code>\n"
        f"⏱ <b>{tr(session.user_id, 'duration')}:</b> <code>{seconds_to_mmss(session.duration_seconds)}</code>\n"
        f"✂️ <b>{tr(session.user_id, 'trim')}:</b> {trim_value}"
    )


def build_admin_panel_text(user_id: int) -> str:
    return tr(
        user_id,
        "admin_panel_title",
        user_count=len(known_users),
        uptime=format_uptime(),
    )


def build_admin_users_text(user_id: int) -> str:
    sorted_users = sorted(
        known_users.items(),
        key=lambda item: item[1].get("last_seen", 0),
        reverse=True,
    )[:15]
    lines = []
    for item_user_id, info in sorted_users:
        username = info.get("username") or "—"
        name = " ".join(part for part in [info.get("first_name", ""), info.get("last_name", "")] if part).strip() or "—"
        lines.append(f"• <code>{item_user_id}</code> | @{escape(username)} | {escape(name)}")
    users_text = "\n".join(lines) if lines else "—"
    return tr(user_id, "admin_users_text", user_count=len(known_users), users=users_text)


def build_admin_files_text(user_id: int) -> str:
    file_count = 0
    total_size = 0
    downloads_count = 0
    temp_count = 0
    for root, _, files in os.walk(DOWNLOADS):
        for file_name in files:
            path = os.path.join(root, file_name)
            if os.path.isfile(path):
                file_count += 1
                total_size += os.path.getsize(path)
                if root == DOWNLOADS:
                    downloads_count += 1
                elif root.startswith(TEMP_DIR):
                    temp_count += 1
    return tr(
        user_id,
        "admin_files_text",
        file_count=file_count,
        total_size_mb=total_size / 1024 / 1024,
        downloads_count=downloads_count,
        temp_count=temp_count,
    )


def get_session(user_id: int) -> Optional[TrackSession]:
    return user_sessions.get(user_id)


async def delete_prompt_if_exists(session: TrackSession) -> None:
    if session.prompt_message_id and session.prompt_chat_id:
        try:
            await bot.delete_message(session.prompt_chat_id, session.prompt_message_id)
        except Exception:
            pass
    session.prompt_message_id = None
    session.prompt_chat_id = None


async def delete_video_prompt_if_exists(session: VideoSession) -> None:
    if session.prompt_message_id and session.prompt_chat_id:
        try:
            await bot.delete_message(session.prompt_chat_id, session.prompt_message_id)
        except Exception:
            pass
    session.prompt_message_id = None
    session.prompt_chat_id = None


async def update_track_card(session: TrackSession) -> None:
    try:
        await bot.edit_message_media(
            chat_id=session.chat_id,
            message_id=session.card_message_id,
            media=InputMediaPhoto(
                media=FSInputFile(get_display_cover_path(session)),
                caption=format_track_text(session),
                parse_mode="HTML",
            ),
            reply_markup=music_menu(session.user_id),
        )
    except Exception:
        pass


async def send_prompt(
    source: CallbackQuery,
    session: TrackSession,
    text: str,
    markup: InlineKeyboardMarkup,
    parse_mode: Optional[str] = "HTML",
) -> None:
    await delete_prompt_if_exists(session)
    sent = await source.message.answer(text, reply_markup=markup, parse_mode=parse_mode)
    session.prompt_message_id = sent.message_id
    session.prompt_chat_id = sent.chat.id


async def send_video_prompt(
    source: CallbackQuery,
    session: VideoSession,
    text: str,
    markup: InlineKeyboardMarkup,
    parse_mode: Optional[str] = "HTML",
) -> None:
    await delete_video_prompt_if_exists(session)
    sent = await source.message.answer(text, reply_markup=markup, parse_mode=parse_mode)
    session.prompt_message_id = sent.message_id
    session.prompt_chat_id = sent.chat.id


async def read_audio_metadata(file_path: str) -> tuple[str, str, int, float]:
    audio = MP3(file_path)
    title = "Неизвестно"
    performer = "Неизвестно"
    if audio.tags:
        if audio.tags.get("TIT2"):
            title = str(audio.tags.get("TIT2"))
        if audio.tags.get("TPE1"):
            performer = str(audio.tags.get("TPE1"))
    duration_seconds = int(round(audio.info.length))
    size_mb = os.path.getsize(file_path) / 1024 / 1024
    return title, performer, duration_seconds, size_mb


def ensure_jpeg_cover(image_bytes: bytes, output_path: str) -> str:
    with Image.open(BytesIO(image_bytes)) as img:
        rgb = img.convert("RGB")
        rgb.save(output_path, format="JPEG", quality=95)
    return output_path


def prepare_audio_thumbnail(source_path: str, output_path: str) -> str:
    with Image.open(source_path) as img:
        rgb = img.convert("RGB")
        width, height = rgb.size
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        square = rgb.crop((left, top, left + side, top + side))
        square = square.resize((320, 320))
        for quality in (85, 75, 65, 55, 45):
            square.save(output_path, format="JPEG", quality=quality, optimize=True)
            if os.path.getsize(output_path) <= 190 * 1024:
                break
    return output_path


def ensure_default_cover() -> str:
    if os.path.exists(DEFAULT_COVER_PATH):
        return DEFAULT_COVER_PATH

    size = 900
    image = Image.new("RGB", (size, size), "#edf2fb")
    draw = ImageDraw.Draw(image)

    card_margin = 70
    draw.rounded_rectangle(
        (card_margin, card_margin, size - card_margin, size - card_margin),
        radius=90,
        fill="#ffffff",
    )

    draw.ellipse((170, 160, 580, 570), fill="#1f2734")
    draw.ellipse((285, 275, 465, 455), fill="#2f80ed")
    draw.ellipse((350, 340, 400, 390), fill="#1f2734")
    draw.arc((220, 210, 420, 410), start=205, end=295, fill="#aeb7c8", width=14)

    draw.rounded_rectangle((500, 215, 745, 325), radius=35, fill="#ffffff")
    draw.rounded_rectangle((520, 165, 770, 415), radius=40, fill="#1f2734")
    draw.rounded_rectangle((545, 190, 735, 380), radius=28, fill="#2f80ed")
    draw.polygon([(670, 188), (738, 188), (738, 258), (700, 222)], fill="#ff9d2e")
    draw.polygon([(548, 385), (660, 497), (700, 415), (588, 303)], fill="#ffffff")
    draw.polygon([(595, 348), (653, 406), (599, 432), (543, 376)], fill="#ffcf62")

    draw.ellipse((650, 505, 760, 615), fill="#1f2734")
    draw.rectangle((735, 370, 760, 560), fill="#1f2734")
    draw.polygon([(760, 370), (832, 415), (832, 510), (792, 490), (792, 432), (760, 412)], fill="#1f2734")

    image.save(DEFAULT_COVER_PATH, format="JPEG", quality=95)
    return DEFAULT_COVER_PATH


def extract_embedded_cover(track_path: str, user_id: int) -> Optional[str]:
    try:
        audio_file = MutagenFile(track_path)
    except Exception:
        return None

    if not audio_file or not getattr(audio_file, "tags", None):
        return None

    apic_tags = audio_file.tags.getall("APIC") if hasattr(audio_file.tags, "getall") else []
    for apic in apic_tags:
        if getattr(apic, "data", None):
            cover_path = os.path.join(TEMP_DIR, f"{user_id}_embedded_cover.jpg")
            ensure_jpeg_cover(apic.data, cover_path)
            return cover_path
    return None


def get_display_cover_path(session: TrackSession) -> str:
    if session.cover_path and os.path.exists(session.cover_path):
        return session.cover_path
    return ensure_default_cover()


def extract_supported_url(text: str) -> Optional[str]:
    match = URL_RE.search(text)
    if not match:
        return None
    url = match.group(0).strip()
    lowered = url.lower()
    if any(host in lowered for host in SUPPORTED_VIDEO_HOSTS):
        return url
    return None


def looks_like_instagram_cookies_file(file_name: str) -> bool:
    lowered = file_name.lower()
    return lowered.endswith(".txt") and "instagram" in lowered and "cookie" in lowered


def find_instagram_cookies_file() -> Optional[str]:
    if os.path.exists(INSTAGRAM_COOKIES_FILE):
        return INSTAGRAM_COOKIES_FILE

    for file_name in os.listdir(os.getcwd()):
        if looks_like_instagram_cookies_file(file_name):
            candidate = os.path.join(os.getcwd(), file_name)
            if os.path.isfile(candidate):
                return candidate
    return None


def resolve_yt_dlp_filepath(info: dict, fallback_path: str) -> str:
    requested_downloads = info.get("requested_downloads") or []
    if requested_downloads:
        filepath = requested_downloads[0].get("filepath")
        if filepath and os.path.exists(filepath):
            return filepath

    direct_filename = info.get("_filename")
    if direct_filename and os.path.exists(direct_filename):
        return direct_filename

    if os.path.exists(fallback_path):
        return fallback_path

    base, _ = os.path.splitext(fallback_path)
    for ext in (".mp4", ".mkv", ".webm"):
        candidate = base + ext
        if os.path.exists(candidate):
            return candidate
    return fallback_path


def get_video_platform(url: str) -> str:
    lowered = url.lower()
    if "tiktok.com" in lowered or "vm.tiktok.com" in lowered or "vt.tiktok.com" in lowered:
        return "tiktok"
    return "instagram"


def iter_candidate_video_urls(url: str) -> list[str]:
    candidates = [url]
    if "vt.tiktok.com/" in url.lower() or "vm.tiktok.com/" in url.lower():
        expanded = resolve_short_video_url(url)
        if expanded and expanded not in candidates:
            candidates.append(expanded)
    cleaned = urlunsplit((*urlsplit(url)[:3], "", ""))
    if cleaned != url:
        candidates.append(cleaned)
    return candidates


def resolve_short_video_url(url: str) -> Optional[str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            final_url = response.geturl()
            return final_url if final_url else None
    except Exception:
        return None


def build_yt_dlp_options(
    user_id: int,
    platform: str,
    *,
    user_agent: Optional[str] = None,
    include_headers: bool = True,
) -> dict:
    outtmpl = os.path.join(TEMP_DIR, f"{user_id}_%(extractor)s_%(id)s.%(ext)s")
    referer = "https://www.tiktok.com/" if platform == "tiktok" else "https://www.instagram.com/"
    headers = {
        "User-Agent": user_agent
        or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/135.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        "Referer": referer,
    }
    options = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": SilentYtDlpLogger(),
        "merge_output_format": "mp4",
        "format": "b[ext=mp4]/bv*+ba/b",
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "nocheckcertificate": True,
    }
    if platform == "instagram":
        options["extractor_args"] = {
            "instagram": {
                "app_id": ["936619743392459"],
            }
        }
    if include_headers:
        options["http_headers"] = headers
    return options


def is_instagram_auth_error(exc: Exception) -> bool:
    pending = [exc]
    seen: set[int] = set()
    markers = (
        "instagram sent an empty media response",
        "login required",
        "requested content is not available",
        "cookies-from-browser",
        "authentication",
        "sessionid",
        "cookie",
        "private",
    )

    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if any(marker in str(current).lower() for marker in markers):
            return True
        pending.append(getattr(current, "__cause__", None))
        pending.append(getattr(current, "__context__", None))
    return False


def is_tiktok_short_link_error(url: str, exc: Exception) -> bool:
    lowered_url = url.lower()
    if "vt.tiktok.com/" not in lowered_url and "vm.tiktok.com/" not in lowered_url:
        return False

    pending = [exc]
    seen: set[int] = set()
    markers = (
        "unexpected_eof_while_reading",
        "ssl",
        "eof occurred in violation of protocol",
        "connection was reset",
        "unable to download webpage",
    )

    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if any(marker in str(current).lower() for marker in markers):
            return True
        pending.append(getattr(current, "__cause__", None))
        pending.append(getattr(current, "__context__", None))
    return False


def iter_ydl_options(user_id: int, url: str) -> list[dict]:
    platform = get_video_platform(url)
    cookies_file = find_instagram_cookies_file()
    desktop_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    )
    mobile_agent = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.3 Mobile/15E148 Safari/604.1"
    )

    if platform == "instagram":
        variants = [
            build_yt_dlp_options(user_id, platform, user_agent=desktop_agent, include_headers=False),
            build_yt_dlp_options(user_id, platform, user_agent=mobile_agent, include_headers=False),
            build_yt_dlp_options(user_id, platform, user_agent=mobile_agent, include_headers=True),
        ]
    else:
        variants = [
            build_yt_dlp_options(user_id, platform, user_agent=desktop_agent, include_headers=True),
            build_yt_dlp_options(user_id, platform, user_agent=mobile_agent, include_headers=True),
            build_yt_dlp_options(user_id, platform, user_agent=desktop_agent, include_headers=False),
        ]

    options: list[dict] = []
    if platform == "instagram" and cookies_file:
        for variant in variants:
            cookie_opts = dict(variant)
            cookie_opts["cookiefile"] = cookies_file
            options.append(cookie_opts)
    options.extend(variants)
    return options


def download_video_from_url(url: str, user_id: int) -> tuple[str, str]:
    last_error: Optional[Exception] = None
    auth_error = False
    platform = get_video_platform(url)

    for candidate_url in iter_candidate_video_urls(url):
        for ydl_opts in iter_ydl_options(user_id, candidate_url):
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(candidate_url, download=True)
                    title = info.get("title") or "video"
                    fallback_path = ydl.prepare_filename(info)
                    return resolve_yt_dlp_filepath(info, fallback_path), title
            except DownloadError as exc:
                last_error = exc
                if platform == "instagram" and is_instagram_auth_error(exc):
                    auth_error = True
                    continue
                continue
            except Exception as exc:
                last_error = exc
                if platform == "instagram" and is_instagram_auth_error(exc):
                    auth_error = True
                    continue
                continue

    if auth_error:
        raise InstagramAuthRequiredError from last_error
    if last_error:
        raise last_error
    raise RuntimeError(f"{platform} download failed without a specific error")


def normalize_video_for_telegram(input_path: str, user_id: int) -> str:
    base_name = os.path.splitext(os.path.basename(input_path))[0] or f"{user_id}_video"
    output_path = os.path.join(TEMP_DIR, f"{user_id}_{base_name}_telegram.mp4")
    command = [
        FFMPEG_EXE,
        "-y",
        "-i",
        input_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        output_path,
    ]
    subprocess.run(command, check=True, capture_output=True)
    return output_path


def convert_video_to_note(input_path: str, user_id: int) -> str:
    base_name = os.path.splitext(os.path.basename(input_path))[0] or f"{user_id}_video_note"
    output_path = os.path.join(TEMP_DIR, f"{user_id}_{base_name}_note.mp4")
    command = [
        FFMPEG_EXE,
        "-y",
        "-i",
        input_path,
        "-t",
        "60",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        "crop=min(iw\\,ih):min(iw\\,ih),scale=640:640",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        output_path,
    ]
    subprocess.run(command, check=True, capture_output=True)
    return output_path


def convert_note_to_video(input_path: str, user_id: int) -> str:
    base_name = os.path.splitext(os.path.basename(input_path))[0] or f"{user_id}_video_from_note"
    output_path = os.path.join(TEMP_DIR, f"{user_id}_{base_name}_video.mp4")
    command = [
        FFMPEG_EXE,
        "-y",
        "-i",
        input_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        output_path,
    ]
    subprocess.run(command, check=True, capture_output=True)
    return output_path


async def handle_video_link(message: Message, url: str) -> None:
    status = await message.answer(tr(message.from_user.id, "video_download_started"))
    try:
        video_path, title = await asyncio.to_thread(download_video_from_url, url, message.from_user.id)
        if not os.path.exists(video_path):
            raise FileNotFoundError(video_path)

        try:
            normalized_path = await asyncio.to_thread(normalize_video_for_telegram, video_path, message.from_user.id)
            if os.path.exists(normalized_path):
                video_path = normalized_path
        except Exception:
            pass

        size_bytes = os.path.getsize(video_path)
        if size_bytes > MAX_VIDEO_SIZE_BYTES:
            size_mb = size_bytes / 1024 / 1024
            await status.edit_text(
                tr(
                    message.from_user.id,
                    "video_too_large",
                    size_mb=size_mb,
                    max_mb=MAX_VIDEO_SIZE_MB,
                )
            )
            return

        try:
            await message.answer_video(
                video=FSInputFile(video_path),
                caption=escape(get_social_caption(message.from_user.id)),
                parse_mode="HTML",
                reply_markup=await social_result_menu(),
            )
        except Exception:
            await message.answer_document(
                document=FSInputFile(video_path),
                caption=escape(get_social_caption(message.from_user.id)),
                parse_mode="HTML",
                reply_markup=await social_result_menu(),
            )
        record_stat("social_downloads")
        await status.delete()
    except InstagramAuthRequiredError:
        await status.edit_text(tr(message.from_user.id, "instagram_auth_required"))
    except Exception as exc:
        if is_instagram_auth_error(exc):
            await status.edit_text(tr(message.from_user.id, "instagram_auth_required"))
            return
        if is_tiktok_short_link_error(url, exc):
            await status.edit_text(tr(message.from_user.id, "tiktok_short_link_failed"))
            return
        logging.exception("Failed to download video from %s", url, exc_info=exc)
        await status.edit_text(tr(message.from_user.id, "video_download_failed"))


async def handle_video_link_as_circle(message: Message, url: str) -> None:
    status = await message.answer(tr(message.from_user.id, "circle_started"))
    try:
        video_path, _ = await asyncio.to_thread(download_video_from_url, url, message.from_user.id)
        note_path = await asyncio.to_thread(convert_video_to_note, video_path, message.from_user.id)
        await message.answer_video_note(video_note=FSInputFile(note_path), length=640)
        record_stat("video_circles")
        await status.delete()
    except Exception:
        await status.edit_text(tr(message.from_user.id, "circle_failed"))


async def handle_video_circle(message: Message) -> None:
    video = message.video
    if not video:
        return

    if video.file_size and video.file_size > MAX_VIDEO_SIZE_BYTES:
        size_mb = video.file_size / 1024 / 1024
        await message.answer(
            tr(
                message.from_user.id,
                "video_too_large",
                size_mb=size_mb,
                max_mb=MAX_VIDEO_SIZE_MB,
            )
        )
        return

    status = await message.answer(tr(message.from_user.id, "circle_started"))
    source_ext = os.path.splitext(video.file_name or "")[1] or ".mp4"
    source_path = os.path.join(TEMP_DIR, f"{message.from_user.id}_{video.file_unique_id}_source{source_ext}")

    try:
        await download_telegram_file(video.file_id, source_path)
        note_path = await asyncio.to_thread(convert_video_to_note, source_path, message.from_user.id)
        if os.path.getsize(note_path) > MAX_VIDEO_SIZE_BYTES:
            size_mb = os.path.getsize(note_path) / 1024 / 1024
            await status.edit_text(
                tr(
                    message.from_user.id,
                    "video_too_large",
                    size_mb=size_mb,
                    max_mb=MAX_VIDEO_SIZE_MB,
                )
            )
            return

        await message.answer_video_note(
            video_note=FSInputFile(note_path),
            length=640,
        )
        record_stat("video_circles")
        await status.delete()
    except Exception:
        await status.edit_text(tr(message.from_user.id, "circle_failed"))


def extract_audio_from_video(input_path: str, user_id: int) -> str:
    output_path = os.path.join(DOWNLOADS, f"{user_id}_video_audio.mp3")
    command = [
        FFMPEG_EXE,
        "-y",
        "-i",
        input_path,
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "192k",
        output_path,
    ]
    subprocess.run(command, check=True, capture_output=True)
    return output_path


def trim_video_file(input_path: str, user_id: int, start_ms: int, end_ms: int) -> str:
    output_path = os.path.join(DOWNLOADS, f"{user_id}_trimmed_video.mp4")
    duration = max(0.1, (end_ms - start_ms) / 1000)
    command = [
        FFMPEG_EXE,
        "-y",
        "-ss",
        f"{start_ms / 1000:.2f}",
        "-i",
        input_path,
        "-t",
        f"{duration:.2f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        output_path,
    ]
    subprocess.run(command, check=True, capture_output=True)
    return output_path


async def handle_video_note_to_video(message: Message) -> None:
    video_note = message.video_note
    if not video_note:
        return

    if video_note.file_size and video_note.file_size > MAX_VIDEO_SIZE_BYTES:
        size_mb = video_note.file_size / 1024 / 1024
        await message.answer(
            tr(
                message.from_user.id,
                "video_too_large",
                size_mb=size_mb,
                max_mb=MAX_VIDEO_SIZE_MB,
            )
        )
        return

    status = await message.answer(tr(message.from_user.id, "uncircle_started"))
    source_path = os.path.join(TEMP_DIR, f"{message.from_user.id}_{video_note.file_unique_id}_note_source.mp4")

    try:
        await download_telegram_file(video_note.file_id, source_path)
        video_path = await asyncio.to_thread(convert_note_to_video, source_path, message.from_user.id)
        if os.path.getsize(video_path) > MAX_VIDEO_SIZE_BYTES:
            size_mb = os.path.getsize(video_path) / 1024 / 1024
            await status.edit_text(
                tr(
                    message.from_user.id,
                    "video_too_large",
                    size_mb=size_mb,
                    max_mb=MAX_VIDEO_SIZE_MB,
                )
            )
            return

        await message.answer_video(video=FSInputFile(video_path))
        record_stat("circle_to_video")
        await status.delete()
    except Exception:
        await status.edit_text(tr(message.from_user.id, "uncircle_failed"))


async def download_telegram_file(file_id: str, output_path: str) -> str:
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, output_path)
    return output_path


async def fetch_cover_by_metadata(session: TrackSession) -> Optional[str]:
    query = f"{session.title} {session.performer}".strip()
    if not query:
        return None

    url = "https://itunes.apple.com/search"
    params = {"term": query, "entity": "song", "limit": 1}

    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url, params=params, timeout=20) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        results = data.get("results", [])
        if not results:
            return None
        artwork = results[0].get("artworkUrl100")
        if not artwork:
            return None
        artwork = artwork.replace("100x100bb", "600x600bb")
        async with aiohttp.ClientSession() as http:
            async with http.get(artwork, timeout=20) as resp:
                if resp.status != 200:
                    return None
                image_bytes = await resp.read()
        cover_path = os.path.join(TEMP_DIR, f"{session.user_id}_shazam_cover.jpg")
        ensure_jpeg_cover(image_bytes, cover_path)
        return cover_path
    except Exception:
        return None


def export_processed_track(session: TrackSession) -> tuple[str, bool]:
    safe_name = "".join(ch if ch.isalnum() or ch in " _-" else "_" for ch in session.title).strip() or "track"
    filters = []

    if session.bass_level > 0:
        bass_gain = max(2, min(20, int(round(session.bass_level / 10))))
        filters.append(f"bass=g={bass_gain}:f=110:w=0.6")

    if session.effect_8d > 0:
        width = min(1.0, max(0.2, session.effect_8d / 350.0))
        filters.append(f"apulsator=mode=sine:hz=0.10:width={width:.2f}")

    if abs(session.speed - 1.0) > 0.001:
        filters.append(f"atempo={session.speed}")

    command = [FFMPEG_EXE, "-y"]
    if session.trim_start_ms is not None:
        command.extend(["-ss", f"{session.trim_start_ms / 1000:.2f}"])
    command.extend(["-i", session.track_path])
    if session.trim_end_ms is not None and session.trim_start_ms is not None:
        duration = max(0.1, (session.trim_end_ms - session.trim_start_ms) / 1000)
        command.extend(["-t", f"{duration:.2f}"])

    if filters:
        command.extend(["-af", ",".join(filters)])

    if session.voice_mode:
        output_path = os.path.join(DOWNLOADS, f"{session.user_id}_{safe_name}_voice.ogg")
        command.extend(["-vn", "-c:a", "libopus", "-b:a", "64k", output_path])
        subprocess.run(command, check=True, capture_output=True)
        return output_path, True

    output_path = os.path.join(DOWNLOADS, f"{session.user_id}_{safe_name}_edited.mp3")
    command.extend(["-vn", "-codec:a", "libmp3lame", "-b:a", f"{session.bitrate}k", output_path])
    subprocess.run(command, check=True, capture_output=True)
    return output_path, False


def write_tags_to_mp3(file_path: str, session: TrackSession) -> None:
    try:
        tags = ID3(file_path)
    except ID3NoHeaderError:
        tags = ID3()

    tags.delall("TIT2")
    tags.delall("TPE1")
    tags.delall("TALB")
    tags.add(TIT2(encoding=3, text=session.title))
    tags.add(TPE1(encoding=3, text=session.performer))
    tags.add(TALB(encoding=3, text="Music Bot"))

    if session.cover_path and os.path.exists(session.cover_path):
        with open(session.cover_path, "rb") as cover_file:
            tags.delall("APIC")
            tags.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=cover_file.read(),
                )
            )

    tags.save(file_path)


async def send_processed_track(message: Message, session: TrackSession) -> None:
    output_path, is_voice = export_processed_track(session)

    if is_voice:
        await message.answer_voice(
            voice=FSInputFile(output_path),
        )
        return

    write_tags_to_mp3(output_path, session)
    _, _, duration_seconds, size_mb = await read_audio_metadata(output_path)
    session.duration_seconds = duration_seconds
    session.size_mb = size_mb

    thumbnail_path = os.path.join(TEMP_DIR, f"{session.user_id}_audio_thumb.jpg")
    prepare_audio_thumbnail(get_display_cover_path(session), thumbnail_path)
    send_kwargs = {
        "audio": FSInputFile(output_path),
        "title": session.title,
        "performer": session.performer,
    }
    if session.caption:
        send_kwargs["caption"] = session.caption
        send_kwargs["parse_mode"] = "HTML"

    try:
        await message.answer_audio(
            **send_kwargs,
            thumbnail=FSInputFile(thumbnail_path),
        )
    except Exception:
        await message.answer_audio(**send_kwargs)


async def show_main_menu(target: Message) -> None:
    await target.edit_text(tr(target.chat.id, "start_text"), reply_markup=start_menu(target.chat.id))


async def ensure_admin_reply_keyboard(message: Message) -> None:
    user_id = message.from_user.id
    if not is_admin(user_id) or user_id in admin_reply_keyboard_seeded:
        return
    markup = admin_reply_keyboard(user_id)
    if markup is None:
        return
    await message.answer(tr(user_id, "admin_panel_title", user_count=len(known_users), uptime=format_uptime()), reply_markup=markup, parse_mode="HTML")
    admin_reply_keyboard_seeded.add(user_id)


async def send_admin_panel(message: Message) -> None:
    await message.answer(
        build_admin_panel_text(message.from_user.id),
        reply_markup=admin_reply_keyboard(message.from_user.id),
        parse_mode="HTML",
    )


async def show_saved_items(call: CallbackQuery) -> None:
    files = []
    for name in os.listdir(DOWNLOADS):
        path = os.path.join(DOWNLOADS, name)
        if os.path.isfile(path):
            size_mb = os.path.getsize(path) / 1024 / 1024
            files.append(f"• <code>{escape(name)}</code> ({size_mb:.2f} MB)")

    text = tr(call.from_user.id, "saved_tracks_title") + "\n".join(files[:20]) if files else tr(call.from_user.id, "no_saved_tracks")
    await call.message.edit_text(text, reply_markup=back_to_menu_button(call.from_user.id), parse_mode="HTML")


def build_saved_tracks_text(user_id: int) -> str:
    files = []
    for name in os.listdir(DOWNLOADS):
        path = os.path.join(DOWNLOADS, name)
        if os.path.isfile(path):
            size_mb = os.path.getsize(path) / 1024 / 1024
            files.append(f"• <code>{escape(name)}</code> ({size_mb:.2f} MB)")
    return tr(user_id, "saved_tracks_title") + "\n".join(files[:20]) if files else tr(user_id, "no_saved_tracks")


@dp.message(CommandStart())
async def start_cmd(message: Message) -> None:
    register_user(message.from_user)
    await ensure_admin_reply_keyboard(message)
    await message.answer(tr(message.from_user.id, "start_text"), reply_markup=start_menu(message.from_user.id))


@dp.message(Command("menu"))
async def menu_cmd(message: Message) -> None:
    register_user(message.from_user)
    await ensure_admin_reply_keyboard(message)
    await message.answer(tr(message.from_user.id, "start_text"), reply_markup=start_menu(message.from_user.id))


@dp.message(Command("help"))
async def help_cmd(message: Message) -> None:
    register_user(message.from_user)
    await message.answer(
        tr(message.from_user.id, "how_to_use_text"),
        reply_markup=how_to_use_menu(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(Command("language"))
async def language_cmd(message: Message) -> None:
    register_user(message.from_user)
    await message.answer(
        tr(message.from_user.id, "language_text"),
        reply_markup=language_menu(message.from_user.id),
    )


@dp.message(Command("quicktags"))
async def quicktags_cmd(message: Message) -> None:
    register_user(message.from_user)
    settings = get_quick_settings(message.from_user.id)
    await message.answer(
        quick_tags_text(message.from_user.id, settings),
        reply_markup=quick_tags_menu(message.from_user.id, settings),
        parse_mode="HTML",
    )


@dp.message(Command("saved"))
async def saved_cmd(message: Message) -> None:
    register_user(message.from_user)
    await message.answer(
        build_saved_tracks_text(message.from_user.id),
        reply_markup=back_to_menu_button(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(Command("search"))
async def search_cmd(message: Message) -> None:
    register_user(message.from_user)
    await message.answer(
        tr(message.from_user.id, "search_text"),
        reply_markup=search_music_menu(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(Command("circle"))
async def circle_cmd(message: Message) -> None:
    register_user(message.from_user)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        url = extract_supported_url(parts[1])
        if url:
            await handle_video_link_as_circle(message, url)
            return
    await message.answer(tr(message.from_user.id, "circle_prompt"))


@dp.message(Command("video"))
async def video_cmd(message: Message) -> None:
    register_user(message.from_user)
    await message.answer(tr(message.from_user.id, "uncircle_prompt"))


@dp.message(Command("socialcaption"))
async def social_caption_cmd(message: Message) -> None:
    register_user(message.from_user)
    text = (message.text or "").split(maxsplit=1)
    if len(text) < 2:
        await message.answer(
            tr(message.from_user.id, "video_caption_current", caption=escape(get_social_caption(message.from_user.id))),
            parse_mode="HTML",
        )
        return
    user_social_captions[message.from_user.id] = text[1].strip()
    await message.answer(tr(message.from_user.id, "video_caption_saved"))


@dp.message(Command("stats"))
async def stats_cmd(message: Message) -> None:
    register_user(message.from_user)
    if not is_admin(message.from_user.id):
        await message.answer(tr(message.from_user.id, "admin_only"))
        return
    await message.answer(tr(message.from_user.id, "stats_text", **bot_stats), parse_mode="HTML")


@dp.message(Command("hits"))
async def hits_cmd(message: Message) -> None:
    register_user(message.from_user)
    await message.answer(
        tr(message.from_user.id, "weekly_hits_text"),
        reply_markup=back_to_menu_button(message.from_user.id),
    )


@dp.message(F.audio)
async def get_music(message: Message) -> None:
    register_user(message.from_user)
    audio = message.audio
    if audio.file_size and audio.file_size > MAX_AUDIO_SIZE_BYTES:
        size_mb = round(audio.file_size / 1024 / 1024, 2)
        await message.answer(tr(message.from_user.id, "file_too_large", size_mb=size_mb, max_mb=MAX_AUDIO_SIZE_MB))
        return

    file_name = audio.file_name or f"{audio.file_unique_id}.mp3"
    file_ext = os.path.splitext(file_name)[1] or ".mp3"
    original_path = os.path.join(DOWNLOADS, f"{audio.file_unique_id}{file_ext}")

    await download_telegram_file(audio.file_id, original_path)

    title = audio.title or tr(message.from_user.id, "unknown_title")
    performer = audio.performer or tr(message.from_user.id, "unknown_title")
    duration_seconds = audio.duration
    size_mb = round(audio.file_size / 1024 / 1024, 2)
    cover_path = extract_embedded_cover(original_path, message.from_user.id)
    cover_source = tr(message.from_user.id, "music_cover_source") if cover_path else tr(message.from_user.id, "not_added")
    settings = get_quick_settings(message.from_user.id)

    if settings.title_mode == "blank":
        title = " "
    elif settings.title_mode == "value":
        title = settings.title_value

    if settings.artist_mode == "blank":
        performer = " "
    elif settings.artist_mode == "value":
        performer = settings.artist_value

    bitrate = settings.bitrate or 192
    if settings.cover_path and os.path.exists(settings.cover_path):
        cover_path = settings.cover_path
        cover_source = tr(message.from_user.id, "default_cover_source")

    session = TrackSession(
        track_path=original_path,
        original_file_name=file_name,
        title=title,
        performer=performer,
        duration_seconds=duration_seconds,
        size_mb=size_mb,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        card_message_id=0,
        cover_path=cover_path,
        cover_source=cover_source,
        bitrate=bitrate,
    )
    card = await message.answer_photo(
        photo=FSInputFile(get_display_cover_path(session)),
        caption=format_track_text(session),
        reply_markup=music_menu(message.from_user.id),
        parse_mode="HTML",
    )
    session.card_message_id = card.message_id
    user_sessions[message.from_user.id] = session


@dp.message(F.video)
async def handle_video_message(message: Message) -> None:
    register_user(message.from_user)
    session = get_session(message.from_user.id)
    settings = get_quick_settings(message.from_user.id)
    if session or settings.pending_action:
        return
    video = message.video
    if not video:
        return
    if video.file_size and video.file_size > MAX_VIDEO_SIZE_BYTES:
        size_mb = video.file_size / 1024 / 1024
        await message.answer(
            tr(message.from_user.id, "video_too_large", size_mb=size_mb, max_mb=MAX_VIDEO_SIZE_MB)
        )
        return

    source_ext = os.path.splitext(video.file_name or "")[1] or ".mp4"
    source_path = os.path.join(TEMP_DIR, f"{message.from_user.id}_{video.file_unique_id}_editor{source_ext}")
    await download_telegram_file(video.file_id, source_path)
    video_session = VideoSession(
        video_path=source_path,
        original_file_name=video.file_name or f"{video.file_unique_id}.mp4",
        duration_seconds=video.duration,
        size_mb=(video.file_size or 0) / 1024 / 1024,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        card_message_id=0,
    )
    card = await message.answer(
        format_video_text(video_session),
        reply_markup=video_menu(message.from_user.id),
        parse_mode="HTML",
    )
    video_session.card_message_id = card.message_id
    video_sessions[message.from_user.id] = video_session


@dp.message(F.video_note)
async def handle_video_note_message(message: Message) -> None:
    register_user(message.from_user)
    session = get_session(message.from_user.id)
    settings = get_quick_settings(message.from_user.id)
    if session or settings.pending_action:
        return
    await handle_video_note_to_video(message)


@dp.message(F.photo)
async def handle_photo_input(message: Message) -> None:
    register_user(message.from_user)
    session = get_session(message.from_user.id)
    settings = get_quick_settings(message.from_user.id)

    if session and session.pending_action == "await_photo":
        photo = message.photo[-1]
        photo_path = os.path.join(TEMP_DIR, f"{message.from_user.id}_cover.jpg")
        telegram_path = os.path.join(TEMP_DIR, f"{message.from_user.id}_cover_raw")
        await download_telegram_file(photo.file_id, telegram_path)
        with open(telegram_path, "rb") as source:
            ensure_jpeg_cover(source.read(), photo_path)
        if os.path.exists(telegram_path):
            os.remove(telegram_path)

        session.cover_path = photo_path
        session.cover_source = tr(message.from_user.id, "added")
        session.pending_action = None
        await delete_prompt_if_exists(session)
        await update_track_card(session)
        try:
            await message.delete()
        except Exception:
            pass
        return

    if settings.pending_action == "await_quick_photo":
        photo = message.photo[-1]
        photo_path = os.path.join(TEMP_DIR, f"{message.from_user.id}_quick_cover.jpg")
        telegram_path = os.path.join(TEMP_DIR, f"{message.from_user.id}_quick_cover_raw")
        await download_telegram_file(photo.file_id, telegram_path)
        with open(telegram_path, "rb") as source:
            ensure_jpeg_cover(source.read(), photo_path)
        if os.path.exists(telegram_path):
            os.remove(telegram_path)

        settings.cover_path = photo_path
        settings.pending_action = None
        await message.answer(
            quick_tags_text(message.from_user.id, settings),
            reply_markup=quick_tags_menu(message.from_user.id, settings),
            parse_mode="HTML",
        )
        try:
            await message.delete()
        except Exception:
            pass
        return


@dp.message(F.document)
async def handle_document_input(message: Message) -> None:
    register_user(message.from_user)
    document = message.document
    if not document:
        return

    file_name = (document.file_name or "").strip()
    if file_name.lower() != "instagram_cookies.txt" and not looks_like_instagram_cookies_file(file_name):
        await message.answer(tr(message.from_user.id, "instagram_cookies_invalid"))
        return

    await download_telegram_file(document.file_id, INSTAGRAM_COOKIES_FILE)
    await message.answer(tr(message.from_user.id, "instagram_cookies_saved"), parse_mode="HTML")


@dp.message(F.text)
async def handle_text_input(message: Message) -> None:
    register_user(message.from_user)
    session = get_session(message.from_user.id)
    video_session = video_sessions.get(message.from_user.id)
    settings = get_quick_settings(message.from_user.id)
    text = message.text.strip()
    admin_panel_buttons = {TRANSLATIONS[lang]["admin_reply_btn"] for lang in TRANSLATIONS}
    admin_stats_buttons = {TRANSLATIONS[lang]["admin_stats_btn"] for lang in TRANSLATIONS}
    admin_users_buttons = {TRANSLATIONS[lang]["admin_users_btn"] for lang in TRANSLATIONS}
    admin_files_buttons = {TRANSLATIONS[lang]["admin_files_btn"] for lang in TRANSLATIONS}
    if is_admin(message.from_user.id) and text in admin_panel_buttons:
        await send_admin_panel(message)
        return
    if is_admin(message.from_user.id) and text in admin_stats_buttons:
        await message.answer(
            tr(message.from_user.id, "stats_text", **bot_stats),
            reply_markup=admin_reply_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
        return
    if is_admin(message.from_user.id) and text in admin_users_buttons:
        await message.answer(
            build_admin_users_text(message.from_user.id),
            reply_markup=admin_reply_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
        return
    if is_admin(message.from_user.id) and text in admin_files_buttons:
        await message.answer(
            build_admin_files_text(message.from_user.id),
            reply_markup=admin_reply_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
        return
    media_url = extract_supported_url(text)
    if not session and not video_session and not settings.pending_action:
        if media_url:
            await handle_video_link(message, media_url)
        return

    if session and session.pending_action == "await_tags":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            await message.answer(tr(message.from_user.id, "send_at_least_title"))
            return
        session.title = lines[0]
        if len(lines) > 1:
            session.performer = lines[1]
        session.pending_action = None
        await delete_prompt_if_exists(session)
        await update_track_card(session)
        try:
            await message.delete()
        except Exception:
            pass
        return

    if session and session.pending_action == "await_trim":
        try:
            raw_start, raw_end = [part.strip() for part in text.replace("–", "-").split("-", 1)]
            start_seconds = parse_mmss(raw_start)
            end_seconds = parse_mmss(raw_end)
            if start_seconds < 0 or end_seconds <= start_seconds or end_seconds > session.duration_seconds:
                raise ValueError
        except Exception:
            await message.answer(tr(message.from_user.id, "invalid_trim_format"))
            return

        session.trim_start_ms = start_seconds * 1000
        session.trim_end_ms = end_seconds * 1000
        session.pending_action = None
        await delete_prompt_if_exists(session)
        await update_track_card(session)
        try:
            await message.delete()
        except Exception:
            pass
        return

    if session and session.pending_action == "await_caption":
        session.caption = text
        session.pending_action = None
        await delete_prompt_if_exists(session)
        await update_track_card(session)
        try:
            await message.delete()
        except Exception:
            pass
        return

    if video_session and video_session.pending_action == "await_video_trim":
        try:
            raw_start, raw_end = [part.strip() for part in text.replace("–", "-").split("-", 1)]
            start_seconds = parse_mmss(raw_start)
            end_seconds = parse_mmss(raw_end)
            if start_seconds < 0 or end_seconds <= start_seconds or end_seconds > video_session.duration_seconds:
                raise ValueError
        except Exception:
            await message.answer(tr(message.from_user.id, "invalid_trim_format"))
            return

        video_session.trim_start_ms = start_seconds * 1000
        video_session.trim_end_ms = end_seconds * 1000
        video_session.pending_action = None
        await delete_video_prompt_if_exists(video_session)
        await bot.edit_message_text(
            chat_id=video_session.chat_id,
            message_id=video_session.card_message_id,
            text=format_video_text(video_session),
            reply_markup=video_menu(message.from_user.id),
            parse_mode="HTML",
        )
        try:
            await message.delete()
        except Exception:
            pass
        return

    if settings.pending_action == "await_quick_title":
        settings.title_mode = "value"
        settings.title_value = text
        settings.pending_action = None
        await message.answer(
            quick_tags_text(message.from_user.id, settings),
            reply_markup=quick_tags_menu(message.from_user.id, settings),
            parse_mode="HTML",
        )
        try:
            await message.delete()
        except Exception:
            pass
        return

    if settings.pending_action == "await_quick_artist":
        settings.artist_mode = "value"
        settings.artist_value = text
        settings.pending_action = None
        await message.answer(
            quick_tags_text(message.from_user.id, settings),
            reply_markup=quick_tags_menu(message.from_user.id, settings),
            parse_mode="HTML",
        )
        try:
            await message.delete()
        except Exception:
            pass
        return


@dp.callback_query()
async def callbacks(call: CallbackQuery) -> None:
    data = call.data
    user_id = call.from_user.id
    register_user(call.from_user)
    session = get_session(user_id)
    video_session = video_sessions.get(user_id)
    settings = get_quick_settings(user_id)
    answered = False

    if data == "admin_panel":
        if not is_admin(user_id):
            await call.answer(tr(user_id, "admin_only"), show_alert=True)
            answered = True
        else:
            await call.message.delete()
            await call.message.answer(
                build_admin_panel_text(user_id),
                reply_markup=admin_reply_keyboard(user_id),
                parse_mode="HTML",
            )
    elif data == "admin_stats":
        if not is_admin(user_id):
            await call.answer(tr(user_id, "admin_only"), show_alert=True)
            answered = True
        else:
            await call.message.delete()
            await call.message.answer(
                tr(user_id, "stats_text", **bot_stats),
                reply_markup=admin_reply_keyboard(user_id),
                parse_mode="HTML",
            )
    elif data == "admin_users":
        if not is_admin(user_id):
            await call.answer(tr(user_id, "admin_only"), show_alert=True)
            answered = True
        else:
            await call.message.delete()
            await call.message.answer(
                build_admin_users_text(user_id),
                reply_markup=admin_reply_keyboard(user_id),
                parse_mode="HTML",
            )
    elif data == "admin_files":
        if not is_admin(user_id):
            await call.answer(tr(user_id, "admin_only"), show_alert=True)
            answered = True
        else:
            await call.message.delete()
            await call.message.answer(
                build_admin_files_text(user_id),
                reply_markup=admin_reply_keyboard(user_id),
                parse_mode="HTML",
            )
    elif data == "admin_close":
        if not is_admin(user_id):
            await call.answer(tr(user_id, "admin_only"), show_alert=True)
            answered = True
        else:
            await call.message.delete()
            await call.message.answer(
                build_admin_panel_text(user_id),
                reply_markup=admin_reply_keyboard(user_id),
                parse_mode="HTML",
            )
    elif data == "main_menu":
        await show_main_menu(call.message)
    elif data == "quick_tags":
        settings.pending_action = None
        await call.message.edit_text(quick_tags_text(user_id, settings), reply_markup=quick_tags_menu(user_id, settings), parse_mode="HTML")
    elif data == "quick_photo":
        settings.pending_action = None
        await call.message.edit_text(tr(user_id, "quick_photo_text"), reply_markup=quick_photo_menu(user_id), parse_mode="HTML")
    elif data == "quick_photo_set":
        settings.pending_action = "await_quick_photo"
        await call.answer(tr(user_id, "send_one_photo_alert"), show_alert=True)
        answered = True
    elif data == "quick_photo_delete":
        settings.cover_path = None
        settings.pending_action = None
        await call.message.edit_text(quick_tags_text(user_id, settings), reply_markup=quick_tags_menu(user_id, settings), parse_mode="HTML")
    elif data == "quick_title":
        settings.pending_action = None
        await call.message.edit_text(tr(user_id, "quick_title_text"), reply_markup=quick_title_menu(user_id), parse_mode="HTML")
    elif data == "quick_title_set":
        settings.pending_action = "await_quick_title"
        await call.answer(tr(user_id, "send_title_alert"), show_alert=True)
        answered = True
    elif data == "quick_title_blank":
        settings.title_mode = "blank"
        settings.title_value = ""
        settings.pending_action = None
        await call.message.edit_text(quick_tags_text(user_id, settings), reply_markup=quick_tags_menu(user_id, settings), parse_mode="HTML")
    elif data == "quick_title_keep":
        settings.title_mode = "keep"
        settings.title_value = ""
        settings.pending_action = None
        await call.message.edit_text(quick_tags_text(user_id, settings), reply_markup=quick_tags_menu(user_id, settings), parse_mode="HTML")
    elif data == "quick_artist":
        settings.pending_action = None
        await call.message.edit_text(tr(user_id, "quick_artist_text"), reply_markup=quick_artist_menu(user_id), parse_mode="HTML")
    elif data == "quick_artist_set":
        settings.pending_action = "await_quick_artist"
        await call.answer(tr(user_id, "send_artist_alert"), show_alert=True)
        answered = True
    elif data == "quick_artist_blank":
        settings.artist_mode = "blank"
        settings.artist_value = ""
        settings.pending_action = None
        await call.message.edit_text(quick_tags_text(user_id, settings), reply_markup=quick_tags_menu(user_id, settings), parse_mode="HTML")
    elif data == "quick_artist_keep":
        settings.artist_mode = "keep"
        settings.artist_value = ""
        settings.pending_action = None
        await call.message.edit_text(quick_tags_text(user_id, settings), reply_markup=quick_tags_menu(user_id, settings), parse_mode="HTML")
    elif data == "quick_bitrate":
        settings.pending_action = None
        await call.message.edit_text(tr(user_id, "quick_bitrate_text"), reply_markup=quick_bitrate_menu(user_id), parse_mode="HTML")
    elif data.startswith("quick_bitrate_value:"):
        settings.bitrate = int(data.split(":", 1)[1])
        settings.pending_action = None
        await call.message.edit_text(quick_tags_text(user_id, settings), reply_markup=quick_tags_menu(user_id, settings), parse_mode="HTML")
    elif data == "quick_bitrate_keep":
        settings.bitrate = None
        settings.pending_action = None
        await call.message.edit_text(quick_tags_text(user_id, settings), reply_markup=quick_tags_menu(user_id, settings), parse_mode="HTML")
    elif data == "how_to_use":
        await call.message.edit_text(tr(user_id, "how_to_use_text"), reply_markup=how_to_use_menu(user_id), parse_mode="HTML")
    elif data in {"guide_tags", "guide_quick_tags", "guide_cover", "guide_voice", "guide_cut", "guide_other", "guide_search"}:
        await call.answer(tr(user_id, data), show_alert=True)
        answered = True
    elif data == "change_language":
        await call.message.edit_text(tr(user_id, "language_text"), reply_markup=language_menu(user_id))
    elif data == "lang_ru":
        if get_user_lang(user_id) == "ru":
            await call.answer(tr(user_id, "russian_already_selected"), show_alert=True)
            answered = True
        else:
            user_languages[user_id] = "ru"
            await call.message.edit_text(tr(user_id, "language_text"), reply_markup=language_menu(user_id))
            await call.answer(tr(user_id, "language_changed_ru"), show_alert=True)
            answered = True
    elif data == "lang_en":
        user_languages[user_id] = "en"
        await call.message.edit_text(tr(user_id, "language_text"), reply_markup=language_menu(user_id))
        await call.answer(tr(user_id, "language_changed_en"), show_alert=True)
        answered = True
    elif data == "lang_uz":
        user_languages[user_id] = "uz"
        await call.message.edit_text(tr(user_id, "language_text"), reply_markup=language_menu(user_id))
        await call.answer(tr(user_id, "language_changed_uz"), show_alert=True)
        answered = True
    elif data == "saved_items":
        await show_saved_items(call)
    elif data == "weekly_hits":
        await call.message.edit_text(tr(user_id, "weekly_hits_text"), reply_markup=back_to_menu_button(user_id))
    elif data == "search_music":
        await call.message.edit_text(tr(user_id, "search_text"), reply_markup=search_music_menu(user_id), parse_mode="HTML")
    elif data == "video_close" and video_session:
        await delete_video_prompt_if_exists(video_session)
        try:
            await bot.delete_message(video_session.chat_id, video_session.card_message_id)
        except Exception:
            pass
        video_sessions.pop(user_id, None)
    elif data == "video_trim" and video_session:
        video_session.pending_action = "await_video_trim"
        await send_video_prompt(call, video_session, tr(user_id, "video_trim_prompt"), cancel_menu(user_id), parse_mode="HTML")
    elif data == "video_circle" and video_session:
        await delete_video_prompt_if_exists(video_session)
        status = await call.message.answer(tr(user_id, "circle_started"))
        try:
            note_path = await asyncio.to_thread(convert_video_to_note, video_session.video_path, user_id)
            await call.message.answer_video_note(video_note=FSInputFile(note_path), length=640)
            record_stat("video_circles")
            await status.delete()
        except Exception:
            await status.edit_text(tr(user_id, "circle_failed"))
    elif data == "video_mp3" and video_session:
        await delete_video_prompt_if_exists(video_session)
        status = await call.message.answer(tr(user_id, "video_mp3_started"))
        try:
            audio_path = await asyncio.to_thread(extract_audio_from_video, video_session.video_path, user_id)
            await call.message.answer_audio(audio=FSInputFile(audio_path))
            record_stat("video_mp3")
            await status.delete()
        except Exception:
            await status.edit_text(tr(user_id, "video_mp3_failed"))
    elif data == "video_send" and video_session:
        await delete_video_prompt_if_exists(video_session)
        status = await call.message.answer(tr(user_id, "video_ready"))
        try:
            video_path = video_session.video_path
            if video_session.trim_start_ms is not None and video_session.trim_end_ms is not None:
                status = await call.message.answer(tr(user_id, "video_trim_started"))
                video_path = await asyncio.to_thread(
                    trim_video_file,
                    video_session.video_path,
                    user_id,
                    video_session.trim_start_ms,
                    video_session.trim_end_ms,
                )
                record_stat("video_trims")
            await call.message.answer_video(video=FSInputFile(video_path))
            await status.delete()
        except Exception:
            await status.edit_text(tr(user_id, "video_trim_failed"))
    elif not session:
        await call.answer(tr(user_id, "send_music_first"), show_alert=True)
        answered = True
    elif data == "tags":
        session.pending_action = "await_tags"
        await send_prompt(call, session, tr(user_id, "tags_prompt"), cancel_menu(user_id))
    elif data == "photo":
        session.pending_action = "await_photo"
        await send_prompt(call, session, tr(user_id, "photo_prompt"), cancel_menu(user_id), parse_mode=None)
    elif data == "bass":
        await send_prompt(call, session, tr(user_id, "choose_bass"), choice_menu(user_id, "bass_level", ["0", "25", "50", "75", "100", "125", "150", "175", "200"]), parse_mode=None)
    elif data.startswith("bass_level:"):
        session.bass_level = int(data.split(":", 1)[1])
        await delete_prompt_if_exists(session)
        await update_track_card(session)
    elif data == "8d":
        await send_prompt(call, session, tr(user_id, "choose_8d"), choice_menu(user_id, "effect8d", ["50", "100", "150", "200", "300", "350"]), parse_mode=None)
    elif data.startswith("effect8d:"):
        session.effect_8d = int(data.split(":", 1)[1])
        await delete_prompt_if_exists(session)
        await update_track_card(session)
    elif data == "speed":
        await send_prompt(call, session, tr(user_id, "choose_speed"), choice_menu(user_id, "speed_value", ["0.5", "0.8", "1.0", "1.2", "1.5", "2.0"]), parse_mode=None)
    elif data.startswith("speed_value:"):
        session.speed = float(data.split(":", 1)[1])
        await delete_prompt_if_exists(session)
        await update_track_card(session)
    elif data == "bitrate":
        await send_prompt(call, session, tr(user_id, "choose_bitrate"), choice_menu(user_id, "bitrate_value", ["64", "128", "160", "192", "256", "320"]), parse_mode=None)
    elif data.startswith("bitrate_value:"):
        session.bitrate = int(data.split(":", 1)[1])
        await delete_prompt_if_exists(session)
        await update_track_card(session)
    elif data == "cut":
        session.pending_action = "await_trim"
        await send_prompt(call, session, tr(user_id, "trim_prompt"), cancel_menu(user_id))
    elif data == "voice":
        session.voice_mode = True
        await update_track_card(session)
    elif data == "caption":
        session.pending_action = "await_caption"
        await send_prompt(call, session, tr(user_id, "caption_prompt"), caption_menu(user_id))
    elif data == "caption:none":
        session.caption = ""
        session.pending_action = None
        await delete_prompt_if_exists(session)
        await update_track_card(session)
    elif data == "shazam":
        cover_path = await fetch_cover_by_metadata(session)
        if cover_path:
            session.cover_path = cover_path
            session.cover_source = tr(user_id, "search_cover_source")
            await update_track_card(session)
        else:
            await call.answer(tr(user_id, "shazam_cover_not_found"), show_alert=True)
            answered = True
    elif data == "save_track":
        await delete_prompt_if_exists(session)
        await send_processed_track(call.message, session)
        record_stat("audio_edits")
        try:
            await bot.delete_message(session.chat_id, session.card_message_id)
        except Exception:
            pass
        user_sessions.pop(user_id, None)
    elif data == "cancel_action":
        if session:
            session.pending_action = None
            await delete_prompt_if_exists(session)
        if video_session:
            video_session.pending_action = None
            await delete_video_prompt_if_exists(video_session)
        await call.message.edit_text(tr(user_id, "action_cancelled"), reply_markup=back_to_menu_button(user_id))
    else:
        await call.answer(tr(user_id, "unknown_command"), show_alert=True)
        answered = True

    if not answered:
        await call.answer()


async def main() -> None:
    print("Bot started...")
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="menu", description="Открыть главное меню"),
            BotCommand(command="help", description="Как пользоваться ботом"),
            BotCommand(command="language", description="Сменить язык"),
            BotCommand(command="quicktags", description="Быстрые теги"),
            BotCommand(command="saved", description="Сохраненные треки"),
            BotCommand(command="search", description="Поиск музыки"),
            BotCommand(command="circle", description="Сделать кружочек из видео"),
            BotCommand(command="video", description="Сделать видео из кружочка"),
            BotCommand(command="socialcaption", description="Подпись для соцвидео"),
            BotCommand(command="stats", description="Статистика бота"),
            BotCommand(command="hits", description="Хиты недели"),
        ]
    )
    try:
        await bot.get_updates(offset=-1, timeout=1, allowed_updates=[])
    except TelegramConflictError:
        print("Another bot instance is already using this token. Stop the other copy and run only one music.py process.")
        return

    while True:
        try:
            await dp.start_polling(bot, handle_signals=False, close_bot_session=False)
        except asyncio.CancelledError:
            await asyncio.sleep(1)
            continue
        except Exception:
            await asyncio.sleep(2)
            continue
        await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
