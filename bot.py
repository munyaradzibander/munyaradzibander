"""Telegram mirror/leech bot with bounded, public-HTTP downloads."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
log = logging.getLogger("mirrorbot")

MAX_BYTES = int(os.getenv("MAX_FILE_BYTES", str(45 * 1024 * 1024)))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "900"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "").strip().rstrip("/")
ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if value.strip()
}
ALLOWED_CHAT_IDS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
    if value.strip()
}


class DownloadError(Exception):
    """An expected error while safely fetching a user-provided URL."""


class PublicOnlyResolver(AbstractResolver):
    """Resolve public destinations only, preventing private-network SSRF."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise OSError(f"Could not resolve host: {host}") from exc
        if not records:
            raise OSError(f"Could not resolve host: {host}")

        results = []
        for record_family, _type, proto, _canonname, sockaddr in records:
            address = ipaddress.ip_address(sockaddr[0])
            if not address.is_global:
                raise OSError("URL resolves to a non-public network address")
            results.append(
                {
                    "hostname": host,
                    "host": str(address),
                    "port": port,
                    "family": record_family,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return results

    async def close(self) -> None:
        return None


def _safe_filename(value: str) -> str:
    value = unquote(value).replace("\\", "/").split("/")[-1]
    value = re.sub(r"[\x00-\x1f\x7f]", "", value).strip(" .")
    value = re.sub(r"[^A-Za-z0-9._() -]", "_", value)
    return (value[:180] or "download.bin")


def _filename_from_response(response: aiohttp.ClientResponse, url: str) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^;\"]+)", disposition, re.I)
    chosen = (match.group(1) or match.group(2)) if match else ""
    if not chosen:
        chosen = Path(urlsplit(url).path).name
    return _safe_filename(chosen or "download.bin")


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise DownloadError("Only http:// and https:// URLs are supported.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise DownloadError("That URL is not valid. Credentials in URLs are not accepted.")
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not literal.is_global:
        raise DownloadError("Private or reserved network addresses are not allowed.")


async def _download(url: str, directory: Path) -> tuple[Path, str]:
    _validate_url(url)
    resolver = PublicOnlyResolver()
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT, connect=30, sock_read=60)
    path: Path | None = None
    try:
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, trust_env=False
        ) as session:
            # Redirects are intentionally rejected; re-checking redirect targets is
            # necessary to avoid turning an external URL into an SSRF primitive.
            async with session.get(url, allow_redirects=False) as response:
                if 300 <= response.status < 400:
                    raise DownloadError("This URL redirects. Please provide the final direct URL.")
                if response.status != 200:
                    raise DownloadError(f"The server returned HTTP {response.status}.")
                length = response.content_length
                if length is not None and length > MAX_BYTES:
                    raise DownloadError(f"File is larger than the configured {MAX_BYTES // (1024 * 1024)} MB limit.")
                filename = _filename_from_response(response, url)
                path = directory / filename
                downloaded = 0
                with path.open("wb") as output:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        downloaded += len(chunk)
                        if downloaded > MAX_BYTES:
                            raise DownloadError(f"File is larger than the configured {MAX_BYTES // (1024 * 1024)} MB limit.")
                        output.write(chunk)
        return path, filename
    except DownloadError:
        if path:
            path.unlink(missing_ok=True)
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        if path:
            path.unlink(missing_ok=True)
        raise DownloadError(f"Download failed: {exc}") from exc


