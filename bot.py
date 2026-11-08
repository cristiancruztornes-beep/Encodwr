import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path

from telethon import Button, TelegramClient, events

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("encoder")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

client = TelegramClient("encoder_bot", API_ID, API_HASH)

# CRF por códec y calidad (menor CRF = mejor calidad y más peso)
QUALITIES = {
    "alta": ("🟢 Alta", 19, 23),
    "media": ("🟡 Media", 23, 28),
    "baja": ("🟠 Baja", 28, 32),
    "minima": ("🔴 Mínima", 32, 36),
}

jobs = {}  # user_id -> datos del video pendiente
LOCK = asyncio.Semaphore(1)  # un encode a la vez


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def progress_cb(msg, label):
    last = [0.0]

    async def cb(done, total):
        if time.time() - last[0] < 4:
            return
        last[0] = time.time()
        try:
            await msg.edit(f"{label} {done * 100 // max(total, 1)}% ({human(done)}/{human(total)})")
        except Exception:
            pass

    return cb


@client.on(events.NewMessage(pattern="/start"))
async def start(e):
    await e.respond("👋 Mándame un video y lo recomprimo en H.264 o H.265 con la calidad que elijas (hasta 2 GB).")


@client.on(events.NewMessage(func=lambda e: e.is_private and (e.video or (e.document and (e.document.mime_type or "").startswith("video/")))))
async def on_video(e):
    jobs[e.sender_id] = {
        "msg": e.message,
        "name": (e.file.name if e.file and e.file.name else "video.mp4"),
        "size": e.file.size if e.file else 0,
    }
    await e.respond(
        "¿Qué códec quieres?",
        buttons=[[Button.inline("H.264 (compatible)", b"codec:264"),
                  Button.inline("H.265 (más liviano)", b"codec:265")]],
    )


@client.on(events.CallbackQuery(pattern=rb"codec:(\d+)"))
async def on_codec(e):
    job = jobs.get(e.sender_id)
    if not job:
        await e.answer("Mándame el video de nuevo", alert=True)
        return
    codec = e.pattern_match.group(1).decode()
    job["codec"] = codec
    idx = 1 if codec == "264" else 2
    btns = [Button.inline(f"{v[0]} (CRF {v[idx]})", f"q:{k}".encode()) for k, v in QUALITIES.items()]
    await e.edit(f"H.{codec} ✅\nAhora elige la calidad:", buttons=[btns[:2], btns[2:]])


@client.on(events.CallbackQuery(pattern=rb"q:(\w+)"))
async def on_quality(e):
    job = jobs.pop(e.sender_id, None)
    if not job or "codec" not in job:
        await e.answer("Mándame el video de nuevo", alert=True)
        return
    codec = job["codec"]
    key = e.pattern_match.group(1).decode()
    label, crf264, crf265 = QUALITIES[key]
    crf = crf264 if codec == "264" else crf265
    status = await e.edit(f"⏳ En cola: H.{codec}, {label} (CRF {crf})…")

    async with LOCK:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input"
            dst = Path(tmp) / f"{Path(job['name']).stem}_h{codec}.mp4"
            try:
                await status.edit("⬇️ Descargando…")
                await client.download_media(job["msg"], file=str(src),
                                            progress_callback=progress_cb(status, "⬇️ Descargando"))

                await status.edit(f"⚙️ Codificando en H.{codec} (CRF {crf})… puede tardar.")
                cmd = ["ffmpeg", "-y", "-i", str(src),
                       "-c:v", "libx264" if codec == "264" else "libx265",
                       "-crf", str(crf), "-preset", "veryfast", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
                if codec == "265":
                    cmd += ["-tag:v", "hvc1", "-x265-params", "log-level=error"]
                cmd.append(str(dst))
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, err = await proc.communicate()
                if proc.returncode != 0:
                    log.error(err.decode(errors="ignore")[-1500:])
                    await status.edit("❌ ffmpeg falló. ¿Es un video válido?")
                    return

                out = dst.stat().st_size
                await status.edit("⬆️ Subiendo…")
                await client.send_file(
                    e.chat_id, str(dst),
                    caption=f"H.{codec} · {label} (CRF {crf})\n{human(job['size'])} → {human(out)}",
                    force_document=(codec == "265"),
                    supports_streaming=True,
                    progress_callback=progress_cb(status, "⬆️ Subiendo"),
                )
                await status.delete()
            except Exception:
                log.exception("Error procesando")
                await status.edit("❌ Ocurrió un error procesando el video.")


async def main():
    await client.start(bot_token=TOKEN)
    log.info("Bot iniciado")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
