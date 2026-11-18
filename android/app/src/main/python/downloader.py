import ipaddress
import json
import os
import re
import socket
import urllib.request
from urllib.parse import urljoin, urlparse
import yt_dlp


_STREAM_RE = re.compile(r"""(?:["'])(https?://[^"'\s<>]+?\.(?:m3u8|mp4|mpd|webm)(?:\?[^"'\s<>]*)?)(?:["'])""", re.IGNORECASE)
_META_VIDEO_RE = re.compile(r"""<meta[^>]+(?:property|name)=["']og:video(?::url|:secure_url)?["'][^>]+content=["']([^"']+)["']""", re.IGNORECASE)
_SOURCE_TAG_RE = re.compile(r"""<(?:video|source)[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
_IFRAME_RE = re.compile(r"""<iframe[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


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
    return host[4:] if host.startswith("www.") else (host or "Web")


def _validate_url(url):
    cleaned = (url or "").strip()
    try:
        parsed = urlparse(cleaned)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return None
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
            return None
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                return None
        return cleaned
    except Exception:
        return None


def _discover_embedded_media_url(page_url):
    try:
        req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "video/" in ctype or "mpegurl" in ctype:
                return page_url
            raw = resp.read(1500000).decode("utf-8", errors="ignore")
    except Exception:
        return None
    for pat in (_STREAM_RE, _META_VIDEO_RE, _SOURCE_TAG_RE, _IFRAME_RE):
        for match in pat.findall(raw):
            candidate = urljoin(page_url, match.strip().replace("\\/", "/"))
            if _validate_url(candidate) and not candidate.lower().endswith((".js", ".css", ".png", ".jpg", ".svg")):
                return candidate
    return None


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
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(clean_url, download=False)
        except Exception as first_exc:
            embedded = _discover_embedded_media_url(clean_url)
            if not embedded or embedded == clean_url:
                raise first_exc
            with yt_dlp.YoutubeDL({**opts, "http_headers": {"Referer": clean_url}}) as ydl:
                info = ydl.extract_info(embedded, download=False)
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


def download_video(url, quality, output_dir, progress_cb=None, subtitle_lang="", dub_lang=""):
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
        dub_langs = _expand_sub_langs(dub_lang)
        if (sub_langs or dub_langs) and quality != "audio":
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = list(dict.fromkeys(sub_langs + dub_langs))
            opts["subtitlesformat"] = "srt/vtt/best"

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(clean_url, download=True)
                filepath = None
                req_dl = info.get("requested_downloads") or []
                if req_dl:
                    filepath = req_dl[0].get("filepath")
                if not filepath:
                    filepath = ydl.prepare_filename(info)
        except Exception as first_exc:
            embedded = _discover_embedded_media_url(clean_url)
            if not embedded or embedded == clean_url:
                raise first_exc
            with yt_dlp.YoutubeDL({**opts, "http_headers": {"Referer": clean_url}}) as ydl:
                info = ydl.extract_info(embedded, download=True)
                filepath = None
                req_dl = info.get("requested_downloads") or []
                if req_dl:
                    filepath = req_dl[0].get("filepath")
                if not filepath:
                    filepath = ydl.prepare_filename(info)

        subtitle_path = ""
        subtitle_name = ""
        if (sub_langs or dub_langs) and filepath:
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
