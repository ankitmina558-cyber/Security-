"""
Yuta Bot - Telegram group NSFW content auto-moderator (HIGH SECURITY v2)
-----------------------------------------------------------------------
Kaam: Group me bheje gaye stickers / photos / GIF / videos scan karta hai.
Agar 18+ / NSFW content milta hai, to bot usay turant delete kar deta hai,
admins ko tag karta hai, aur baar-baar karne walon ko mute kar deta hai.

Zaroorat:
- Bot ko group me ADMIN banao
- "Delete Messages" permission do (mute ke liye "Restrict Members" bhi)

Detection: NudeNet (local model, koi API call nahi, koi daily limit nahi, 24/7 free)
Env: BOT_TOKEN
"""

import os

# RAM bachane ke liye: heavy libraries ko 1 thread par chalao (imports se pehle set hona zaroori)
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import asyncio
import io
import logging
import shutil
import subprocess
import tempfile
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone

from PIL import Image
from nudenet import NudeDetector
from telegram import ChatPermissions, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# HIGH SECURITY: har label ka apna threshold (chhota number = zyada sensitive)
NSFW_THRESHOLDS = {
    # Exposed (sabse serious) - bahut kam confidence par bhi flag
    "FEMALE_GENITALIA_EXPOSED": 0.25,
    "MALE_GENITALIA_EXPOSED": 0.25,
    "FEMALE_BREAST_EXPOSED": 0.25,
    "BUTTOCKS_EXPOSED": 0.25,
    "ANUS_EXPOSED": 0.25,
    # Covered / suggestive
    "FEMALE_BREAST_COVERED": 0.35,
    "BUTTOCKS_COVERED": 0.35,
    "FEMALE_GENITALIA_COVERED": 0.35,
    "ANUS_COVERED": 0.35,
    # Mild (normal photos me false positive ho sakte hain, isliye thoda upar)
    "MALE_BREAST_EXPOSED": 0.40,
    "ARMPITS_EXPOSED": 0.40,
    "BELLY_EXPOSED": 0.40,
}

COMBO_THRESHOLD = 0.25   # 2+ alag labels is score par mile to bhi flag (combo rule)
TILE_SCAN = True         # image ke 4 hisse (zoom) bhi scan karo - chhoti cheezein pakadne ke liye
MAX_SIDE = 640           # scan se pehle image ko is size tak chhota karo (RAM bachat)
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10MB se badi file skip
VIDEO_FRAMES = 6         # video/GIF/sticker se itne frames scan honge (0.5 sec ke gap par)
MAX_STRIKES = 3          # itni violations par user mute
MUTE_MINUTES = 60        # mute ka time
CACHE_SIZE = 500         # same media dobara scan na ho

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("yuta-bot")

detector = NudeDetector()  # pehli baar chalne par model download/load hota hai

SCAN_SEMAPHORE: asyncio.Semaphore | None = None
STRIKES: dict = defaultdict(int)
SCAN_CACHE: "OrderedDict[str, tuple | None]" = OrderedDict()


# ---------------- IMAGE HELPERS ----------------
def prepare_jpeg(raw: bytes) -> bytes | None:
    """Kisi bhi image bytes ko chhota JPEG bana kar return karta hai. Fail hone par None."""
    try:
        img = Image.open(io.BytesIO(raw))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"Image prepare failed: {e}")
        return None


def extract_frames(video_bytes: bytes) -> list:
    """Video (.webm/.mp4) se kai frames nikal kar JPEG bytes ki list return karta hai.
    ffmpeg installed hona chahiye. Fail ho to khaali list."""
    work_dir = tempfile.mkdtemp(prefix="yuta_")
    vid_path = os.path.join(work_dir, "input.bin")
    frames = []
    try:
        with open(vid_path, "wb") as f:
            f.write(video_bytes)

        cmd = [
            "ffmpeg", "-y", "-threads", "1", "-i", vid_path,
            "-vf", "fps=2,scale=480:-2",
            "-frames:v", str(VIDEO_FRAMES),
            os.path.join(work_dir, "f_%02d.jpg"),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=25)

        names = sorted(n for n in os.listdir(work_dir) if n.startswith("f_") and n.endswith(".jpg"))
        if not names:
            # fallback: sirf pehla frame
            cmd2 = [
                "ffmpeg", "-y", "-threads", "1", "-i", vid_path,
                "-vframes", "1", "-vf", "scale=480:-2",
                os.path.join(work_dir, "f_00.jpg"),
            ]
            result = subprocess.run(cmd2, capture_output=True, timeout=25)
            names = sorted(n for n in os.listdir(work_dir) if n.startswith("f_") and n.endswith(".jpg"))

        if not names:
            logger.error(f"ffmpeg frame extraction failed: {result.stderr.decode(errors='ignore')[-300:]}")
            return []

        for n in names:
            with open(os.path.join(work_dir, n), "rb") as f:
                frames.append(f.read())
        return frames
    except FileNotFoundError:
        logger.error("ffmpeg not installed on this host - video frames can't be extracted.")
        return []
    except Exception as e:
        logger.error(f"Frame extraction error: {e}")
        return []
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def get_jpegs(context: ContextTypes.DEFAULT_TYPE, file_id: str, is_video: bool) -> list:
    """Telegram se file download karke JPEG bytes ki list deta hai (image = 1, video = kai frames)."""
    try:
        tg_file = await context.bot.get_file(file_id)
        if tg_file.file_size and tg_file.file_size > MAX_FILE_BYTES:
            logger.warning(f"File too large ({tg_file.file_size} bytes) - skipping this file.")
            return []
        file_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        logger.error(f"File download failed: {e}")
        return []

    if is_video:
        raw_frames = await asyncio.to_thread(extract_frames, file_bytes)
        del file_bytes
        out = []
        for fr in raw_frames:
            j = await asyncio.to_thread(prepare_jpeg, fr)
            if j:
                out.append(j)
        return out

    j = await asyncio.to_thread(prepare_jpeg, file_bytes)
    return [j] if j else []


