import asyncio
from datetime import datetime
import json
import logging
import os
import re
import sys
import time
import uuid
from urllib.parse import quote

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TPE2, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
import yt_dlp

# Enable ANSI escape sequences in Windows terminal (PowerShell / CMD)
if os.name == "nt":
    os.system("")

# =====================================================================
# 👇 1. YOUR BOT TOKEN:
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# 👇 2. YOUR NETLIFY MINI APP URL:
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://creative-biscotti-47c598.netlify.app")

# 👇 3. NUMBER OF SONGS PER PAGE IN CHAT SEARCH:
ITEMS_PER_PAGE = 5
MAX_SEARCH_RESULTS = 25

PORT = int(os.getenv("PORT", 8080))
# =====================================================================


# =====================================================================
# 🎨 TERMINAL UI & COLORFUL DEBUG LOGGER
# =====================================================================
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"


TAG_STYLES = {
    "SYSTEM": (Colors.CYAN, "⚙️"),
    "USER": (Colors.MAGENTA, "👤"),
    "WEBAPP": (Colors.MAGENTA, "📱"),
    "SEARCH": (Colors.BLUE, "🔎"),
    "PAGE": (Colors.YELLOW, "📄"),
    "DL": (Colors.CYAN, "⬇️"),
    "META": (Colors.GREEN, "🎨"),
    "SEND": (Colors.GREEN, "🚀"),
    "CLEAN": (Colors.GRAY, "🗑️"),
    "API": (Colors.BLUE, "🌐"),
    "WARN": (Colors.YELLOW, "⚠️"),
    "ERROR": (Colors.RED, "❌"),
}


def tlog(tag: str, message: str):
    """Prints a formatted, color-coded log line with timestamp and category tag."""
    now_str = datetime.now().strftime("%H:%M:%S")
    color, icon = TAG_STYLES.get(tag, (Colors.WHITE, "•"))
    padded_tag = f"[{tag}]".ljust(8)
    print(
        f"{Colors.GRAY}{now_str}{Colors.RESET} │ "
        f"{color}{Colors.BOLD}{padded_tag}{Colors.RESET} │ "
        f"{icon} {message}",
        flush=True,
    )


def format_user(user: types.User | None) -> str:
    """Formats user details for terminal output."""
    if not user:
        return "Unknown User"
    name = user.full_name or "NoName"
    uname = f"@{user.username}" if user.username else "no_username"
    return f"{Colors.BOLD}{name}{Colors.RESET} ({uname} | ID: {Colors.CYAN}{user.id}{Colors.RESET})"


def print_startup_banner(bot_info: types.User):
    """Renders an informative startup dashboard in the terminal."""
    abs_dl_dir = os.path.abspath(DOWNLOAD_DIR)
    line = "─" * 68
    print(f"\n{Colors.CYAN}╭{line}╮{Colors.RESET}")
    print(
        f"{Colors.CYAN}│{Colors.RESET}  🎵 {Colors.BOLD}{Colors.WHITE}TELEGRAM MUSIC BOT — MONITORING & DEBUG DASHBOARD{Colors.RESET}"
        + " " * 14
        + f"{Colors.CYAN}│{Colors.RESET}"
    )
    print(f"{Colors.CYAN}├{line}┤{Colors.RESET}")
    print(
        f"{Colors.CYAN}│{Colors.RESET}  🤖 {Colors.BOLD}Bot         :{Colors.RESET} {Colors.GREEN}{bot_info.full_name}{Colors.RESET} (@{bot_info.username}) [ID: {bot_info.id}]"
    )
    print(
        f"{Colors.CYAN}│{Colors.RESET}  🌐 {Colors.BOLD}API / Server:{Colors.RESET} {Colors.YELLOW}http://localhost:{PORT}{Colors.RESET}"
    )
    print(
        f"{Colors.CYAN}│{Colors.RESET}  📱 {Colors.BOLD}Mini App URL:{Colors.RESET} {Colors.MAGENTA}{WEBAPP_URL}{Colors.RESET}"
    )
    print(
        f"{Colors.CYAN}│{Colors.RESET}  📄 {Colors.BOLD}Pagination  :{Colors.RESET} {ITEMS_PER_PAGE} tracks per page (max {MAX_SEARCH_RESULTS} results)"
    )
    print(
        f"{Colors.CYAN}│{Colors.RESET}  📂 {Colors.BOLD}Downloads   :{Colors.RESET} {Colors.GRAY}{abs_dl_dir}{Colors.RESET}"
    )
    print(f"{Colors.CYAN}╰{line}╯{Colors.RESET}\n", flush=True)


