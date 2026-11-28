"""
Yuta Bot - Telegram group NSFW content auto-moderator
--------------------------------------------------
Kaam: Group me bheje gaye stickers aur photos ko scan karta hai.
Agar 18+ / NSFW content milta hai, to bot usay turant delete kar deta hai.

Zaroorat:
- Bot ko group me ADMIN banao
- Bot ko "Delete Messages" permission do

Detection: NudeNet (local model, koi API call nahi, koi daily limit nahi)
"""

import logging
import os
import io
import subprocess
import tempfile
from datetime import datetime

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)
from PIL import Image
from nudenet import NudeDetector

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# HIGH SECURITY MODE: exposed + covered/suggestive dono categories flag hoti hain.
NSFW_LABELS = {
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "FEMALE_BREAST_COVERED",
    "BUTTOCKS_COVERED",
    "FEMALE_GENITALIA_COVERED",
    "ANUS_COVERED",
    "ARMPITS_EXPOSED",
    "BELLY_EXPOSED",
}

# High security: zyada sensitive (kam confidence par bhi flag karega).
CONFIDENCE_THRESHOLD = 0.35

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("yuta-bot")

detector = NudeDetector()  # pehli baar chalne par model download/load hota hai


def extract_first_frame(video_bytes: bytes) -> bytes | None:
    """Video sticker (.webm) ke bytes se pehla frame nikal kar JPEG bytes return karta hai.
    ffmpeg system me installed hona chahiye. Agar fail ho to None return karta hai."""
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as vid_f:
        vid_f.write(video_bytes)
        vid_path = vid_f.name
    out_path = vid_path + ".jpg"

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", vid_path, "-vframes", "1", out_path],
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0 or not os.path.exists(out_path):
            logger.error(f"ffmpeg frame extraction failed: {result.stderr.decode(errors='ignore')[-300:]}")
            return None
        with open(out_path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        logger.error("ffmpeg not installed on this host - video stickers can't be scanned without it.")
        return None
    except Exception as e:
        logger.error(f"Frame extraction error: {e}")
        return None
    finally:
        for p in (vid_path, out_path):
            if os.path.exists(p):
                os.remove(p)


def check_nsfw(image_bytes: bytes):
    """Image bytes ko check karta hai. NSFW milne par (label, confidence_score) return karta hai,
    nahi to None return karta hai."""
    tmp_path = "/tmp/yuta_scan_tmp.jpg"
    with open(tmp_path, "wb") as f:
        f.write(image_bytes)

    try:
        results = detector.detect(tmp_path)
    except Exception as e:
        logger.error(f"Detection error: {e}")
        return None

    logger.info(f"Raw detector results: {results}")

    best_match = None
    for r in results:
        if r.get("class") in NSFW_LABELS and r.get("score", 0) >= CONFIDENCE_THRESHOLD:
            if best_match is None or r.get("score", 0) > best_match[1]:
                best_match = (r.get("class"), r.get("score", 0))

    if best_match:
        logger.info(f"NSFW match: {best_match[0]} score={best_match[1]:.2f}")
    return best_match


async def scan_and_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, is_sticker: bool = False, is_video: bool = False):
    chat = update.effective_chat
    message = update.effective_message
    logger.info(f"Scanning {'video sticker' if is_video else ('sticker' if is_sticker else 'photo')} in chat {chat.id}")

    try:
        tg_file = await context.bot.get_file(file_id)
        file_bytes = await tg_file.download_as_bytearray()
    except Exception as e:
        logger.error(f"File download failed: {e}")
        return

    if is_video:
        # Video sticker (.webm) - pehle frame nikal kar check karte hain
        frame_bytes = extract_first_frame(bytes(file_bytes))
        if frame_bytes is None:
            logger.warning("Could not extract frame from video sticker - skipping scan.")
            return
        try:
            img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG")
            jpeg_bytes = buf.getvalue()
        except Exception as e:
            logger.error(f"Extracted frame could not be processed: {e}")
            return
    else:
        # Static sticker/photo (.webp/.png/.jpg)
        try:
            img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG")
            jpeg_bytes = buf.getvalue()
        except Exception as e:
            logger.warning(f"Image convert skip (likely animated .tgs sticker): {e}")
            return

    match = check_nsfw(jpeg_bytes)
    if match:
        label, score = match
        confidence_pct = round(score * 100, 1)

        sender = message.from_user
        sender_mention = sender.mention_html() if sender else "Unknown user"

        try:
            await message.delete()
            logger.info(f"Deleted NSFW {'sticker' if is_sticker else 'photo'} in chat {chat.id}, sent by {sender.id if sender else 'unknown'}")
        except Exception as e:
            logger.error(f"Delete failed (check admin/delete permission): {e}")
            return

        # Group ke admins fetch karke tag karte hain
        admin_mentions = []
        try:
            admins = await context.bot.get_chat_administrators(chat.id)
            for a in admins:
                if a.user.is_bot:
                    continue
                admin_mentions.append(a.user.mention_html())
        except Exception as e:
            logger.error(f"Could not fetch admins: {e}")

        admin_text = " ".join(admin_mentions) if admin_mentions else "Admins"

        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"🚫 <b>NSFW Media Removed</b>\n"
                    f"Confidence: {confidence_pct}%\n"
                    f"Sent by: {sender_mention}\n"
                    f"{admin_text}, please review this user."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Could not send delete notice: {e}")
    else:
        logger.info("No NSFW content found above threshold - message not deleted.")


async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sticker = update.effective_message.sticker
    if sticker is None:
        return
    logger.info(f"Sticker received. animated={sticker.is_animated} video={sticker.is_video}")
    # .tgs (Lottie animated) stickers abhi bhi skip hote hain - unse frame nikalna alag tareeka maangta hai
    if sticker.is_animated:
        logger.info("Skipping .tgs animated sticker (not supported yet).")
        return
    await scan_and_delete(update, context, sticker.file_id, is_sticker=True, is_video=sticker.is_video)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = update.effective_message.photo
    if not photos:
        return
    largest = photos[-1]
    await scan_and_delete(update, context, largest.file_id, is_sticker=False)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Yuta bot is active.\n"
        "Make me an ADMIN in the group and grant 'Delete Messages' permission.\n"
        "I will scan every sticker/photo 24/7 and auto-delete NSFW content (high security mode)."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"✅ Yuta running.\nTime: {datetime.now()}\nThreshold: {CONFIDENCE_THRESHOLD}"
    )


def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN environment variable set karo (BotFather se liya hua token).")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Yuta bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
