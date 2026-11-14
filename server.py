import os
import shutil
import asyncio
import subprocess
from typing import Optional
from pathlib import Path

import pyperclip
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Depends, Query, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

# Добавляем папку скрипта в PATH для поиска ffmpeg.exe
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ["PATH"] = CURRENT_DIR + os.pathsep + os.environ.get("PATH", "")

AUTH_PIN = os.getenv("APP_PIN", "1234")
DOWNLOADS_DIR = str(Path.home() / "Downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

app = FastAPI(title="PC Home Hub & Media Converter")

def verify_auth(authorization: Optional[str] = Header(None)):
    if not authorization or authorization != f"Bearer {AUTH_PIN}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный PIN")
    return True

@app.post("/api/auth/verify")
def api_auth(auth: bool = Depends(verify_auth)):
    return {"status": "ok"}

# --- БУФЕР ОБМЕНА ---
class ClipboardPayload(BaseModel):
    text: str

@app.get("/api/clipboard")
def get_clipboard(auth: bool = Depends(verify_auth)):
    try:
        return {"text": pyperclip.paste() or ""}
    except Exception as e:
        return {"text": "", "error": str(e)}

@app.post("/api/clipboard")
def set_clipboard(payload: ClipboardPayload, auth: bool = Depends(verify_auth)):
    try:
        pyperclip.copy(payload.text)
        return {"status": "ok", "message": "Скопировано в буфер ПК"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- AIRDROP ФАЙЛОВ ---
@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), auth: bool = Depends(verify_auth)):
    try:
        file_path = os.path.join(DOWNLOADS_DIR, file.filename)
        base, ext = os.path.splitext(file.filename)
        counter = 1
        while os.path.exists(file_path):
            file_path = os.path.join(DOWNLOADS_DIR, f"{base}_{counter}{ext}")
            counter += 1

        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        return {"status": "ok", "filename": os.path.basename(file_path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/recent-files")
def get_recent_files(auth: bool = Depends(verify_auth)):
    try:
        files = []
        for p in Path(DOWNLOADS_DIR).iterdir():
            if p.is_file():
                files.append({
                    "name": p.name,
                    "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
                    "time": p.stat().st_mtime
                })
        files.sort(key=lambda x: x["time"], reverse=True)
        return files[:8]
    except Exception:
        return []

@app.get("/api/download-from-pc/{filename}")
def download_from_pc(filename: str, token: str = Query(...)):
    if token != AUTH_PIN:
        raise HTTPException(status_code=401, detail="Неверный токен")
    safe_path = os.path.join(DOWNLOADS_DIR, filename)
    if os.path.exists(safe_path) and os.path.isfile(safe_path):
        return FileResponse(safe_path, filename=filename)
    raise HTTPException(status_code=404, detail="Файл не найден")

# --- СКАЧИВАНИЕ С YOUTUBE/VK ---
class DownloadRequest(BaseModel):
    url: str
    mode: str

@app.post("/api/ytdl")
async def download_media(req: DownloadRequest, auth: bool = Depends(verify_auth)):
    def run_dl():
        if req.mode == "audio":
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(DOWNLOADS_DIR, '%(title)s.%(ext)s'),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'quiet': True,
                'no_warnings': True
            }
        else:
            ydl_opts = {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'outtmpl': os.path.join(DOWNLOADS_DIR, '%(title)s.%(ext)s'),
                'quiet': True,
                'no_warnings': True
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            return info.get('title', 'Медиафайл')

    try:
        title = await asyncio.to_thread(run_dl)
        return {"status": "ok", "message": f"Скачано: {title}"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка: {str(e)}")

# --- МУЛЬТИ-КОНВЕРТЕР И КОМПРЕССОР (FFmpeg) ---
@app.post("/api/convert")
async def media_converter(
    file: UploadFile = File(...),
    action: str = Form(...), # "to_mp3", "to_mp4", "to_gif", "compress_25", "compress_10"
    auth: bool = Depends(verify_auth)
):
    input_path = os.path.join(DOWNLOADS_DIR, f"temp_{file.filename}")
    base_name, _ = os.path.splitext(file.filename)

    with open(input_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    def process():
        try:
            # 1. Любое видео / аудио -> MP3
            if action == "to_mp3":
                output_name = f"{base_name}.mp3"
                output_path = os.path.join(DOWNLOADS_DIR, output_name)
                cmd = ["ffmpeg", "-y", "-i", input_path, "-vn", "-c:a", "libmp3lame", "-q:a", "2", output_path]
                subprocess.run(cmd, check=True)
                return output_name

            # 2. MP3 -> MP4 (видео с фоном) или любое видео -> стандартный MP4
            elif action == "to_mp4":
                output_name = f"{base_name}.mp4"
                output_path = os.path.join(DOWNLOADS_DIR, output_name)
                # Проверяем, это аудио или видео
                probe = subprocess.check_output([
                    "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                    "-of", "default=noprint_wrappers=1:nokey=1", input_path
                ], text=True)

                if "video" not in probe:
                    # Если аудио: создаем темный фон 1080p под трек
                    cmd = [
                        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x0f172a:s=1280x720:r=5",
                        "-i", input_path, "-c:v", "libx264", "-tune", "stillimage",
                        "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", "-shortest", output_path
                    ]
                else:
                    # Если видео другого формата (MKV, AVI, WebM) -> в MP4
                    cmd = ["ffmpeg", "-y", "-i", input_path, "-c:v", "libx264", "-c:a", "aac", output_path]

                subprocess.run(cmd, check=True)
                return output_name

            # 3. Видео -> GIF (анимация высокого качества через палитру)
            elif action == "to_gif":
                output_name = f"{base_name}.gif"
                output_path = os.path.join(DOWNLOADS_DIR, output_name)
                filters = "fps=12,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
                cmd = ["ffmpeg", "-y", "-i", input_path, "-vf", filters, "-loop", "0", output_path]
                subprocess.run(cmd, check=True)
                return output_name

            # 4. Сжатие до точного размера (25 МБ для Discord или 10 МБ)
            elif action in ("compress_25", "compress_10"):
                target_mb = 25 if action == "compress_25" else 10
                output_name = f"{base_name}_{target_mb}MB.mp4"
                output_path = os.path.join(DOWNLOADS_DIR, output_name)

                probe_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", input_path]
                duration = float(subprocess.check_output(probe_cmd, text=True).strip())

                target_bits = target_mb * 8 * 1024 * 1024 * 0.95
                audio_k = 128
                video_k = max(int((target_bits / duration / 1000) - audio_k), 100)

                cmd = ["ffmpeg", "-y", "-i", input_path, "-c:v", "libx264", "-b:v", f"{video_k}k", "-c:a", "aac", "-b:a", f"{audio_k}k", output_path]
                subprocess.run(cmd, check=True)
                return output_name

            raise ValueError("Неизвестное действие")
        finally:
            if os.path.exists(input_path):
                os.remove(input_path)

    try:
        result_filename = await asyncio.to_thread(process)
        return {"status": "ok", "filename": result_filename, "message": f"Готово: {result_filename}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка обработки: {str(e)}")

# Статика
os.makedirs(os.path.join(CURRENT_DIR, "static"), exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(CURRENT_DIR, "static")), name="static")

@app.get("/")
def get_root():
    return FileResponse(os.path.join(CURRENT_DIR, "static", "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)