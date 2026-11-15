import json
import os
import re
import yt_dlp


def _strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text or "").strip()


def _detect_platform(url):
    lower = url.lower()
    if any(d in lower for d in ("youtube.com", "youtu.be", "youtube-nocookie.com")):
        return "YouTube"
    if any(d in lower for d in ("instagram.com", "instagr.am")):
        return "Instagram"
    return "Diğer"


def search_youtube(query, limit=20):
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
            "ignoreerrors": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(f"ytsearch{int(limit)}:{query}", download=False)
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
            results.append({
                "id": vid_id,
                "title": item.get("title") or "Başlıksız Video",
                "url": vid_url,
                "uploader": item.get("uploader") or item.get("channel") or "YouTube",
                "duration": item.get("duration") or 0,
                "thumbnail": thumb or "",
            })
        return json.dumps({"ok": True, "results": results}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": _strip_ansi(str(exc))}, ensure_ascii=False)


def get_info(url):
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return json.dumps({"ok": False, "error": "Video bilgileri alınamadı."}, ensure_ascii=False)
        return json.dumps({
            "ok": True,
            "title": info.get("title") or "Başlıksız Video",
            "uploader": info.get("uploader") or info.get("channel") or "Bilinmiyor",
            "duration": info.get("duration") or 0,
            "thumbnail": info.get("thumbnail") or "",
            "platform": _detect_platform(url),
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": _strip_ansi(str(exc))}, ensure_ascii=False)


def download_video(url, quality, output_dir, progress_cb=None):
    try:
        os.makedirs(output_dir, exist_ok=True)

        def hook(d):
            if progress_cb is None:
                return
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                pct = round(min(100.0, (downloaded / total) * 100), 1) if total > 0 else 0.0
                progress_cb.onProgress(float(pct), f"İndiriliyor... %{pct}")
            elif status == "finished":
                progress_cb.onProgress(100.0, "Dosya hazırlanıyor...")

        if quality == "audio":
            fmt = "bestaudio[ext=m4a]/bestaudio/best"
        elif quality in ("720", "480"):
            h = int(quality)
            fmt = f"best[height<={h}][ext=mp4]/best[ext=mp4]/best"
        else:
            fmt = "best[ext=mp4]/best"

        opts = {
            "outtmpl": os.path.join(output_dir, "%(title).100s [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": False,
            "format": fmt,
            "progress_hooks": [hook],
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = None
            req_dl = info.get("requested_downloads") or []
            if req_dl:
                filepath = req_dl[0].get("filepath")
            if not filepath:
                filepath = ydl.prepare_filename(info)

        return json.dumps({
            "ok": True,
            "filepath": filepath,
            "filename": os.path.basename(filepath),
            "title": info.get("title") or "Video",
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": _strip_ansi(str(exc))}, ensure_ascii=False)
