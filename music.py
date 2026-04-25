# music.py
# Telegram Music Bot (Aiogram 3.x)

import asyncio
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from html import escape
from io import BytesIO
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramConflictError
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from imageio_ffmpeg import get_ffmpeg_exe
from mutagen import File as MutagenFile
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TIT2, TPE1
from mutagen.mp3 import MP3
from PIL import Image, ImageDraw
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

TOKEN = "8307283859:AAFuR9_rKnAUnnbJseTTUb3oAKmIXu8dVxc"

bot = Bot(TOKEN)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)
logging.disable(logging.CRITICAL)

DOWNLOADS = "downloads"
TEMP_DIR = os.path.join(DOWNLOADS, "temp")
DEFAULT_COVER_PATH = os.path.join(TEMP_DIR, "default_cover.jpg")
INSTAGRAM_COOKIES_FILE = os.path.join(os.getcwd(), "instagram_cookies.txt")
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


user_sessions: dict[int, TrackSession] = {}
quick_tag_settings: dict[int, QuickTagSettings] = {}
user_languages: dict[int, str] = {}


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
        "instagram_auth_required": "❌ Instagram не отдал это видео без входа в аккаунт.\nЕсли ссылка не скачивается, отправьте в бот файл `instagram_cookies.txt` и попробуйте снова.",
        "instagram_cookies_saved": "✅ Файл `instagram_cookies.txt` сохранён. Теперь отправьте ссылку ещё раз.",
        "instagram_cookies_invalid": "❌ Отправьте файл именно с именем `instagram_cookies.txt`.",
        "video_too_large": "❌ Видео получилось слишком большим: {size_mb:.2f} MB.\nЯ могу отправлять только файлы до {max_mb} MB.",
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
        "instagram_auth_required": "❌ Instagram didn't return this video without login.\nIf this reel doesn't download, send an `instagram_cookies.txt` file to the bot and try again.",
        "instagram_cookies_saved": "✅ The `instagram_cookies.txt` file was saved. Now send the link again.",
        "instagram_cookies_invalid": "❌ Please send a file named exactly `instagram_cookies.txt`.",
        "video_too_large": "❌ The downloaded video is too large: {size_mb:.2f} MB.\nI can send files only up to {max_mb} MB.",
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
        "instagram_auth_required": "❌ Instagram bu videoni akkauntga kirmasdan bermadi.\nAgar havola yuklanmasa, botga `instagram_cookies.txt` faylini yuboring va yana urinib ko'ring.",
        "instagram_cookies_saved": "✅ `instagram_cookies.txt` fayli saqlandi. Endi havolani yana yuboring.",
        "instagram_cookies_invalid": "❌ Aynan `instagram_cookies.txt` nomli fayl yuboring.",
        "video_too_large": "❌ Yuklangan video juda katta: {size_mb:.2f} MB.\nMen faqat {max_mb} MB gacha fayl yubora olaman.",
        "lang_ru": "Ruscha", "lang_en": "English", "lang_uz": "O'zbek",
    },
}


def get_user_lang(user_id: int) -> str:
    return user_languages.get(user_id, "ru")


def tr(user_id: int, key: str, **kwargs) -> str:
    lang = get_user_lang(user_id)
    text = TRANSLATIONS.get(lang, TRANSLATIONS["ru"]).get(key, TRANSLATIONS["ru"].get(key, key))
    return text.format(**kwargs) if kwargs else text


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
    me = await bot.me()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🤖 @{me.username}", url=await get_bot_link())],
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
    cleaned = urlunsplit((*urlsplit(url)[:3], "", ""))
    if cleaned != url:
        candidates.append(cleaned)
    return candidates


def build_yt_dlp_options(user_id: int, platform: str) -> dict:
    outtmpl = os.path.join(TEMP_DIR, f"{user_id}_%(extractor)s_%(id)s.%(ext)s")
    referer = "https://www.tiktok.com/" if platform == "tiktok" else "https://www.instagram.com/"
    return {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": SilentYtDlpLogger(),
        "merge_output_format": "mp4",
        "format": "bv*+ba/b[ext=mp4]/b",
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "Referer": referer,
        },
    }


def is_instagram_auth_error(exc: Exception) -> bool:
    error_msg = str(exc).lower()
    return any(
        marker in error_msg
        for marker in (
            "instagram sent an empty media response",
            "login required",
            "requested content is not available",
            "cookies-from-browser",
            "authentication",
        )
    )


