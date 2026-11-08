import asyncio
import logging
import os
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("encoder-bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Opcional: servidor local de Bot API (permite archivos de hasta 2 GB)
LOCAL_API = os.environ.get("TELEGRAM_API_URL")  # ej: http://localhost:8081

# Límites de la API oficial (sin servidor local)
MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_UPLOAD = 50 * 1024 * 1024

# CRF por códec y calidad (menor CRF = mejor calidad y más peso)
QUALITIES = {
    "alta": {"label": "🟢 Alta", "264": 19, "265": 23},
    "media": {"label": "🟡 Media", "264": 23, "265": 28},
    "baja": {"label": "🟠 Baja", "264": 28, "265": 32},
    "minima": {"label": "🔴 Mínima", "264": 32, "265": 36},
}

# Solo un encode a la vez (el Codespace tiene pocos núcleos)
ENCODE_LOCK = asyncio.Semaphore(1)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Mándame un video (o archivo de video) y lo recomprimo en "
        "H.264 o H.265 con la calidad que elijas."
    )


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    media = msg.video or msg.document
    if media is None:
        return
    if not LOCAL_API and media.file_size and media.file_size > MAX_DOWNLOAD:
        await msg.reply_text(
            f"❌ El archivo pesa {human(media.file_size)}. La API oficial solo "
            "permite descargar hasta 20 MB. Mira el README para usar un "
            "servidor Bot API local (hasta 2 GB)."
        )
        return

    context.user_data["job"] = {
        "file_id": media.file_id,
        "name": getattr(media, "file_name", None) or "video.mp4",
        "size": media.file_size or 0,
    }
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("H.264 (compatible)", callback_data="codec:264"),
            InlineKeyboardButton("H.265 (más liviano)", callback_data="codec:265"),
        ]]
    )
    await msg.reply_text("¿Qué códec quieres?", reply_markup=kb)


async def on_codec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    job = context.user_data.get("job")
    if not job:
        await q.edit_message_text("Mándame el video de nuevo 🙏")
        return
    job["codec"] = q.data.split(":")[1]

    rows, row = [], []
    for key, v in QUALITIES.items():
        row.append(
            InlineKeyboardButton(
                f"{v['label']} (CRF {v[job['codec']]})", callback_data=f"q:{key}"
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await q.edit_message_text(
        f"H.{job['codec']} ✅\nAhora elige la calidad:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    job = context.user_data.pop("job", None)
    if not job or "codec" not in job:
        await q.edit_message_text("Mándame el video de nuevo 🙏")
        return

    codec = job["codec"]
    quality = q.data.split(":")[1]
    crf = QUALITIES[quality][codec]
    chat_id = q.message.chat_id
    status = q.message
    await status.edit_text(
        f"⏳ En cola: H.{codec}, calidad {QUALITIES[quality]['label']} (CRF {crf})…"
    )

    async with ENCODE_LOCK:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input"
            dst = Path(tmp) / f"{Path(job['name']).stem}_h{codec}.mp4"
            try:
                await status.edit_text("⬇️ Descargando…")
                tg_file = await context.bot.get_file(job["file_id"])
                await tg_file.download_to_drive(src)

                await status.edit_text(
                    f"⚙️ Codificando en H.{codec} (CRF {crf})… puede tardar."
                )
                cmd = [
                    "ffmpeg", "-y", "-i", str(src),
                    "-c:v", "libx264" if codec == "264" else "libx265",
                    "-crf", str(crf),
                    "-preset", "veryfast",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart",
                ]
                if codec == "265":
                    cmd += ["-tag:v", "hvc1", "-x265-params", "log-level=error"]
                cmd.append(str(dst))

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, err = await proc.communicate()
                if proc.returncode != 0:
                    log.error(err.decode(errors="ignore")[-1500:])
                    await status.edit_text("❌ ffmpeg falló. ¿Es un video válido?")
                    return

                out_size = dst.stat().st_size
                if not LOCAL_API and out_size > MAX_UPLOAD:
                    await status.edit_text(
                        f"❌ El resultado pesa {human(out_size)} y supera los "
                        "50 MB que permite un bot. Prueba una calidad más baja."
                    )
                    return

                await status.edit_text("⬆️ Subiendo…")
                caption = (
                    f"H.{codec} · {QUALITIES[quality]['label']} (CRF {crf})\n"
                    f"{human(job['size'])} → {human(out_size)}"
                )
                with open(dst, "rb") as f:
                    await context.bot.send_document(
                        chat_id, f, filename=dst.name, caption=caption,
                        read_timeout=600, write_timeout=600,
                    )
                await status.delete()
            except Exception:
                log.exception("Error procesando")
                await status.edit_text("❌ Ocurrió un error procesando el video.")


def main():
    builder = Application.builder().token(TOKEN).concurrent_updates(True)
    if LOCAL_API:
        builder = (
            builder.base_url(f"{LOCAL_API}/bot")
            .base_file_url(f"{LOCAL_API}/file/bot")
            .local_mode(True)
        )
    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.add_handler(CallbackQueryHandler(on_codec, pattern=r"^codec:"))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q:"))

    log.info("Bot iniciado")
    app.run_polling()


if __name__ == "__main__":
    main()
