import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import qrcode
import qrcode.image.svg
import yt_dlp

from version import APP_VERSION, UPDATE_MANIFEST_URL

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent

STATIC_DIR = BASE_DIR / "static"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "VideoIndirici"
DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Video İndirici (YouTube & Instagram)")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()


class InfoRequest(BaseModel):
    url: str


class SearchRequest(BaseModel):
    query: str
    limit: int = 25


class DownloadRequest(BaseModel):
    url: str
    quality: str = "best"  # best, 1080, 720, 480, audio


class ApplyUpdateRequest(BaseModel):
    download_url: str


def get_ffmpeg_location() -> Optional[str]:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def parse_version(v: str) -> tuple:
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts) if parts else (0,)


def detect_platform(url: str) -> str:
    lower = url.lower()
    if any(domain in lower for domain in ("youtube.com", "youtu.be", "youtube-nocookie.com")):
        return "YouTube"
    if any(domain in lower for domain in ("instagram.com", "instagr.am")):
        return "Instagram"
    return "Diğer"


def validate_url(url: str) -> str:
    cleaned = url.strip()
    if not re.match(r"^https?://", cleaned, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Geçerli bir http:// veya https:// bağlantısı girin.")
    platform = detect_platform(cleaned)
    if platform == "Diğer":
        raise HTTPException(
            status_code=400,
            detail="Şu anda yalnızca YouTube ve Instagram bağlantıları desteklenmektedir.",
        )
    return cleaned


def build_format_selector(quality: str) -> Dict[str, Any]:
    if quality == "audio":
        return {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
    if quality in ("1080", "720", "480"):
        height = int(quality)
        return {
            "format": (
                f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[height<={height}][ext=mp4]/best[height<={height}]/best"
            ),
            "merge_output_format": "mp4",
        }
    return {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
        "merge_output_format": "mp4",
    }


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text or "").strip()


def format_speed(speed_bytes: Optional[float]) -> str:
    if not speed_bytes or speed_bytes <= 0:
        return ""
    if speed_bytes >= 1024 * 1024:
        return f"{speed_bytes / (1024 * 1024):.1f} MB/s"
    return f"{speed_bytes / 1024:.0f} KB/s"


def format_eta(eta_sec: Optional[float]) -> str:
    if eta_sec is None or eta_sec < 0:
        return ""
    sec = int(eta_sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}sa {m}dk"
    if m > 0:
        return f"{m}dk {s}sn"
    return f"{s}sn"


def run_download_job(job_id: str, url: str, quality: str) -> None:
    def update_job(**kwargs: Any) -> None:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(kwargs)

    def progress_hook(d: Dict[str, Any]) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            percent = round(min(100.0, (downloaded / total) * 100), 1) if total > 0 else 0.0
            speed = format_speed(d.get("speed")) or strip_ansi(d.get("_speed_str", ""))
            eta = format_eta(d.get("eta")) or strip_ansi(d.get("_eta_str", ""))
            update_job(
                status="downloading",
                percent=percent,
                speed=speed,
                eta=eta,
            )
        elif status == "finished":
            update_job(
                status="processing",
                percent=100.0,
                speed="",
                eta="",
            )

    fmt_opts = build_format_selector(quality)
    ydl_opts: Dict[str, Any] = {
        "outtmpl": str(DEFAULT_DOWNLOAD_DIR / "%(title).120B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "progress_hooks": [progress_hook],
        **fmt_opts,
    }
    ffmpeg_loc = get_ffmpeg_location()
    if ffmpeg_loc:
        ydl_opts["ffmpeg_location"] = ffmpeg_loc

    try:
        update_job(status="downloading", percent=0.0)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title") or "Video"
            requested_downloads = info.get("requested_downloads") or []
            filepath: Optional[str] = None
            if requested_downloads:
                filepath = requested_downloads[0].get("filepath")
            if not filepath:
                filepath = ydl.prepare_filename(info)
                if quality == "audio":
                    filepath = str(Path(filepath).with_suffix(".mp3"))
                elif Path(filepath).with_suffix(".mp4").exists():
                    filepath = str(Path(filepath).with_suffix(".mp4"))

            filename = Path(filepath).name if filepath else title
            update_job(
                status="completed",
                percent=100.0,
                title=title,
                filename=filename,
                filepath=filepath,
            )
    except Exception as exc:
        update_job(status="error", error=strip_ansi(str(exc)))


@app.get("/")
def read_root() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/telefon")
def read_phone_page() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "phone.html"))


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