# Suppress verbose third-party logs so only clean debug output is shown
logging.basicConfig(level=logging.WARNING, stream=sys.stdout)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

# =====================================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

SEARCH_CACHE = {}


def cleanup_cache():
    now = time.time()
    expired = [k for k, v in SEARCH_CACHE.items() if now - v["created"] > 3600]
    for k in expired:
        SEARCH_CACHE.pop(k, None)
    if expired:
        tlog("CLEAN", f"Cleared expired search sessions from cache: {len(expired)}")


def clean_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return cleaned if cleaned else "track"


def format_duration(seconds) -> str:
    if not seconds:
        return "--:--"
    seconds = int(seconds)
    mins, secs = divmod(seconds, 60)
    return f"{mins:02d}:{secs:02d}"


def extract_artist_and_title(info: dict, fallback_query: str = "") -> tuple[str, str]:
    """Separates artist and track title, stripping noise like (Official Audio)."""
    raw_title = (info.get("title") or fallback_query or "Unknown Track").strip()
    raw_artist = (
        info.get("artist")
        or info.get("creator")
        or info.get("uploader")
        or info.get("channel")
        or "Unknown Artist"
    ).strip()
    raw_track = (info.get("track") or "").strip()

    raw_artist = re.sub(r"\s*-\s*Topic$", "", raw_artist, flags=re.IGNORECASE).strip()

    clean_title = re.sub(
        r"\s*[\(\[](?:official\s*(?:music\s*)?(?:video|audio|lyric\s*video)|lyrics?|hd|hq|4k|prod\.?[^\)\]]*)[\)\]]",
        "",
        raw_title,
        flags=re.IGNORECASE,
    ).strip()

    if raw_track and info.get("artist"):
        return raw_artist, raw_track

    for sep in [" - ", " – ", " — ", " ~ "]:
        if sep in clean_title:
            parts = clean_title.split(sep, 1)
            artist_part, title_part = parts[0].strip(), parts[1].strip()
            if artist_part and title_part:
                return artist_part, title_part

    return raw_artist, clean_title


async def fetch_itunes_metadata(artist: str, title: str, fallback_query: str = "") -> dict:
    """Fetches official metadata (Artist, Title, Album, and Cover Art) from the iTunes API."""
    search_term = f"{artist} {title}".strip() if artist != "Unknown Artist" else (fallback_query or title)
    url = f"https://itunes.apple.com/search?term={quote(search_term)}&entity=song&limit=1"

    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    results = data.get("results") or []
                    if results:
                        item = results[0]
                        artwork = item.get("artworkUrl100", "")
                        cover_url = artwork.replace("100x100bb", "300x300bb") if artwork else ""
                        return {
                            "found": True,
                            "artist": item.get("artistName") or artist,
                            "title": item.get("trackName") or title,
                            "album": item.get("collectionName") or "",
                            "cover_url": cover_url,
                        }
    except Exception as e:
        tlog("WARN", f"iTunes API lookup failed: {e}")

    return {"found": False, "artist": artist, "title": title, "album": "", "cover_url": ""}


async def download_image_bytes(url: str) -> bytes | None:
    """Downloads cover image into memory as bytes."""
    if not url:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        tlog("WARN", f"Failed to download cover image ({url[:40]}...): {e}")
    return None


def write_audio_tags(filepath: str, artist: str, title: str, album: str = "", cover_bytes: bytes | None = None) -> bool:
    """Embeds metadata (Artist, Title, Album, and Cover Art) into .m4a or .mp3 files."""
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".m4a":
            audio = MP4(filepath)
            audio["\xa9nam"] = [title]
            audio["\xa9ART"] = [artist]
            audio["aART"] = [artist]
            if album:
                audio["\xa9alb"] = [album]
            if cover_bytes:
                audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            return True

        elif ext == ".mp3":
            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                tags = ID3()

            tags.delall("TIT2")
            tags.delall("TPE1")
            tags.delall("TPE2")
            tags.add(TIT2(encoding=3, text=title))
            tags.add(TPE1(encoding=3, text=artist))
            tags.add(TPE2(encoding=3, text=artist))

            if album:
                tags.delall("TALB")
                tags.add(TALB(encoding=3, text=album))

            if cover_bytes:
                tags.delall("APIC")
                tags.add(
                    APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,
                        desc="Cover",
                        data=cover_bytes,
                    )
                )
            tags.save(filepath, v2_version=3)
            return True
    except Exception as e:
        tlog("ERROR", f"Failed to write internal tags to {ext} file: {e}")
    return False


