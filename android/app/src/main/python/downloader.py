import json
import os
import re
from urllib.parse import urlparse
import yt_dlp


def _strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text or "").strip()


def _host_matches(host, domain):
    return host == domain or host.endswith("." + domain)


def _detect_platform(url):
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if any(_host_matches(host, d) for d in ("youtube.com", "youtu.be", "youtube-nocookie.com")):
        return "YouTube"
    if any(_host_matches(host, d) for d in ("instagram.com", "instagr.am")):
        return "Instagram"
    return "Diğer"


def _validate_url(url):
    cleaned = (url or "").strip()
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return None
    if _detect_platform(cleaned) == "Diğer":
        return None
    return cleaned


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
        clean_url = _validate_url(url)
        if not clean_url:
            return json.dumps({"ok": False, "error": "Yalnızca geçerli YouTube ve Instagram bağlantıları desteklenir."}, ensure_ascii=False)
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
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


def _expand_sub_langs(lang_code):
    code = (lang_code or "").strip().lower()
    if not code or not re.match(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$", code):
        return []
    if code == "zh":
        return ["zh-Hans", "zh-Hant", "zh"]
    if code == "en":
        return ["en", "en-US", "en-GB"]
    return [code]


def download_video(url, quality, output_dir, progress_cb=None, subtitle_lang=""):
    try:
        clean_url = _validate_url(url)
        if not clean_url:
            return json.dumps({"ok": False, "error": "Yalnızca geçerli YouTube ve Instagram bağlantıları desteklenir."}, ensure_ascii=False)
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
            fmt = f"best[height<={h}][ext=mp4][acodec!=none]/best[ext=mp4][acodec!=none]/best[acodec!=none]/best"
        else:
            fmt = "best[ext=mp4][acodec!=none]/best[acodec!=none]/best"

        opts = {
            "outtmpl": os.path.join(output_dir, "%(title).100s [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": False,
            "format": fmt,
            "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
            "progress_hooks": [hook],
        }
        sub_langs = _expand_sub_langs(subtitle_lang)
        if sub_langs and quality != "audio":
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = sub_langs
            opts["subtitlesformat"] = "srt/vtt/best"

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(clean_url, download=True)
            filepath = None
            req_dl = info.get("requested_downloads") or []
            if req_dl:
                filepath = req_dl[0].get("filepath")
            if not filepath:
                filepath = ydl.prepare_filename(info)

        subtitle_path = ""
        subtitle_name = ""
        if sub_langs and filepath:
            stem = os.path.splitext(filepath)[0]
            for fn in os.listdir(output_dir):
                full = os.path.join(output_dir, fn)
                if full.startswith(stem) and os.path.splitext(fn)[1].lower() in (".srt", ".vtt"):
                    subtitle_path = full
                    subtitle_name = fn
                    break

        return json.dumps({
            "ok": True,
            "filepath": filepath,
            "filename": os.path.basename(filepath),
            "subtitle_path": subtitle_path,
            "subtitle_name": subtitle_name,
            "title": info.get("title") or "Video",
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": _strip_ansi(str(exc))}, ensure_ascii=False)