@app.get("/api/phone-link")
def get_phone_link(request: Request) -> JSONResponse:
    ip = get_local_ip()
    port = os.environ.get("VIDEO_INDIRICI_PORT") or str(request.url.port or 8765)
    url = f"http://{ip}:{port}/telefon"

    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    qr_svg = buf.getvalue().decode("utf-8")

    return JSONResponse({"url": url, "ip": ip, "port": port, "qr_svg": qr_svg})


@app.get("/api/file/{filename}")
def serve_downloaded_file(filename: str, download: int = 0) -> FileResponse:
    safe_name = Path(filename).name
    target = (DEFAULT_DOWNLOAD_DIR / safe_name).resolve()
    if not target.is_file() or target.parent != DEFAULT_DOWNLOAD_DIR.resolve():
        raise HTTPException(status_code=404, detail="Dosya bulunamadı.")

    headers = {}
    if download:
        encoded = urllib.parse.quote(safe_name)
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded}"
    return FileResponse(str(target), headers=headers)


@app.post("/api/open-file/{filename}")
def open_downloaded_file(filename: str) -> JSONResponse:
    safe_name = Path(filename).name
    target = (DEFAULT_DOWNLOAD_DIR / safe_name).resolve()
    if not target.is_file() or target.parent != DEFAULT_DOWNLOAD_DIR.resolve():
        raise HTTPException(status_code=404, detail="Dosya bulunamadı.")
    if os.name == "nt":
        os.startfile(str(target))
    else:
        subprocess.Popen(["xdg-open", str(target)])
    return JSONResponse({"ok": True})


@app.delete("/api/file/{filename}")
def delete_downloaded_file(filename: str) -> JSONResponse:
    safe_name = Path(filename).name
    target = (DEFAULT_DOWNLOAD_DIR / safe_name).resolve()
    if not target.is_file() or target.parent != DEFAULT_DOWNLOAD_DIR.resolve():
        raise HTTPException(status_code=404, detail="Dosya bulunamadı.")
    target.unlink()
    return JSONResponse({"ok": True})


@app.get("/api/version")
def get_version() -> JSONResponse:
    return JSONResponse(
        {
            "version": APP_VERSION,
            "frozen": getattr(sys, "frozen", False),
        }
    )