async def search_audius_fallback(query: str, limit: int = 3) -> list[dict]:
    """Backup source: Queries the public Audius API (no keys/cookies needed, direct MP3 stream)."""
    url = f"https://discoveryprovider.audius.co/v1/tracks/search?query={quote(query)}&app_name=tg_music_bot"
    results = []
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    for item in (data.get("data") or [])[:limit]:
                        track_id = item.get("id")
                        if not track_id:
                            continue
                        duration = item.get("duration") or 0
                        if duration and int(duration) <= 35:
                            continue
                        stream_url = f"https://discoveryprovider.audius.co/v1/tracks/{track_id}/stream?app_name=tg_music_bot"
                        artwork = (item.get("artwork") or {}).get("480x480") or ""
                        artist = (item.get("user") or {}).get("name") or "Unknown Artist"
                        title = item.get("title") or query
                        results.append({
                            "url": stream_url,
                            "artist": artist,
                            "title": title,
                            "duration": duration,
                            "duration_str": format_duration(duration),
                            "thumbnail": artwork,
                            "source": "Audius",
                        })
    except Exception as e:
        tlog("WARN", f"Audius backup lookup error: {e}")
    return results


def find_candidate_tracks_sync(query: str) -> list[dict]:
    """
    Collects a ranked list of candidate streams across SoundCloud, YouTube, and Bilibili
    for direct Mini App downloads, skipping 30-second previews.
    """
    start_t = time.perf_counter()
    search_sources = [
        ("SoundCloud", f"scsearch3:{query}"),
        ("YouTube", f"ytsearch3:{query}"),
        ("Bilibili", f"bilisearch2:{query}"),
    ]

    ydl_opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    candidates = []
    seen_urls = set()

    for source_name, source_query in search_sources:
        try:
            tlog("SEARCH", f"Checking {Colors.BOLD}{source_name}{Colors.RESET} ({source_query})...")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(source_query, download=False)
                entries = info.get("entries") or []

                for entry in entries:
                    if not entry:
                        continue
                    url = entry.get("url") or entry.get("webpage_url")
                    if not url or url in seen_urls:
                        continue

                    duration = entry.get("duration")
                    if duration and int(duration) <= 35:
                        tlog("WARN", f"Skipped short preview ({duration}s) on {source_name}")
                        continue

                    seen_urls.add(url)
                    artist, title = extract_artist_and_title(entry, query)
                    thumbnail = entry.get("thumbnail")
                    if not thumbnail and entry.get("thumbnails"):
                        thumbnail = entry["thumbnails"][-1].get("url")

                    candidates.append({
                        "url": url,
                        "artist": artist,
                        "title": title,
                        "duration": duration,
                        "duration_str": format_duration(duration),
                        "thumbnail": thumbnail or "",
                        "source": source_name,
                    })

            if len(candidates) >= 2 and source_name == "YouTube":
                break
        except Exception as e:
            tlog("WARN", f"Lookup error on {source_name}: {e}")
            continue

    elapsed = time.perf_counter() - start_t
    tlog(
        "SEARCH",
        f"Prepared {Colors.GREEN}{len(candidates)}{Colors.RESET} stream candidates in {Colors.YELLOW}{elapsed:.2f}s{Colors.RESET}",
    )
    return candidates


