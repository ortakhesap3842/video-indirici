import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
import yt_dlp

from backend.config import DEFAULT_DOWNLOAD_DIR
from backend.dubbing import (
    NEURAL_VOICES,
    apply_ai_dubbing,
    parse_subtitle_cues,
    transcribe_video_to_srt,
    translate_cues_contextual,
)
from backend.security import detect_platform
from backend import storage
from backend.universal import discover_embedded_media_url

jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()


def _get_yt_cookies_opt() -> Optional[str]:
    env_c = os.environ.get("YT_COOKIES_FILE", "").strip()
    if env_c and Path(env_c).is_file():
        return env_c
    default_c = Path.home() / ".video_indirici" / "cookies.txt"
    if default_c.is_file():
        return str(default_c)
    return None


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
        return ["zh-Hans"]
    return [code]


def _write_clean_srt(sub_path: Path) -> Path:
    try:
        cues = parse_subtitle_cues(sub_path)
        if not cues:
            return sub_path
        clean_srt = sub_path.with_suffix(".clean.srt")
        blocks = []
        for idx, (s_ms, e_ms, txt) in enumerate(cues, start=1):
            def _fmt(ms: int) -> str:
                ms = max(0, int(ms))
                h, r = divmod(ms, 3600000)
                m, r = divmod(r, 60000)
                s, ml = divmod(r, 1000)
                return f"{h:02d}:{m:02d}:{s:02d},{ml:03d}"
            blocks.append(f"{idx}\n{_fmt(s_ms)} --> {_fmt(e_ms)}\n{txt}")
        clean_srt.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
        return clean_srt
    except Exception:
        return sub_path