@app.get("/api/check-update")
def check_update() -> JSONResponse:
    if "OWNER/" in UPDATE_MANIFEST_URL:
        return JSONResponse(
            {
                "current_version": APP_VERSION,
                "update_available": False,
                "configured": False,
                "message": "Güncelleme sunucusu henüz yapılandırılmadı (version.py içinde GITHUB_REPO ayarlayın).",
            }
        )
    try:
        req = urllib.request.Request(
            UPDATE_MANIFEST_URL,
            headers={"User-Agent": f"VideoIndirici/{APP_VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = str(data.get("version", APP_VERSION)).lstrip("v")
        update_available = parse_version(latest) > parse_version(APP_VERSION)
        return JSONResponse(
            {
                "current_version": APP_VERSION,
                "latest_version": latest,
                "update_available": update_available,
                "configured": True,
                "download_url": data.get("download_url", ""),
                "notes": data.get("notes", ""),
            }
        )
    except Exception as exc:
        return JSONResponse(
            {
                "current_version": APP_VERSION,
                "update_available": False,
                "configured": True,
                "error": f"Güncelleme kontrolü başarısız: {exc}",
            }
        )


@app.post("/api/apply-update")
def apply_update(req: ApplyUpdateRequest) -> JSONResponse:
    if not getattr(sys, "frozen", False):
        raise HTTPException(
            status_code=400,
            detail="Otomatik güncelleme yalnızca paketlenmiş .exe uygulamasında çalışır.",
        )
    if not req.download_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Geçersiz güncelleme bağlantısı.")

    current_exe = Path(sys.executable).resolve()
    temp_dir = Path(tempfile.gettempdir())
    new_exe = temp_dir / f"VideoIndirici_new_{uuid.uuid4().hex[:6]}.exe"
    updater_bat = temp_dir / f"update_video_indirici_{uuid.uuid4().hex[:6]}.bat"

    try:
        url_req = urllib.request.Request(
            req.download_url,
            headers={"User-Agent": f"VideoIndirici/{APP_VERSION}"},
        )
        with urllib.request.urlopen(url_req, timeout=60) as resp, open(new_exe, "wb") as out:
            shutil.copyfileobj(resp, out)

        pid = os.getpid()
        bat_content = (
            "@echo off\n"
            "set _MEIPASS2=\n"
            "set PYTHONHOME=\n"
            "set PYTHONPATH=\n"
            f"timeout /t 2 /nobreak >nul\n"
            f":waitloop\n"
            f"tasklist /FI \"PID eq {pid}\" 2>NUL | find /I \"{pid}\" >NUL\n"
            f"if not errorlevel 1 (\n"
            f"  timeout /t 1 /nobreak >nul\n"
            f"  goto waitloop\n"
            f")\n"
            f"timeout /t 1 /nobreak >nul\n"
            f"move /Y \"{new_exe}\" \"{current_exe}\"\n"
            f"start \"\" \"{current_exe}\"\n"
            f"del \"%~f0\"\n"
        )
        updater_bat.write_text(bat_content, encoding="utf-8")
        clean_env = os.environ.copy()
        clean_env.pop("_MEIPASS2", None)
        clean_env.pop("PYTHONHOME", None)
        clean_env.pop("PYTHONPATH", None)
        subprocess.Popen(
            ["cmd", "/c", str(updater_bat)],
            env=clean_env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        def exit_soon() -> None:
            import time

            time.sleep(0.6)
            os._exit(0)

        threading.Thread(target=exit_soon, daemon=True).start()
        return JSONResponse({"ok": True, "message": "Güncelleme indirildi, uygulama yeniden başlatılıyor..."})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Güncelleme uygulanamadı: {exc}")


@app.post("/api/search")
def search_youtube(req: SearchRequest) -> JSONResponse:
    query = req.query.strip()
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Arama için en az 2 karakter girin.")
    limit = max(1, min(req.limit, 60))
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        entries = (data or {}).get("entries") or []
        results = []
        for item in entries:
            if not item:
                continue
            vid_id = item.get("id") or ""
            vid_url = item.get("url") or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else "")
            if vid_url and not vid_url.startswith("http"):
                vid_url = f"https://www.youtube.com/watch?v={vid_url}"
            thumb = item.get("thumbnail")
            if not thumb and item.get("thumbnails"):
                thumb = item["thumbnails"][-1].get("url")
            if not thumb and vid_id:
                thumb = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
            results.append(
                {
                    "id": vid_id,
                    "title": item.get("title") or "Başlıksız Video",
                    "url": vid_url,
                    "uploader": item.get("uploader") or item.get("channel") or "YouTube",
                    "duration": item.get("duration") or 0,
                    "thumbnail": thumb or "",
                }
            )
        return JSONResponse({"results": results})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"YouTube araması başarısız: {strip_ansi(str(exc))}")


@app.post("/api/info")
def get_video_info(req: InfoRequest) -> JSONResponse:
    url = validate_url(req.url)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise HTTPException(status_code=400, detail="Video bilgileri alınamadı.")
        return JSONResponse(
            {
                "title": info.get("title") or "Başlıksız Video",
                "uploader": info.get("uploader") or info.get("channel") or "Bilinmiyor",
                "duration": info.get("duration") or 0,
                "thumbnail": info.get("thumbnail") or "",
                "platform": detect_platform(url),
            }
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Video bilgileri alınamadı: {strip_ansi(str(exc))}")


@app.post("/api/download")
def start_download(req: DownloadRequest) -> JSONResponse:
    url = validate_url(req.url)
    quality = req.quality if req.quality in ("best", "1080", "720", "480", "audio") else "best"
    job_id = str(uuid.uuid4())[:8]

    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "url": url,
            "platform": detect_platform(url),
            "quality": quality,
            "status": "queued",
            "percent": 0.0,
            "speed": "",
            "eta": "",
            "title": "",
            "filename": "",
            "filepath": "",
            "error": "",
        }

    thread = threading.Thread(target=run_download_job, args=(job_id, url, quality), daemon=True)
    thread.start()
    return JSONResponse({"job_id": job_id})


@app.get("/api/status/{job_id}")
def get_status(job_id: str) -> JSONResponse:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="İndirme görevi bulunamadı.")
        return JSONResponse(dict(job))


@app.get("/api/downloads")
def list_downloads() -> JSONResponse:
    files = []
    if DEFAULT_DOWNLOAD_DIR.exists():
        for entry in sorted(DEFAULT_DOWNLOAD_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if entry.is_file() and not entry.name.endswith(".part"):
                size_mb = round(entry.stat().st_size / (1024 * 1024), 2)
                files.append({"name": entry.name, "size_mb": size_mb})
    return JSONResponse({"folder": str(DEFAULT_DOWNLOAD_DIR), "files": files[:15]})


@app.post("/api/open-folder")
def open_folder() -> JSONResponse:
    DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(DEFAULT_DOWNLOAD_DIR))
    else:
        subprocess.Popen(["xdg-open", str(DEFAULT_DOWNLOAD_DIR)])
    return JSONResponse({"ok": True, "folder": str(DEFAULT_DOWNLOAD_DIR)})