def search_tracks_sync(query: str) -> list[dict]:
    """Searches for multiple tracks across SoundCloud, YouTube, and Bilibili for chat pagination."""
    start_t = time.perf_counter()
    half_limit = max(10, MAX_SEARCH_RESULTS // 2)
    search_sources = [
        ("SoundCloud", f"scsearch{half_limit}:{query}"),
        ("YouTube", f"ytsearch{half_limit}:{query}"),
        ("Bilibili", f"bilisearch5:{query}"),
    ]

    ydl_opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    collected_results = []
    seen_urls = set()

    for source_name, source_query in search_sources:
        if source_name == "Bilibili" and len(collected_results) >= ITEMS_PER_PAGE:
            break

        try:
            tlog("SEARCH", f"Querying {Colors.BOLD}{source_name}{Colors.RESET} ({source_query})...")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(source_query, download=False)
                entries = info.get("entries") or []
                added_from_source = 0

                for entry in entries:
                    if not entry:
                        continue
                    url = entry.get("url") or entry.get("webpage_url")
                    if not url or url in seen_urls:
                        continue

                    duration = entry.get("duration")
                    if duration and int(duration) <= 35:
                        continue

                    seen_urls.add(url)
                    artist, title = extract_artist_and_title(entry, query)

                    thumbnail = entry.get("thumbnail")
                    if not thumbnail and entry.get("thumbnails"):
                        thumbnail = entry["thumbnails"][-1].get("url")

                    collected_results.append({
                        "url": url,
                        "artist": artist,
                        "title": title,
                        "duration": duration,
                        "duration_str": format_duration(duration),
                        "thumbnail": thumbnail or "",
                        "source": source_name,
                    })
                    added_from_source += 1

                tlog(
                    "SEARCH",
                    f"Source {Colors.BOLD}{source_name}{Colors.RESET} returned {Colors.GREEN}{added_from_source}{Colors.RESET} tracks",
                )
        except Exception as e:
            tlog("WARN", f"Search error on {source_name}: {e}")
            continue

    elapsed = time.perf_counter() - start_t
    tlog(
        "SEARCH",
        f"Search summary: {Colors.BOLD}{len(collected_results[:MAX_SEARCH_RESULTS])}{Colors.RESET} tracks found in {Colors.YELLOW}{elapsed:.2f}s{Colors.RESET}",
    )
    return collected_results[:MAX_SEARCH_RESULTS]


def download_raw_audio_sync(url: str) -> dict:
    """Downloads the raw audio stream (.m4a or .mp3) and returns technical metadata."""
    unique_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s")

    ydl_opts = {
        "format": (
            "bestaudio[ext=m4a][protocol^=http]/"
            "bestaudio[ext=mp3][protocol^=http]/"
            "bestaudio[ext=m4a]/"
            "bestaudio[ext=mp3]/"
            "bestaudio"
        ),
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {"player_client": ["ios", "android", "tv", "web"]}
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = ydl.prepare_filename(info)

        if not os.path.exists(filepath):
            raise FileNotFoundError("File not found on disk after yt-dlp finished.")

        artist, title = extract_artist_and_title(info)
        thumbnail = info.get("thumbnail") or ""
        if not thumbnail and info.get("thumbnails"):
            thumbnail = info["thumbnails"][-1].get("url") or ""

        return {
            "filepath": filepath,
            "artist": artist,
            "title": title,
            "duration": info.get("duration"),
            "ext": info.get("ext") or "m4a",
            "abr": info.get("abr"),
            "thumbnail": thumbnail,
        }


async def download_and_send_to_user(chat_id: int, track_data: dict, user_label: str = ""):
    """
    Downloads track with automatic multi-source failover,
    embeds cover art & metadata, sends to Telegram, and deletes the temporary file.
    """
    init_artist = track_data.get("artist") or "Unknown Artist"
    init_title = track_data.get("title") or "Track"
    sep = " - "
    query_str = track_data.get("query") or f"{init_artist}{sep}{init_title}"

    display_name = (
        f"{init_artist} — {init_title}"
        if init_artist != "Unknown Artist"
        else query_str
    )

    progress_msg = await bot.send_message(
        chat_id=chat_id,
        text=f"⬇️ Downloading: {display_name}...",
    )
    filepath = None

    try:
        candidates = []
        if track_data.get("url"):
            candidates.append(track_data)

        extra_candidates = await asyncio.to_thread(find_candidate_tracks_sync, query_str)
        for c in extra_candidates:
            if not any(existing.get("url") == c["url"] for existing in candidates):
                candidates.append(c)

        if len(candidates) < 2:
            audius_candidates = await search_audius_fallback(query_str, limit=2)
            candidates.extend(audius_candidates)

        if not candidates:
            tlog("WARN", f"No audio streams found across any source for: \"{query_str}\"")
            await progress_msg.edit_text("❌ Could not find an audio stream for this song.")
            return

        raw_result = None
        used_candidate = None
        dl_time = 0.0

        for attempt_idx, cand in enumerate(candidates[:4], start=1):
            cand_url = cand["url"]
            cand_source = cand.get("source") or "Direct"
            tlog(
                "DL",
                f"Attempt #{attempt_idx} [{Colors.BOLD}{cand_source}{Colors.RESET}] for [Chat: {chat_id}] {user_label} ➔ "
                f"{Colors.BOLD}{init_artist} — {init_title}{Colors.RESET} │ URL: {Colors.GRAY}{cand_url}{Colors.RESET}",
            )
            try:
                dl_start = time.perf_counter()
                raw_result = await asyncio.to_thread(download_raw_audio_sync, cand_url)
                dl_time = time.perf_counter() - dl_start
                used_candidate = cand
                break
            except Exception as err:
                tlog("WARN", f"Source [{cand_source}] failed ({err}). Switching to next backup source...")
                continue

        if not raw_result or not used_candidate:
            raise RuntimeError("All primary and backup sources failed to download the stream.")

        filepath = raw_result["filepath"]
        file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
        ext = raw_result["ext"]
        abr = f"~{int(raw_result['abr'])} kbps" if raw_result.get("abr") else "original"

        tlog(
            "DL",
            f"Stream downloaded from [{Colors.BOLD}{used_candidate.get('source', 'Source')}{Colors.RESET}] "
            f"in {Colors.YELLOW}{dl_time:.2f}s{Colors.RESET} │ "
            f"Format: {Colors.CYAN}.{ext}{Colors.RESET} │ Bitrate: {abr} │ "
            f"Size: {Colors.GREEN}{file_size_mb:.2f} MB{Colors.RESET}",
        )

        if file_size_mb > 49.5:
            tlog("WARN", f"File exceeds Telegram limit ({file_size_mb:.2f} MB). Upload aborted.")
            await progress_msg.edit_text("⚠️ File exceeds Telegram's 50 MB limit. Please choose another song.")
            return

        artist = init_artist if init_artist != "Unknown Artist" else (used_candidate.get("artist") or raw_result["artist"])
        title = init_title if init_title != "Unknown Track" else (used_candidate.get("title") or raw_result["title"])

        # Fetch official metadata and cover art from iTunes
        itunes_meta = await fetch_itunes_metadata(artist, title, query_str)
        if itunes_meta.get("artist") and itunes_meta["artist"] != "Unknown Artist":
            artist = itunes_meta["artist"]
        if itunes_meta.get("title") and itunes_meta["title"] != "Unknown Track":
            title = itunes_meta["title"]
        album = itunes_meta.get("album") or ""

        cover_url = (
            track_data.get("thumbnail")
            or itunes_meta.get("cover_url")
            or used_candidate.get("thumbnail")
            or raw_result.get("thumbnail")
        )
        cover_bytes = await download_image_bytes(cover_url)
        cover_kb = f"{len(cover_bytes) / 1024:.1f} KB" if cover_bytes else "none"

        tlog(
            "META",
            f"Metadata: Artist=\"{Colors.BOLD}{artist}{Colors.RESET}\", "
            f"Title=\"{Colors.BOLD}{title}{Colors.RESET}\", "
            f"Album=\"{album or '—'}\" │ Cover: {Colors.CYAN}{cover_kb}{Colors.RESET}",
        )

        tags_ok = await asyncio.to_thread(write_audio_tags, filepath, artist, title, album, cover_bytes)
        if tags_ok:
            tlog("META", f"Internal tags and cover art embedded into {Colors.CYAN}.{ext}{Colors.RESET} file")

        safe_filename = f"{clean_filename(title)}.{ext}"
        audio_file = FSInputFile(filepath, filename=safe_filename)
        thumb_file = BufferedInputFile(cover_bytes, filename="cover.jpg") if cover_bytes else None
        duration_val = raw_result.get("duration") or used_candidate.get("duration") or track_data.get("duration")

        send_start = time.perf_counter()
        await bot.send_audio(
            chat_id=chat_id,
            audio=audio_file,
            performer=artist,
            title=title,
            thumbnail=thumb_file,
            duration=int(duration_val) if duration_val else None,
        )
        send_time = time.perf_counter() - send_start

        tlog(
            "SEND",
            f"File \"{Colors.BOLD}{safe_filename}{Colors.RESET}\" delivered to chat {chat_id} in {Colors.YELLOW}{send_time:.2f}s{Colors.RESET}",
        )
        await progress_msg.delete()

    except Exception as e:
        tlog("ERROR", f"Failed to download/send track ({query_str}): {e}")
        await progress_msg.edit_text("❌ Failed to download this track. Please try another song.")
    finally:
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
                tlog("CLEAN", f"Temporary file deleted from disk: {os.path.basename(filepath)}")
            except OSError as e:
                tlog("WARN", f"Failed to delete temporary file: {e}")


# =====================================================================
# 🌐 WEB SERVER (SERVES YOUR INDEX.HTML & API ENDPOINTS)
# =====================================================================
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


async def serve_webapp_html(request: web.Request):
    tlog("API", f"GET / (serving index.html to IP: {request.remote})")
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    if os.path.exists(html_path):
        return web.FileResponse(html_path)
    tlog("WARN", "Requested index.html, but the file was not found in the bot directory!")
    return web.Response(text="index.html not found in the bot directory.", status=404)


async def handle_options(request: web.Request):
    return web.Response(status=204, headers=CORS_HEADERS)


async def api_search(request: web.Request):
    query = request.query.get("q", "").strip()
    tlog("API", f"GET /api/search?q={query} (IP: {request.remote})")
    if not query:
        return web.json_response({"ok": False, "error": "Empty query"}, status=400, headers=CORS_HEADERS)

    tracks = await asyncio.to_thread(search_tracks_sync, query)
    return web.json_response({"ok": True, "results": tracks}, headers=CORS_HEADERS)


async def api_download(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400, headers=CORS_HEADERS)

    user_id = data.get("user_id")
    track = data.get("track")
    tlog("API", f"POST /api/download for user_id={user_id}")

    if not user_id or not track:
        return web.json_response(
            {"ok": False, "error": "user_id and track object are required"},
            status=400,
            headers=CORS_HEADERS,
        )

    asyncio.create_task(download_and_send_to_user(int(user_id), track, user_label="[via Mini App API]"))
    return web.json_response({"ok": True, "message": "Download started!"}, headers=CORS_HEADERS)


# =====================================================================
# 🤖 TELEGRAM BOT & PAGINATION LOGIC
# =====================================================================
def build_pagination_keyboard(session_id: str, page: int) -> InlineKeyboardMarkup:
    session = SEARCH_CACHE.get(session_id)
    if not session:
        return InlineKeyboardMarkup(inline_keyboard=[])

    tracks = session["tracks"]
    total_pages = max(1, (len(tracks) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_tracks = tracks[start_idx:end_idx]

    builder = InlineKeyboardBuilder()

    for idx, track in enumerate(page_tracks, start=start_idx):
        dur_str = format_duration(track.get("duration"))
        btn_text = f"🎵 {track['artist']} — {track['title']} ({dur_str})"
        if len(btn_text) > 55:
            btn_text = btn_text[:52] + "..."

        builder.row(
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"dl:{session_id}:{idx}",
            )
        )

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"page:{session_id}:{page - 1}"))
    else:
        nav_buttons.append(InlineKeyboardButton(text="⏹", callback_data="noop"))

    nav_buttons.append(InlineKeyboardButton(text=f"📄 {page + 1}/{total_pages}", callback_data="noop"))

    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"page:{session_id}:{page + 1}"))
    else:
        nav_buttons.append(InlineKeyboardButton(text="⏹", callback_data="noop"))

    builder.row(*nav_buttons)
    return builder.as_markup()


