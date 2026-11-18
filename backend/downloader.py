import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
import yt_dlp

from backend.config import DEFAULT_DOWNLOAD_DIR
from backend.security import detect_platform

jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()


def get_ffmpeg_location() -> Optional[str]:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


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


def expand_subtitle_langs(lang_code: str) -> List[str]:
    code = (lang_code or "").strip().lower()
    if not code or not re.match(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$", code):
        return []
    if code == "zh":
        return ["zh-Hans", "zh-Hant", "zh"]
    if code == "en":
        return ["en", "en-US", "en-GB"]
    return [code]


def run_download_job(job_id: str, url: str, quality: str, subtitle_lang: str = "") -> None:
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
            update_job(status="downloading", percent=percent, speed=speed, eta=eta)
        elif status == "finished":
            update_job(status="processing", percent=100.0, speed="", eta="")

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

    sub_langs = expand_subtitle_langs(subtitle_lang)
    if sub_langs and quality != "audio":
        ydl_opts["writesubtitles"] = True
        ydl_opts["writeautomaticsub"] = True
        ydl_opts["subtitleslangs"] = sub_langs
        ydl_opts["subtitlesformat"] = "srt/vtt/best"
        if ffmpeg_loc:
            pps = list(ydl_opts.get("postprocessors") or [])
            pps.append({"key": "FFmpegEmbedSubtitle"})
            ydl_opts["postprocessors"] = pps

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


def search_youtube_items(query: str, limit: int) -> List[Dict[str, Any]]:
    cleaned = query.strip()
    if len(cleaned) < 2:
        raise HTTPException(status_code=400, detail="Arama için en az 2 karakter girin.")
    bounded_limit = max(1, min(limit, 60))
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(f"ytsearch{bounded_limit}:{cleaned}", download=False)
        entries = (data or {}).get("entries") or []
        results: List[Dict[str, Any]] = []
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
            results.append({
                "id": vid_id,
                "title": item.get("title") or "Başlıksız Video",
                "url": vid_url,
                "uploader": item.get("uploader") or item.get("channel") or "YouTube",
                "duration": item.get("duration") or 0,
                "thumbnail": thumb or "",
            })
        return results
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"YouTube araması başarısız: {strip_ansi(str(exc))}")


def extract_video_info(url: str) -> Dict[str, Any]:
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
        return {
            "title": info.get("title") or "Başlıksız Video",
            "uploader": info.get("uploader") or info.get("channel") or "Bilinmiyor",
            "duration": info.get("duration") or 0,
            "thumbnail": info.get("thumbnail") or "",
            "platform": detect_platform(url),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Video bilgileri alınamadı: {strip_ansi(str(exc))}")


def create_download_job(url: str, quality: str, subtitle_lang: str = "") -> str:
    clean_quality = quality if quality in ("best", "1080", "720", "480", "audio") else "best"
    clean_sub = subtitle_lang.strip().lower() if expand_subtitle_langs(subtitle_lang) else ""
    job_id = str(uuid.uuid4())[:8]

    with jobs_lock:
        if len(jobs) > 200:
            done_ids = [jid for jid, j in jobs.items() if j.get("status") in ("completed", "error")]
            for jid in done_ids[: len(done_ids) // 2 + 1]:
                jobs.pop(jid, None)
        jobs[job_id] = {
            "job_id": job_id,
            "url": url,
            "platform": detect_platform(url),
            "quality": clean_quality,
            "subtitle_lang": clean_sub,
            "status": "queued",
            "percent": 0.0,
            "speed": "",
            "eta": "",
            "title": "",
            "filename": "",
            "filepath": "",
            "error": "",
        }

    thread = threading.Thread(
        target=run_download_job, args=(job_id, url, clean_quality, clean_sub), daemon=True
    )
    thread.start()
    return job_id


def get_job_status(job_id: str) -> Dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="İndirme görevi bulunamadı.")
        return dict(job)