def iter_ydl_options(user_id: int, url: str) -> list[dict]:
    platform = get_video_platform(url)
    options = [build_yt_dlp_options(user_id, platform)]
    if platform != "instagram":
        return options
    if os.path.exists(INSTAGRAM_COOKIES_FILE):
        cookie_opts = dict(options[0])
        cookie_opts["cookiefile"] = INSTAGRAM_COOKIES_FILE
        options.insert(0, cookie_opts)
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
                raise
            except Exception as exc:
                last_error = exc
                raise

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


async def handle_video_link(message: Message, url: str) -> None:
    status = await message.answer(tr(message.from_user.id, "video_download_started"))
    try:
        video_path, title = await asyncio.to_thread(download_video_from_url, url, message.from_user.id)
        if not os.path.exists(video_path):
            raise FileNotFoundError(video_path)

        video_path = await asyncio.to_thread(normalize_video_for_telegram, video_path, message.from_user.id)
        if not os.path.exists(video_path):
            raise FileNotFoundError(video_path)

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

        await message.answer_video(
            video=FSInputFile(video_path),
            caption=escape(title),
            parse_mode="HTML",
            reply_markup=await social_result_menu(),
        )
        await status.delete()
    except InstagramAuthRequiredError:
        await status.edit_text(tr(message.from_user.id, "instagram_auth_required"))
    except Exception as exc:
        if is_instagram_auth_error(exc):
            await status.edit_text(tr(message.from_user.id, "instagram_auth_required"))
            return
        logging.exception("Failed to download video from %s", url, exc_info=exc)
        await status.edit_text(tr(message.from_user.id, "video_download_failed"))


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

    thumb = FSInputFile(get_display_cover_path(session))
    await message.answer_audio(
        audio=FSInputFile(output_path),
        title=session.title,
        performer=session.performer,
        thumbnail=thumb,
    )


async def show_main_menu(target: Message) -> None:
    await target.edit_text(tr(target.chat.id, "start_text"), reply_markup=start_menu(target.chat.id))


async def show_saved_items(call: CallbackQuery) -> None:
    files = []
    for name in os.listdir(DOWNLOADS):
        path = os.path.join(DOWNLOADS, name)
        if os.path.isfile(path):
            size_mb = os.path.getsize(path) / 1024 / 1024
            files.append(f"• <code>{escape(name)}</code> ({size_mb:.2f} MB)")

    text = tr(call.from_user.id, "saved_tracks_title") + "\n".join(files[:20]) if files else tr(call.from_user.id, "no_saved_tracks")
    await call.message.edit_text(text, reply_markup=back_to_menu_button(call.from_user.id), parse_mode="HTML")


@dp.message(CommandStart())
async def start_cmd(message: Message) -> None:
    await message.answer(tr(message.from_user.id, "start_text"), reply_markup=start_menu(message.from_user.id))


@dp.message(F.audio)
async def get_music(message: Message) -> None:
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


@dp.message(F.photo)
async def handle_photo_input(message: Message) -> None:
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
    document = message.document
    if not document:
        return

    file_name = (document.file_name or "").strip()
    if file_name.lower() != "instagram_cookies.txt":
        await message.answer(tr(message.from_user.id, "instagram_cookies_invalid"))
        return

    await download_telegram_file(document.file_id, INSTAGRAM_COOKIES_FILE)
    await message.answer(tr(message.from_user.id, "instagram_cookies_saved"), parse_mode="HTML")


@dp.message(F.text)
async def handle_text_input(message: Message) -> None:
    session = get_session(message.from_user.id)
    settings = get_quick_settings(message.from_user.id)
    text = message.text.strip()
    media_url = extract_supported_url(text)
    if not session and not settings.pending_action:
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
    session = get_session(user_id)
    settings = get_quick_settings(user_id)
    answered = False

    if data == "main_menu":
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
        try:
            await bot.delete_message(session.chat_id, session.card_message_id)
        except Exception:
            pass
        user_sessions.pop(user_id, None)
    elif data == "cancel_action":
        session.pending_action = None
        await delete_prompt_if_exists(session)
        await call.message.edit_text(tr(user_id, "action_cancelled"), reply_markup=back_to_menu_button(user_id))
    else:
        await call.answer(tr(user_id, "unknown_command"), show_alert=True)
        answered = True

    if not answered:
        await call.answer()


async def main() -> None:
    print("Bot started...")
    await bot.delete_webhook(drop_pending_updates=False)
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
