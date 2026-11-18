import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from typing import Optional

_STREAM_RE = re.compile(
    r"""(?:["'])(https?://[^"'\s<>]+?\.(?:m3u8|mp4|mpd|webm)(?:\?[^"'\s<>]*)?)(?:["'])""",
    re.IGNORECASE,
)
_META_VIDEO_RE = re.compile(
    r"""<meta[^>]+(?:property|name)=["']og:video(?::url|:secure_url)?["'][^>]+content=["']([^"']+)["']""",
    re.IGNORECASE,
)
_SOURCE_TAG_RE = re.compile(
    r"""<(?:video|source)[^>]+src=["']([^"']+)["']""",
    re.IGNORECASE,
)
_IFRAME_RE = re.compile(
    r"""<iframe[^>]+src=["']([^"']+)["']""",
    re.IGNORECASE,
)


def is_public_http_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse((url or "").strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
            return False
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return False
        return True
    except Exception:
        return False


def discover_embedded_media_url(page_url: str) -> Optional[str]:
    """Fetch a webpage and extract direct stream (.m3u8/.mp4/.mpd), <video>, og:video, or <iframe> player URL."""
    if not is_public_http_url(page_url):
        return None
    try:
        req = urllib.request.Request(
            page_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "video/" in ctype or "mpegurl" in ctype:
                return page_url
            raw = resp.read(2 * 1024 * 1024).decode("utf-8", errors="ignore")
    except Exception:
        return None

    for pat in (_STREAM_RE, _META_VIDEO_RE, _SOURCE_TAG_RE, _IFRAME_RE):
        for match in pat.findall(raw):
            candidate = urllib.parse.urljoin(page_url, match.strip().replace("\\/", "/"))
            if is_public_http_url(candidate) and not candidate.lower().endswith(
                (".js", ".css", ".png", ".jpg", ".svg", ".ico")
            ):
                return candidate
    return None