def run_download_job(
    job_id: str, url: str, quality: str, subtitle_lang: str = "", dub_lang: str = "", gemini_api_key: str = ""
) -> None:
    def update_job(**kwargs: Any) -> None:
        snapshot: Optional[Dict[str, Any]] = None
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(kwargs)
                snapshot = dict(jobs[job_id])
        if snapshot is not None and kwargs.get("status") in ("completed", "error", "dubbing", "transcribing", "processing"):
            storage.save_job(job_id, snapshot)

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
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "tv", "web"]}},
        "progress_hooks": [progress_hook],
        **fmt_opts,
    }
    cookie_file = _get_yt_cookies_opt()
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
    ffmpeg_loc = get_ffmpeg_location()
    if ffmpeg_loc:
        ydl_opts["ffmpeg_location"] = ffmpeg_loc

    sub_langs = expand_subtitle_langs(subtitle_lang)
    dub_code = dub_lang.strip().lower() if dub_lang and dub_lang.strip().lower() in NEURAL_VOICES else ""

    try:
        update_job(status="downloading", percent=0.0)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as first_exc:
            embedded = discover_embedded_media_url(url)
            if not embedded or embedded == url:
                raise first_exc
            fallback_opts = {**ydl_opts, "http_headers": {"Referer": url}}
            with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                info = ydl.extract_info(embedded, download=True)
        title = info.get("title") or "Video"
        requested_downloads = info.get("requested_downloads") or []
        filepath: Optional[str] = None
        if requested_downloads:
            filepath = requested_downloads[0].get("filepath")
        if not filepath:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl_name:
                filepath = ydl_name.prepare_filename(info)
            if quality == "audio":
                filepath = str(Path(filepath).with_suffix(".mp3"))
            elif Path(filepath).with_suffix(".mp4").exists():
                filepath = str(Path(filepath).with_suffix(".mp4"))

        if (sub_langs or dub_code) and quality != "audio" and filepath:
            avail_subs = list((info.get("subtitles") or {}).keys())
            avail_auto = list((info.get("automatic_captions") or {}).keys())
            all_avail = avail_subs + [k for k in avail_auto if k not in avail_subs]
            want_langs: List[str] = []
            if sub_langs and sub_langs[0] in all_avail:
                want_langs = [sub_langs[0]]
            elif dub_code and dub_code in all_avail:
                want_langs = [dub_code]
            else:
                vid_lang = (info.get("language") or "").lower()
                if vid_lang and vid_lang in all_avail:
                    want_langs = [vid_lang]
                elif "en" in all_avail:
                    want_langs = ["en"]
                elif all_avail:
                    want_langs = [all_avail[0]]
                elif sub_langs:
                    want_langs = sub_langs[:1]
                elif dub_code:
                    want_langs = [dub_code]
            sub_opts: Dict[str, Any] = {
                "outtmpl": str(DEFAULT_DOWNLOAD_DIR / "%(title).120B [%(id)s].%(ext)s"),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": want_langs[:1],
                "subtitlesformat": "srt/vtt/best",
                "sleep_interval_subtitles": 2,
            }
            try:
                with yt_dlp.YoutubeDL(sub_opts) as sydl:
                    sydl.extract_info(url, download=True)
            except Exception:
                pass

            stem = str(Path(filepath).with_suffix(""))
            sub_candidate: Optional[Path] = None
            for p in Path(filepath).parent.iterdir():
                if str(p).startswith(stem) and p.suffix.lower() in (".srt", ".vtt"):
                    sub_candidate = p
                    break
            if sub_candidate is None and ffmpeg_loc:
                update_job(status="transcribing", percent=100.0, speed="", eta="")
                stt_srt = Path(filepath).with_suffix(".whisper.srt")
                sub_candidate = transcribe_video_to_srt(
                    Path(filepath), ffmpeg_loc, stt_srt, gemini_api_key=gemini_api_key
                )
            if sub_candidate is not None and ffmpeg_loc:
                if sub_langs and sub_langs[0] not in sub_candidate.name.lower():
                    cues = parse_subtitle_cues(sub_candidate)
                    if cues:
                        tr_texts = translate_cues_contextual([c[2] for c in cues], sub_langs[0], gemini_api_key)
                        tr_srt = Path(filepath).with_suffix(f".{sub_langs[0]}.srt")
                        tr_blocks = []
                        for idx, (c, tr_t) in enumerate(zip(cues, tr_texts), start=1):
                            def _fmt_ms(ms: int) -> str:
                                ms = max(0, int(ms))
                                h, r = divmod(ms, 3600000)
                                m, r = divmod(r, 60000)
                                s, ml = divmod(r, 1000)
                                return f"{h:02d}:{m:02d}:{s:02d},{ml:03d}"
                            tr_blocks.append(f"{idx}\n{_fmt_ms(c[0])} --> {_fmt_ms(c[1])}\n{tr_t}")
                        tr_srt.write_text("\n\n".join(tr_blocks) + "\n", encoding="utf-8")
                        sub_candidate = tr_srt
                clean_sub_file = _write_clean_srt(sub_candidate)
                if sub_langs and Path(filepath).suffix.lower() == ".mp4":
                    tmp_sub_mp4 = Path(filepath).with_name(f"{Path(filepath).stem}.subtmp.mp4")
                    safe_sub_name = clean_sub_file.name.replace("'", "\\'")
                    burn_res = subprocess.run(
                        [
                            ffmpeg_loc, "-y", "-i", str(filepath),
                            "-vf", f"subtitles='{safe_sub_name}'",
                            "-c:a", "copy", str(tmp_sub_mp4),
                        ],
                        cwd=str(clean_sub_file.parent),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
                    )
                    if burn_res.returncode != 0 or not tmp_sub_mp4.exists() or tmp_sub_mp4.stat().st_size == 0:
                        tmp_sub_mp4.unlink(missing_ok=True)
                        burn_res = subprocess.run(
                            [
                                ffmpeg_loc, "-y", "-i", str(filepath), "-i", str(clean_sub_file),
                                "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text", str(tmp_sub_mp4),
                            ],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180,
                        )
                    if burn_res.returncode == 0 and tmp_sub_mp4.exists() and tmp_sub_mp4.stat().st_size > 0:
                        os.replace(tmp_sub_mp4, filepath)
                    else:
                        tmp_sub_mp4.unlink(missing_ok=True)
                if dub_code:
                    update_job(status="dubbing", percent=100.0, speed="", eta="")
                    dubbed = apply_ai_dubbing(
                        Path(filepath), sub_candidate, dub_code, ffmpeg_loc, gemini_api_key=gemini_api_key
                    )
                    if dubbed is not None:
                        Path(filepath).unlink(missing_ok=True)
                        filepath = str(dubbed)
                if clean_sub_file != sub_candidate:
                    clean_sub_file.unlink(missing_ok=True)
                if not sub_langs:
                    sub_candidate.unlink(missing_ok=True)

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
    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "tv", "web"]}},
    }
    cookie_file = _get_yt_cookies_opt()
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
    try:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as first_exc:
            embedded = discover_embedded_media_url(url)
            if not embedded or embedded == url:
                raise first_exc
            with yt_dlp.YoutubeDL({**ydl_opts, "http_headers": {"Referer": url}}) as ydl:
                info = ydl.extract_info(embedded, download=False)
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


def create_download_job(
    url: str, quality: str, subtitle_lang: str = "", dub_lang: str = "", gemini_api_key: str = ""
) -> str:
    clean_quality = quality if quality in ("best", "1080", "720", "480", "audio") else "best"
    clean_sub = subtitle_lang.strip().lower() if expand_subtitle_langs(subtitle_lang) else ""
    clean_dub = dub_lang.strip().lower() if dub_lang and dub_lang.strip().lower() in NEURAL_VOICES else ""
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
            "dub_lang": clean_dub,
            "status": "queued",
            "percent": 0.0,
            "speed": "",
            "eta": "",
            "title": "",
            "filename": "",
            "filepath": "",
            "error": "",
        }

    storage.save_job(job_id, dict(jobs[job_id]))
    thread = threading.Thread(
        target=run_download_job,
        args=(job_id, url, clean_quality, clean_sub, clean_dub, (gemini_api_key or "").strip()),
        daemon=True,
    )
    thread.start()
    return job_id


def get_job_status(job_id: str) -> Dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            return dict(job)
    persisted = storage.load_job(job_id)
    if persisted:
        return persisted
    raise HTTPException(status_code=404, detail="İndirme görevi bulunamadı.")