async def run_chat_search(message: types.Message, query: str, source_tag: str = "USER"):
    tlog(source_tag, f"{format_user(message.from_user)} ➔ Search: \"{Colors.YELLOW}{query}{Colors.RESET}\"")
    cleanup_cache()
    status_msg = await message.answer(f"🔎 Searching: {query}...")

    tracks = await asyncio.to_thread(search_tracks_sync, query)
    if not tracks:
        tlog("WARN", f"No results found for query \"{query}\".")
        await status_msg.edit_text("❌ Nothing found. Try a different query.")
        return

    session_id = uuid.uuid4().hex[:8]
    SEARCH_CACHE[session_id] = {
        "created": time.time(),
        "query": query,
        "tracks": tracks,
    }

    total_pages = max(1, (len(tracks) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    tlog(
        "SEARCH",
        f"Created session [{Colors.CYAN}{session_id}{Colors.RESET}] │ "
        f"Pages: {total_pages} ({ITEMS_PER_PAGE} tracks per page)",
    )

    keyboard = build_pagination_keyboard(session_id, page=0)
    await status_msg.edit_text(
        f"🎧 Results for \"{query}\".\nSelect a song from the list:",
        reply_markup=keyboard,
    )


@dp.message(CommandStart())
async def start_handler(message: types.Message):
    tlog("USER", f"{format_user(message.from_user)} triggered /start")
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎧 Open Visual Search", web_app=WebAppInfo(url=WEBAPP_URL))]
        ],
        resize_keyboard=True,
    )
    await message.answer(
        "Hi! Send me a song or artist name in the chat, or open the visual search using the button below.",
        reply_markup=kb,
    )