def _authorized(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(
        user
        and chat
        and user.id in ALLOWED_USER_IDS
        and (not ALLOWED_CHAT_IDS or chat.id in ALLOWED_CHAT_IDS)
    )


async def _deny_or_none(update: Update) -> bool:
    if _authorized(update):
        return False
    if update.effective_message:
        await update.effective_message.reply_text(
            "Access denied. Ask the bot owner to add your numeric Telegram user ID."
        )
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_or_none(update):
        return
    await update.effective_message.reply_text(
        "I can /leech a direct HTTP(S) URL to Telegram, or /mirror a URL or a replied-to Telegram file to your configured rclone remote.\n"
        "Example: /leech https://example.org/archive.zip\n"
        "Example: /mirror https://example.org/archive.zip\n"
        "For a Telegram file, reply to it with /mirror."
    )


async def leech(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_or_none(update):
        return
    message = update.effective_message
    if not context.args:
        await message.reply_text("Usage: /leech https://public-host.example/file.zip")
        return
    status = await message.reply_text("Downloading safely…")
    try:
        with tempfile.TemporaryDirectory(prefix="tg-leech-") as temp_dir:
            path, filename = await _download(context.args[0], Path(temp_dir))
            await status.edit_text("Uploading to Telegram…")
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=path,
                filename=filename,
                read_timeout=180,
                write_timeout=180,
                connect_timeout=30,
            )
        await status.delete()
    except DownloadError as exc:
        await status.edit_text(str(exc))
    except Exception:
        log.exception("Leech operation failed")
        await status.edit_text("Upload failed. The file may exceed Telegram's current bot upload limit.")


def _telegram_media(message):
    if message.document:
        return message.document
    if message.video:
        return message.video
    if message.audio:
        return message.audio
    if message.animation:
        return message.animation
    if message.voice:
        return message.voice
    if message.video_note:
        return message.video_note
    if message.photo:
        return message.photo[-1]
    return None


async def _upload_to_remote(path: Path, filename: str) -> None:
    # The remote is admin-configured, never supplied by a Telegram user.
    destination = f"{RCLONE_REMOTE}/{filename}"
    process = await asyncio.create_subprocess_exec(
        "rclone", "copyto", str(path), destination,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1800)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("rclone upload timed out")
    if process.returncode != 0:
        detail = (stderr or stdout).decode(errors="replace")[-1000:]
        raise RuntimeError(detail or "rclone upload failed")


async def mirror(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_or_none(update):
        return
    message = update.effective_message
    if not RCLONE_REMOTE:
        await message.reply_text("Mirror storage is not configured. Set RCLONE_REMOTE on the server.")
        return

    status = await message.reply_text("Preparing mirror…")
    try:
        with tempfile.TemporaryDirectory(prefix="tg-mirror-") as temp_dir:
            directory = Path(temp_dir)
            if context.args:
                path, filename = await _download(context.args[0], directory)
            elif message.reply_to_message:
                media = _telegram_media(message.reply_to_message)
                if media is None:
                    raise DownloadError("Reply to a Telegram document, video, audio, animation, voice, video note, or photo.")
                if media.file_size is not None and media.file_size > MAX_BYTES:
                    raise DownloadError(f"File exceeds the configured {MAX_BYTES // (1024 * 1024)} MB limit.")
                tg_file = await context.bot.get_file(media.file_id)
                filename = _safe_filename(getattr(media, "file_name", None) or f"telegram-{media.file_unique_id}.bin")
                path = directory / filename
                await tg_file.download_to_drive(custom_path=path)
                if path.stat().st_size > MAX_BYTES:
                    raise DownloadError("Downloaded Telegram file exceeds the configured size limit.")
            else:
                raise DownloadError("Use /mirror URL, or reply to a Telegram media message with /mirror.")
            await status.edit_text("Uploading to cloud storage…")
            await _upload_to_remote(path, filename)
        await status.edit_text(f"Mirrored successfully: {filename}")
    except DownloadError as exc:
        await status.edit_text(str(exc))
    except Exception:
        log.exception("Mirror operation failed")
        await status.edit_text("Mirror failed. Check the bot server logs and rclone remote configuration.")


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([])


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN before starting the bot.")
    if not ALLOWED_USER_IDS:
        raise SystemExit("Set ALLOWED_USER_IDS to at least one numeric Telegram user ID.")
    if MAX_BYTES <= 0:
        raise SystemExit("MAX_FILE_BYTES must be positive.")
    if RCLONE_REMOTE.startswith("-"):
        raise SystemExit("RCLONE_REMOTE is invalid.")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("leech", leech))
    application.add_handler(CommandHandler("mirror", mirror))
    log.info("Starting bot polling")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