# ---------------- DETECTION ----------------
def detect_image(img: Image.Image) -> list:
    """Ek PIL image par NudeNet chalata hai. Unique temp file use hoti hai aur turant delete."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp_path = f.name
        img.save(tmp_path, format="JPEG", quality=90)
        return detector.detect(tmp_path) or []
    except Exception as e:
        logger.error(f"Detection error: {e}")
        return []
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def evaluate(detections: list):
    """Detections ko rules se check karta hai. NSFW par (label, score) return, nahi to None."""
    hits = {}
    weak = {}
    for r in detections:
        label = r.get("class")
        if label not in NSFW_THRESHOLDS:
            continue
        score = float(r.get("score", 0) or 0)
        if score >= NSFW_THRESHOLDS[label]:
            hits[label] = max(hits.get(label, 0), score)
        if score >= COMBO_THRESHOLD:
            weak[label] = max(weak.get(label, 0), score)

    if hits:
        return max(hits.items(), key=lambda kv: kv[1])
    # Combo rule: 2+ alag suggestive labels ek hi image me ho
    if len(weak) >= 2:
        return max(weak.items(), key=lambda kv: kv[1])
    return None


def make_views(jpeg_bytes: bytes) -> list:
    """Image ka full view + (agar TILE_SCAN on) 4 overlapping zoom tiles."""
    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    views = [img]
    w, h = img.size
    if TILE_SCAN and min(w, h) >= 200:
        tw, th = int(w * 0.6), int(h * 0.6)
        for x in (0, w - tw):
            for y in (0, h - th):
                views.append(img.crop((x, y, x + tw, y + th)))
    return views


def check_nsfw_sync(jpegs: list):
    """JPEG list ko check karta hai. NSFW milne par (label, score) return karta hai, nahi to None."""
    for idx, jpeg_bytes in enumerate(jpegs):
        try:
            views = make_views(jpeg_bytes)
        except Exception as e:
            logger.warning(f"Could not open frame {idx}: {e}")
            continue

        all_detections = []
        for v_idx, view in enumerate(views):
            all_detections.extend(detect_image(view))
            match = evaluate(all_detections)
            if match:
                logger.info(f"NSFW match: {match[0]} score={match[1]:.2f} (frame {idx}, view {v_idx})")
                return match

        logger.info(f"Frame {idx} clean. Detections: {[(d.get('class'), round(d.get('score', 0), 2)) for d in all_detections]}")
    return None


# ---------------- CORE ----------------
def cache_get(key: str):
    if key in SCAN_CACHE:
        SCAN_CACHE.move_to_end(key)
        return True, SCAN_CACHE[key]
    return False, None


def cache_put(key: str, value):
    SCAN_CACHE[key] = value
    SCAN_CACHE.move_to_end(key)
    while len(SCAN_CACHE) > CACHE_SIZE:
        SCAN_CACHE.popitem(last=False)


async def analyze_media(context, unique_id: str, attempts: list):
    """attempts: [(file_id, is_video), ...] - pehla jo kaam kare use scan karta hai.
    Return: None (safe/skip) ya (label, score)."""
    found, cached = cache_get(unique_id)
    if found:
        logger.info("Cache hit - skipping re-scan.")
        return cached

    sem = SCAN_SEMAPHORE or asyncio.Semaphore(1)
    async with sem:
        jpegs = []
        for file_id, is_video in attempts:
            jpegs = await get_jpegs(context, file_id, is_video)
            if jpegs:
                break
        if not jpegs:
            logger.warning("Could not get any image to scan - skipping.")
            return None

        result = await asyncio.to_thread(check_nsfw_sync, jpegs)
        del jpegs

    cache_put(unique_id, result)
    return result


async def punish(update: Update, context: ContextTypes.DEFAULT_TYPE, score: float):
    chat = update.effective_chat
    message = update.effective_message
    sender = message.from_user
    sender_mention = sender.mention_html() if sender else "Unknown user"
    confidence_pct = round(score * 100, 1)

    try:
        await message.delete()
        logger.info(f"Deleted NSFW media in chat {chat.id}, sent by {sender.id if sender else 'unknown'}")
    except Exception as e:
        logger.error(f"Delete failed (check admin/delete permission): {e}")
        return

    strike_text = ""
    if sender:
        key = (chat.id, sender.id)
        STRIKES[key] += 1
        count = STRIKES[key]
        if count >= MAX_STRIKES:
            try:
                until = datetime.now(timezone.utc) + timedelta(minutes=MUTE_MINUTES)
                await context.bot.restrict_chat_member(
                    chat_id=chat.id,
                    user_id=sender.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until,
                )
                STRIKES[key] = 0
                strike_text = f"\n🔇 User ko {MUTE_MINUTES} minute ke liye mute kar diya gaya."
            except Exception as e:
                logger.error(f"Mute failed (Restrict Members permission / user is admin?): {e}")
                strike_text = f"\n⚠️ Warning {count}/{MAX_STRIKES}"
        else:
            strike_text = f"\n⚠️ Warning {count}/{MAX_STRIKES}"

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
                f"Sent by: {sender_mention}"
                f"{strike_text}\n"
                f"{admin_text}, please review this user."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Could not send delete notice: {e}")


async def scan_and_act(update: Update, context: ContextTypes.DEFAULT_TYPE, unique_id: str, attempts: list):
    logger.info(f"Scanning media in chat {update.effective_chat.id}")
    try:
        result = await analyze_media(context, unique_id, attempts)
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        return

    if result:
        await punish(update, context, result[1])
    else:
        logger.info("No NSFW content found - message not deleted.")


def get_thumb(obj):
    return getattr(obj, "thumbnail", None) or getattr(obj, "thumb", None)


# ---------------- HANDLERS ----------------
async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sticker = update.effective_message.sticker
    if sticker is None:
        return
    logger.info(f"Sticker received. animated={sticker.is_animated} video={sticker.is_video}")

    attempts = []
    if not sticker.is_animated:
        attempts.append((sticker.file_id, bool(sticker.is_video)))
    # .tgs (Lottie) se frame nahi nikal sakte, uska thumbnail scan hota hai
    thumb = get_thumb(sticker)
    if thumb is not None:
        attempts.append((thumb.file_id, False))

    if not attempts:
        logger.info("Sticker has nothing scannable - skipping.")
        return
    await scan_and_act(update, context, sticker.file_unique_id, attempts)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = update.effective_message.photo
    if not photos:
        return
    largest = photos[-1]
    await scan_and_act(update, context, largest.file_unique_id, [(largest.file_id, False)])


async def handle_animation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    anim = update.effective_message.animation
    if anim is None:
        return
    attempts = [(anim.file_id, True)]
    thumb = get_thumb(anim)
    if thumb is not None:
        attempts.append((thumb.file_id, False))
    await scan_and_act(update, context, anim.file_unique_id, attempts)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    media = msg.video or msg.video_note
    if media is None:
        return
    attempts = [(media.file_id, True)]
    thumb = get_thumb(media)
    if thumb is not None:
        attempts.append((thumb.file_id, False))
    await scan_and_act(update, context, media.file_unique_id, attempts)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.effective_message.document
    if doc is None:
        return
    mime = (doc.mime_type or "").lower()
    attempts = []
    if mime.startswith("image/"):
        attempts.append((doc.file_id, False))
    elif mime.startswith("video/"):
        attempts.append((doc.file_id, True))
    thumb = get_thumb(doc)
    if thumb is not None:
        attempts.append((thumb.file_id, False))
    if not attempts:
        return
    await scan_and_act(update, context, doc.file_unique_id, attempts)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Yuta bot is active.\n"
        "Make me an ADMIN in the group and grant 'Delete Messages' (and 'Restrict Members') permission.\n"
        "I scan every sticker/photo/GIF/video 24/7 and auto-delete NSFW content (high security mode)."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"✅ Yuta running.\n"
        f"Time: {datetime.now()}\n"
        f"Exposed threshold: 0.25 | Covered: 0.35 | Mild: 0.40\n"
        f"Zoom tile scan: {'ON' if TILE_SCAN else 'OFF'}\n"
        f"Video frames: {VIDEO_FRAMES}\n"
        f"Mute after {MAX_STRIKES} strikes ({MUTE_MINUTES} min)"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error:", exc_info=context.error)


async def post_init(app: Application):
    global SCAN_SEMAPHORE
    SCAN_SEMAPHORE = asyncio.Semaphore(1)  # ek samay me sirf 1 scan (RAM bachat)


def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN environment variable set karo (BotFather se liya hua token).")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    groups = filters.ChatType.GROUPS
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(groups & filters.Sticker.ALL, handle_sticker))
    app.add_handler(MessageHandler(groups & filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(groups & filters.ANIMATION, handle_animation))
    app.add_handler(MessageHandler(groups & (filters.VIDEO | filters.VIDEO_NOTE), handle_video))
    app.add_handler(MessageHandler(groups & (filters.Document.IMAGE | filters.Document.VIDEO), handle_document))
    app.add_error_handler(error_handler)

    logger.info("Yuta bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
