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

# NudeNet ke labels jinko hum "18+ content" maanenge.
# Zaroorat ke hisaab se yahan se labels add/remove kar sakte ho.
NSFW_LABELS = {
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
    "MALE_BREAST_EXPOSED",
}

# Kitna confident hona chahiye model ko (0.0 - 1.0). Zyada value = kam false positive.
CONFIDENCE_THRESHOLD = 0.55

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("yuta-bot")

detector = NudeDetector()  # pehli baar chalne par model download/load hota hai


def is_nsfw(image_bytes: bytes) -> bool:
    """Image bytes ko check karta hai, True return karta hai agar NSFW content mile."""
    tmp_path = "/tmp/yuta_scan_tmp.jpg"
    with open(tmp_path, "wb") as f:
        f.write(image_bytes)

    try:
        results = detector.detect(tmp_path)
    except Exception as e:
        logger.error(f"Detection error: {e}")
        return False

    for r in results:
        if r.get("class") in NSFW_LABELS and r.get("score", 0) >= CONFIDENCE_THRESHOLD:
            logger.info(f"NSFW match: {r.get('class')} score={r.get('score'):.2f}")
            return True
    return False


async def scan_and_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, is_sticker: bool = False):
    chat = update.effective_chat
    message = update.effective_message

    try:
        tg_file = await context.bot.get_file(file_id)
        file_bytes = await tg_file.download_as_bytearray()
    except Exception as e:
        logger.error(f"File download failed: {e}")
        return

    # Sticker agar .webp animated/video hai to skip (sirf static webp/png/jpg check karte hain)
    try:
        img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        jpeg_bytes = buf.getvalue()
    except Exception as e:
        logger.warning(f"Image convert skip (likely animated sticker): {e}")
        return

    if is_nsfw(jpeg_bytes):
        try:
            await message.delete()
            logger.info(f"Deleted NSFW {'sticker' if is_sticker else 'photo'} in chat {chat.id}")
            warn = await context.bot.send_message(
                chat_id=chat.id,
                text="🚫 18+ content detect hua, message delete kar diya gaya.",
            )
        except Exception as e:
            logger.error(f"Delete failed (check admin/delete permission): {e}")


async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sticker = update.effective_message.sticker
    if sticker is None:
        return
    # Animated/video stickers (.tgs/.webm) skip karte hain, static .webp hi check hota hai
    if sticker.is_animated or sticker.is_video:
        return
    await scan_and_delete(update, context, sticker.file_id, is_sticker=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = update.effective_message.photo
    if not photos:
        return
    largest = photos[-1]
    await scan_and_delete(update, context, largest.file_id, is_sticker=False)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Yuta bot active hai.\n"
        "Mujhe group me ADMIN banao aur 'Delete Messages' permission do.\n"
        "Main 24/7 sticker/photo scan karke 18+ content auto-delete karunga."
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
