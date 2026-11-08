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

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
client = TelegramClient("encoder_bot", API_ID, API_HASH, loop=loop)

# (nombre, CRF H.264, CRF H.265)  menor CRF = mejor calidad y más peso
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


def bar(pct, width=14):
    pct = max(0, min(100, pct))
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def fmt_time(s):
    s = int(max(0, s))
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def progress_cb(msg, label):
    last = [0.0]

    async def cb(done, total):
        if time.time() - last[0] < 4:
            return
        last[0] = time.time()
        pct = done * 100 / max(total, 1)
        try:
            await msg.edit(f"{label}\n{bar(pct)} {pct:.0f}%\n{human(done)} / {human(total)}")
        except Exception:
            pass

    return cb


async def get_duration(path):
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except Exception:
        return 0.0


async def encode(src, dst, codec, crf, status):
    """Corre ffmpeg y va mostrando barra de progreso. Devuelve (ok, log_error)."""
    duration = await get_duration(src)
    cmd = ["ffmpeg", "-y", "-i", str(src),
           "-c:v", "libx264" if codec == "264" else "libx265",
           "-crf", str(crf), "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
           "-progress", "pipe:1", "-nostats"]
    if codec == "265":
        cmd += ["-tag:v", "hvc1", "-x265-params", "log-level=error"]
    cmd.append(str(dst))

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    err_buf = bytearray()

    async def read_err():
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            err_buf.extend(chunk)
            del err_buf[:-4000]

    err_task = asyncio.create_task(read_err())

    start = time.time()
    last_edit = 0.0
    cur = 0.0
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        k, _, v = line.decode(errors="ignore").strip().partition("=")
        if k == "out_time_us":
            try:
                cur = int(v) / 1_000_000
            except ValueError:
                pass
        elif k == "progress" and time.time() - last_edit >= 5:
            last_edit = time.time()
            elapsed = time.time() - start
            head = f"⚙️ Codificando H.{codec} (CRF {crf})"
            if duration > 0 and cur > 0:
                pct = min(99.0, cur * 100 / duration)
                speed = cur / max(elapsed, 0.1)
                eta = (duration - cur) / speed
                text = (f"{head}\n{bar(pct)} {pct:.0f}%\n"
                        f"🎞 {fmt_time(cur)} / {fmt_time(duration)}\n"
                        f"⏱ {fmt_time(elapsed)} · quedan ~{fmt_time(eta)} · {speed:.2f}x")
            else:
                text = f"{head}\n⏱ {fmt_time(elapsed)}"
            try:
                await status.edit(text)
            except Exception:
                pass

    await proc.wait()
    await err_task
    return proc.returncode == 0, err_buf.decode(errors="ignore")


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
                src = await client.download_media(
                    job["msg"], file=str(src),
                    progress_callback=progress_cb(status, "⬇️ Descargando"))

                ok, err = await encode(src, dst, codec, crf, status)
                if not ok:
                    log.error(err)
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
    loop.run_until_complete(main())