@dp.message(F.web_app_data)
async def web_app_data_handler(message: types.Message):
    """Immediately deletes the service message and downloads the song selected in the Mini App."""
    try:
        await message.delete()
    except Exception as e:
        tlog("WARN", f"Could not delete service message: {e}")

    raw_data = message.web_app_data.data.strip()
    user_label = format_user(message.from_user)
    tlog("WEBAPP", f"Direct download requested from Mini App by {user_label}: \"{Colors.YELLOW}{raw_data}{Colors.RESET}\"")

    try:
        payload = json.loads(raw_data)
        if isinstance(payload, dict):
            await download_and_send_to_user(message.chat.id, payload, user_label=user_label)
            return
    except json.JSONDecodeError:
        pass

    if raw_data:
        artist, title = extract_artist_and_title({"title": raw_data}, raw_data)
        track_info = {
            "query": raw_data,
            "artist": artist,
            "title": title,
        }
        await download_and_send_to_user(message.chat.id, track_info, user_label=user_label)


@dp.message(F.text)
async def search_handler(message: types.Message):
    query = message.text.strip()
    if query.startswith("/"):
        return
    await run_chat_search(message, query, source_tag="USER")


@dp.callback_query(F.data == "noop")
async def noop_callback(callback: types.CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("page:"))
async def page_callback(callback: types.CallbackQuery):
    _, session_id, page_str = callback.data.split(":")
    session = SEARCH_CACHE.get(session_id)

    if not session:
        tlog("WARN", f"{format_user(callback.from_user)} tried to flip page on expired session [{session_id}]")
        await callback.answer("⚠️ This search has expired. Please enter the song name again.", show_alert=True)
        return

    page = int(page_str)
    total_pages = max(1, (len(session["tracks"]) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    tlog(
        "PAGE",
        f"{format_user(callback.from_user)} browsing session [{Colors.CYAN}{session_id}{Colors.RESET}] "
        f"➔ Page {Colors.BOLD}{page + 1}/{total_pages}{Colors.RESET}",
    )

    keyboard = build_pagination_keyboard(session_id, page=page)
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("dl:"))
async def download_callback(callback: types.CallbackQuery):
    _, session_id, idx_str = callback.data.split(":")
    session = SEARCH_CACHE.get(session_id)

    if not session:
        tlog("WARN", f"{format_user(callback.from_user)} clicked a track in expired session [{session_id}]")
        await callback.answer("⚠️ This search has expired. Please enter the song name again.", show_alert=True)
        return

    idx = int(idx_str)
    tracks = session["tracks"]
    if idx < 0 or idx >= len(tracks):
        await callback.answer("❌ Track not found.", show_alert=True)
        return

    selected_track = tracks[idx]
    tlog(
        "USER",
        f"{format_user(callback.from_user)} selected track #{idx + 1} in session [{Colors.CYAN}{session_id}{Colors.RESET}]",
    )
    await callback.answer("⏳ Starting download...")
    await download_and_send_to_user(
        callback.message.chat.id,
        selected_track,
        user_label=format_user(callback.from_user),
    )


async def main():
    # Clean up any leftover temporary files on startup
    for f in os.listdir(DOWNLOAD_DIR):
        try:
            os.remove(os.path.join(DOWNLOAD_DIR, f))
        except OSError:
            pass

    app = web.Application()
    app.router.add_get("/", serve_webapp_html)
    app.router.add_route("OPTIONS", "/api/search", handle_options)
    app.router.add_route("OPTIONS", "/api/download", handle_options)
    app.router.add_get("/api/search", api_search)
    app.router.add_post("/api/download", api_download)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    bot_info = await bot.get_me()
    print_startup_banner(bot_info)
    tlog("SYSTEM", "Web server and Telegram Long-Polling started. Listening for events...")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        tlog("SYSTEM", "Bot stopped by user (Ctrl+C).")
