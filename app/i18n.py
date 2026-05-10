"""Lightweight i18n for SMDL.

Two-language design (en, ru). String catalog lives inline — small enough
that a separate .po toolchain isn't worth the dependency. Per-chat
preference persisted to /data/lang.json.

Usage:
    from .i18n import t, get_lang, set_lang, SUPPORTED_LANGS
    msg = t("download_failed", get_lang(chat_id), error=str(e))

Missing-key fallback: returns the key itself (so untranslated strings
are visible in logs/UI rather than crashing).
Missing-language fallback: falls back to English.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

LANG_FILE = Path(os.environ.get("LANG_FILE", "/data/lang.json"))
SUPPORTED_LANGS = ("en", "ru")
DEFAULT_LANG = "en"

LANG_LABELS = {
    "en": "English",
    "ru": "Русский",
}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        # Generic
        "owner_only":           "Owner only.",
        "error_generic":        "Error: {error}",

        # /language
        "lang_picker":          "Choose your language:",
        "lang_set_en":          "Language set to English.",
        "lang_set_ru":          "Language set to Russian.",
        "lang_unknown":         "Unknown language: {lang}. Supported: {supported}",

        # URL identification
        "identifying":          "Identifying {platform} post...",
        "identify_failed":      "Failed to identify post: {error}",
        "private_account":      "Private account — cannot download.",
        "could_not_identify":   "Could not identify post: {error}",

        # Live recording — manual
        "live_disabled":        "{platform} · @{uploader} · 🔴 LIVE\nLive recording is disabled in config (live_enabled=false).",
        "live_site_unsupported": "{platform} · 🔴 LIVE\n⚠ Site not supported / not configured yet (yt-dlp can't extract a live stream from this URL after {budget} attempts).\n\nIf you think this should work, the site may need a yt-dlp extractor update or cookies.",
        "live_started":         "{platform} · @{uploader} · 🔴 LIVE\nRecording started — heartbeats every 5 min. Will auto-stop on stream end or session failure.",
        "live_progress":        "🔴 Recording · @{uploader}\n⏱ {duration} · 💾 {mb:.1f} MB",
        "live_ended_natural":   "✓ Recording ended naturally · {mins} min · {mb:.0f} MB",
        "live_user_stopped":    "⏹ Stopped by /stop_livestream · {mins} min · {mb:.0f} MB saved",
        "live_session_fail":    "⚠ Session/auth failed at {mins} min · {mb:.0f} MB saved\nCookie likely expired — refresh cookies and retry.",
        "live_no_extractor_final": "⚠ Site not supported / not configured yet.\nyt-dlp couldn't extract a live stream from this URL after {attempts} attempts.",
        "live_no_extractor_retry": "⚠ yt-dlp couldn't extract this URL ({attempts}/{budget} attempts)\nTry again — {remaining} more attempts before site is marked as not configured.",
        "live_platform_not_allowed": "⚠ {detail}",
        "live_disk_low":        "⚠ {detail}",
        "live_other_abort":     "⚠ Stopped: {reason} · {mins} min · {mb:.0f} MB · {detail}",

        # /stop_livestream + /live_status
        "no_active_live":       "No active livestream recording in this chat.",
        "no_active_live_short": "No active livestream recording.",
        "stop_requested":       "⏹ Stop requested for {platform} · @{uploader} ({duration} in). Finalizing the file…",
        "live_status_active":   "🔴 Recording · {platform} · @{uploader}\n⏱ {duration} · use /stop_livestream to halt",

        # Normal download
        "downloading":          "{platform} · @{uploader} · {media_label}\nDownloading...",
        "download_failed":      "Download failed: {error}",
        "sending_files":        "{prefix}ending {count} files...",
        "sending_one":          "{prefix}ending {platform} {media_label}...",
        "sent_short":           "Sent ({detail})",
        "uploading_telethon":   "📤 Uploading {size_mb} MB via user account…",
        "uploaded_telethon":    "✓ Uploaded ({size_mb} MB)",
        "send_failed":          "Send failed: {error}",

        # Delivery links
        "file_ready":           "📁 File ready · {size_mb:.0f} MB",
        "tailnet_link":         "🔒 Tailnet (you, on mesh):\n{url}",
        "share_link":           "🌍 Share link (anyone, expires in {hours}h):\n{url}",
        "no_delivery":          "⚠ No delivery method configured. File is at /downloads/{rel}",

        # /watch /unwatch /watchlist
        "watch_usage":          "Usage: /watch <url>\nExample: /watch https://twitch.tv/some_streamer",
        "unwatch_usage":        "Usage: /unwatch <url>",
        "watch_already":        "Already watching {url}",
        "watch_added":          "Now watching {url}",
        "watch_not_found":      "Not in watchlist: {url}",
        "watch_removed":        "Removed {url}",
        "watchlist_empty":      "Watchlist is empty.\nAdd one with: /watch <url>",
        "watchlist_header":     "📺 Watchlist ({count})",

        # Stream monitor live notification
        "monitor_live_prompt":  "🔴 LIVE — {uploader}\n{title}\n\n{url}\n\nRecord this stream?",
        "btn_yes_record":       "✅ Yes — Record",
        "btn_skip":             "❌ Skip",
        "btn_lang_en":          "English",
        "btn_lang_ru":          "Русский",
        "monitor_skipped":      "⏭ Skipped",
        "monitor_starting":     "🎬 Recording starting…",
        "monitor_record_starting": "🔴 Recording · @{uploader}\nStarting…",
        "monitor_recording_crashed": "⚠ Recording crashed: {error}",
        "btn_snooze_1h":        "💤 1h",
        "btn_snooze_8h":        "😴 8h",
        "monitor_snoozed":      "💤 Snoozed for {duration} (until {until})",
    },
    "ru": {
        # Generic
        "owner_only":           "Только для владельца.",
        "error_generic":        "Ошибка: {error}",

        # /language
        "lang_picker":          "Выберите язык:",
        "lang_set_en":          "Язык изменён на английский.",
        "lang_set_ru":          "Язык изменён на русский.",
        "lang_unknown":         "Неизвестный язык: {lang}. Поддерживаются: {supported}",

        # URL identification
        "identifying":          "Анализирую публикацию {platform}...",
        "identify_failed":      "Не удалось проанализировать публикацию: {error}",
        "private_account":      "Закрытый аккаунт — скачивание невозможно.",
        "could_not_identify":   "Не удалось распознать публикацию: {error}",

        # Live recording — manual
        "live_disabled":        "{platform} · @{uploader} · 🔴 ЭФИР\nЗапись эфиров отключена в настройках (live_enabled=false).",
        "live_site_unsupported": "{platform} · 🔴 ЭФИР\n⚠ Сайт не поддерживается / ещё не настроен (yt-dlp не смог извлечь поток после {budget} попыток).\n\nЕсли это должно работать — возможно, нужно обновить yt-dlp или cookies.",
        "live_started":         "{platform} · @{uploader} · 🔴 ЭФИР\nЗапись началась — обновления каждые 5 минут. Автоматически остановится при завершении эфира или ошибке сессии.",
        "live_progress":        "🔴 Запись · @{uploader}\n⏱ {duration} · 💾 {mb:.1f} МБ",
        "live_ended_natural":   "✓ Запись завершилась естественно · {mins} мин · {mb:.0f} МБ",
        "live_user_stopped":    "⏹ Остановлено через /stop_livestream · {mins} мин · {mb:.0f} МБ сохранено",
        "live_session_fail":    "⚠ Ошибка сессии/авторизации на {mins} мин · {mb:.0f} МБ сохранено\nВероятно, истёк срок cookie — обновите cookie и повторите.",
        "live_no_extractor_final": "⚠ Сайт не поддерживается / ещё не настроен.\nyt-dlp не смог извлечь эфир после {attempts} попыток.",
        "live_no_extractor_retry": "⚠ yt-dlp не смог извлечь эту ссылку ({attempts}/{budget} попыток)\nПопробуйте снова — осталось {remaining} попыток.",
        "live_platform_not_allowed": "⚠ {detail}",
        "live_disk_low":        "⚠ {detail}",
        "live_other_abort":     "⚠ Остановлено: {reason} · {mins} мин · {mb:.0f} МБ · {detail}",

        # /stop_livestream + /live_status
        "no_active_live":       "В этом чате нет активной записи эфира.",
        "no_active_live_short": "Нет активной записи эфира.",
        "stop_requested":       "⏹ Запрошена остановка {platform} · @{uploader} ({duration} в эфире). Завершаю файл…",
        "live_status_active":   "🔴 Запись · {platform} · @{uploader}\n⏱ {duration} · /stop_livestream чтобы остановить",

        # Normal download
        "downloading":          "{platform} · @{uploader} · {media_label}\nСкачиваю...",
        "download_failed":      "Ошибка скачивания: {error}",
        "sending_files":        "{prefix}тправляю {count} файлов...",
        "sending_one":          "{prefix}тправляю {platform} {media_label}...",
        "sent_short":           "Отправлено ({detail})",
        "uploading_telethon":   "📤 Загружаю {size_mb} МБ через пользовательский аккаунт…",
        "uploaded_telethon":    "✓ Загружено ({size_mb} МБ)",
        "send_failed":          "Ошибка отправки: {error}",

        # Delivery links
        "file_ready":           "📁 Файл готов · {size_mb:.0f} МБ",
        "tailnet_link":         "🔒 Tailnet (вы, в сети):\n{url}",
        "share_link":           "🌍 Общая ссылка (для всех, истекает через {hours}ч):\n{url}",
        "no_delivery":          "⚠ Способ доставки не настроен. Файл на /downloads/{rel}",

        # /watch /unwatch /watchlist
        "watch_usage":          "Использование: /watch <url>\nПример: /watch https://twitch.tv/some_streamer",
        "unwatch_usage":        "Использование: /unwatch <url>",
        "watch_already":        "Уже отслеживается: {url}",
        "watch_added":          "Теперь отслеживается: {url}",
        "watch_not_found":      "Нет в списке отслеживания: {url}",
        "watch_removed":        "Удалено: {url}",
        "watchlist_empty":      "Список отслеживания пуст.\nДобавить: /watch <url>",
        "watchlist_header":     "📺 Список отслеживания ({count})",

        # Stream monitor live notification
        "monitor_live_prompt":  "🔴 ЭФИР — {uploader}\n{title}\n\n{url}\n\nЗаписать этот стрим?",
        "btn_yes_record":       "✅ Да — записать",
        "btn_skip":             "❌ Пропустить",
        "btn_lang_en":          "English",
        "btn_lang_ru":          "Русский",
        "monitor_skipped":      "⏭ Пропущено",
        "monitor_starting":     "🎬 Запись начинается…",
        "monitor_record_starting": "🔴 Запись · @{uploader}\nЗапуск…",
        "monitor_recording_crashed": "⚠ Запись прервалась с ошибкой: {error}",
        "btn_snooze_1h":        "💤 1ч",
        "btn_snooze_8h":        "😴 8ч",
        "monitor_snoozed":      "💤 Тишина на {duration} (до {until})",
    },
}


_lang_cache: dict[int, str] = {}
_loaded_from_disk = False


def _load_from_disk_once() -> None:
    global _loaded_from_disk
    if _loaded_from_disk:
        return
    _loaded_from_disk = True
    if not LANG_FILE.exists():
        return
    try:
        with open(LANG_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            try:
                _lang_cache[int(k)] = str(v) if str(v) in SUPPORTED_LANGS else DEFAULT_LANG
            except (ValueError, TypeError):
                continue
    except Exception as e:
        logger.warning("i18n: failed to read %s: %s", LANG_FILE, e)


def get_lang(chat_id: int) -> str:
    _load_from_disk_once()
    return _lang_cache.get(int(chat_id), DEFAULT_LANG)


def set_lang(chat_id: int, lang: str) -> bool:
    """Returns True if the lang was set, False if not supported."""
    if lang not in SUPPORTED_LANGS:
        return False
    _load_from_disk_once()
    _lang_cache[int(chat_id)] = lang
    try:
        LANG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LANG_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in _lang_cache.items()}, f, indent=2, ensure_ascii=False)
        tmp.replace(LANG_FILE)
    except Exception as e:
        logger.error("i18n: failed to persist %s: %s", LANG_FILE, e)
    return True


def t(key: str, lang: str = DEFAULT_LANG, **kwargs) -> str:
    """Translate a key, falling back to English then to the key itself."""
    table = STRINGS.get(lang) or STRINGS[DEFAULT_LANG]
    template = table.get(key) or STRINGS[DEFAULT_LANG].get(key) or key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError) as e:
        logger.warning("i18n: format failed for key=%s lang=%s: %s", key, lang, e)
        return template


def format_duration(seconds: int | float) -> str:
    """Format a duration as H:MM:SS (or MM:SS for under an hour).

    Examples: 7 → '0:07', 332 → '5:32', 3932 → '1:05:32'.
    """
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"
