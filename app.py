import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
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

from version import APP_VERSION, GITHUB_REPO, UPDATE_MANIFEST_URL

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent

STATIC_DIR = BASE_DIR / "static"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "VideoIndirici"
DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Video İndirici (YouTube & Instagram)")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

PHONE_TOKEN = secrets.token_urlsafe(24)
CLOUD_ACCESS_KEY = os.environ.get("CLOUD_ACCESS_KEY", "").strip()
CLOUD_MODE = bool(CLOUD_ACCESS_KEY)
CLOUD_ALLOWED_ORIGINS = {
    "https://ortakhesap3842.github.io",
    "http://localhost",
    "http://127.0.0.1",
}
FAILED_TOKEN_ATTEMPTS: Dict[str, list] = {}
FAILED_LOCK = threading.Lock()
WAF_LOCK = threading.Lock()
WAF_RATE: Dict[str, list] = {}
WAF_HEAVY_RATE: Dict[str, list] = {}
WAF_STRIKES: Dict[str, list] = {}
WAF_BANS: Dict[str, float] = {}
WAF_EVENTS: list = []
WAF_BLOCKED_TOTAL = 0
WAF_MAX_BODY_BYTES = 65536
WAF_BAN_SECONDS = 900

WAF_SCANNER_AGENTS = (
    "sqlmap", "nikto", "nmap", "masscan", "zgrab", "gobuster",
    "dirbuster", "acunetix", "nessus", "nuclei", "wpscan", "wfuzz",
)

WAF_SIGNATURES = (
    re.compile(r"(?:\.\./|\.\.\\|%2e%2e|%252e%252e)", re.IGNORECASE),
    re.compile(r"(?:/etc/passwd|/etc/shadow|win\.ini|boot\.ini|/\.git/|/\.env\b)", re.IGNORECASE),
    re.compile(r"(?:<\s*script\b|javascript:|onerror\s*=|onload\s*=)", re.IGNORECASE),
    re.compile(r"(?:\bunion\b\s+\bselect\b|\bdrop\b\s+\btable\b|\bor\b\s+1\s*=\s*1\b)", re.IGNORECASE),
    re.compile(r"(?:\$\{jndi:|cmd\.exe|powershell\s+-|;\s*rm\s+-rf\b|`[^`]{1,80}`)", re.IGNORECASE),
)

jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()

ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "instagram.com",
    "instagr.am",
)


def _get_client_ip(request: Request) -> str:
    if CLOUD_MODE:
        xff = request.headers.get("x-forwarded-for", "").strip()
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _mask_ip(ip: str) -> str:
    if ":" in ip:
        return ip[:8] + "***"
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.*.*"
    return ip


def _record_waf_event(ip: str, reason: str, path: str, strike: bool = True) -> None:
    global WAF_BLOCKED_TOTAL
    now = time.time()
    with WAF_LOCK:
        WAF_BLOCKED_TOTAL += 1
        WAF_EVENTS.append({
            "time": time.strftime("%H:%M:%S"),
            "ip": _mask_ip(ip),
            "reason": reason,
            "path": path[:80],
        })
        if len(WAF_EVENTS) > 50:
            del WAF_EVENTS[:-50]
        if strike and ip not in ("127.0.0.1", "::1", "localhost"):
            strikes = [t for t in WAF_STRIKES.get(ip, []) if now - t < 600]
            strikes.append(now)
            WAF_STRIKES[ip] = strikes
            if len(strikes) >= 5:
                WAF_BANS[ip] = now + WAF_BAN_SECONDS


def _waf_check_text(text: str) -> Optional[str]:
    if not text:
        return None
    if "\x00" in text:
        return "Null-byte saldırı imzası"
    decoded = urllib.parse.unquote_plus(text)
    for target in (text, decoded):
        for pat in WAF_SIGNATURES:
            if pat.search(target):
                return "Zararlı yük / enjeksiyon imzası"
    return None


def _is_allowed_origin(origin: Optional[str], request_host: Optional[str] = None) -> bool:
    if not origin or origin == "null":
        return True
    parsed_orig = urllib.parse.urlparse(origin)
    if parsed_orig.hostname in ("127.0.0.1", "localhost"):
        return True
    if CLOUD_MODE:
        base_orig = f"{parsed_orig.scheme}://{parsed_orig.netloc}"
        if base_orig in CLOUD_ALLOWED_ORIGINS:
            return True
        if request_host and parsed_orig.netloc == request_host:
            return True
    return False


@app.middleware("http")
async def security_middleware(request: Request, call_next: Any) -> Any:
    origin = request.headers.get("origin")
    req_host = request.headers.get("host")
    client_ip = _get_client_ip(request)
    path = request.url.path
    now = time.time()

    # 1. Active IP ban check
    with WAF_LOCK:
        ban_until = WAF_BANS.get(client_ip, 0.0)
        if ban_until > now:
            return JSONResponse(
                {"detail": "IP adresiniz güvenlik duvarı tarafından geçici olarak engellendi."},
                status_code=403,
            )
        elif client_ip in WAF_BANS:
            WAF_BANS.pop(client_ip, None)

    # 2. Scanner / exploit bot User-Agent block
    ua = (request.headers.get("user-agent") or "").lower()
    if any(bot in ua for bot in WAF_SCANNER_AGENTS):
        _record_waf_event(client_ip, "Otomatik tarayıcı/bot engellendi", path, strike=True)
        return JSONResponse({"detail": "Erişim güvenlik duvarı tarafından reddedildi."}, status_code=403)

    # 3. Rate limiting (General: 180/min, Heavy API: 25/min)
    if path != "/health":
        with WAF_LOCK:
            reqs = [t for t in WAF_RATE.get(client_ip, []) if now - t < 60]
            reqs.append(now)
            WAF_RATE[client_ip] = reqs
            if len(reqs) > 180:
                _record_waf_event(client_ip, "Genel hız sınırı aşıldı (DDoS/Flood)", path, strike=True)
                return JSONResponse({"detail": "Çok fazla istek. Güvenlik duvarı hız sınırı."}, status_code=429)

            if path in ("/api/download", "/api/search", "/api/info") and request.method == "POST":
                hreqs = [t for t in WAF_HEAVY_RATE.get(client_ip, []) if now - t < 60]
                hreqs.append(now)
                WAF_HEAVY_RATE[client_ip] = hreqs
                if len(hreqs) > 25:
                    _record_waf_event(client_ip, "API işlem hız sınırı aşıldı", path, strike=False)
                    return JSONResponse({"detail": "Dakikalık işlem sınırı aşıldı."}, status_code=429)

    # 4. Path & query WAF signature inspection
    raw_target = path + ("?" + request.url.query if request.url.query else "")
    sig_hit = _waf_check_text(raw_target)
    if sig_hit:
        _record_waf_event(client_ip, sig_hit, path, strike=True)
        return JSONResponse({"detail": "Zararlı istek güvenlik duvarı tarafından engellendi."}, status_code=403)

    if CLOUD_MODE and request.method == "OPTIONS":
        if not _is_allowed_origin(origin, req_host):
            _record_waf_event(client_ip, "Yetkisiz CORS Origin", path, strike=False)
            return JSONResponse({"detail": "Geçersiz Origin."}, status_code=403)
        preflight = JSONResponse({}, status_code=204)
        if origin:
            preflight.headers["Access-Control-Allow-Origin"] = origin
            preflight.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            preflight.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Access-Key"
            preflight.headers["Access-Control-Max-Age"] = "600"
        return preflight

    # 5. CSRF / cross-origin mutation check & payload inspection
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        if not _is_allowed_origin(origin, req_host):
            _record_waf_event(client_ip, "CSRF / Harici Origin engellendi", path, strike=True)
            return JSONResponse({"detail": "Geçersiz Origin."}, status_code=403)
        body_bytes = await request.body()
        if len(body_bytes) > WAF_MAX_BODY_BYTES:
            _record_waf_event(client_ip, "Aşırı büyük istek gövdesi (413)", path, strike=True)
            return JSONResponse({"detail": "İstek boyutu güvenlik duvarı sınırını aşıyor."}, status_code=413)
        body_sig = _waf_check_text(body_bytes.decode("utf-8", errors="ignore"))
        if body_sig:
            _record_waf_event(client_ip, body_sig, path, strike=True)
            return JSONResponse({"detail": "Zararlı veri güvenlik duvarı tarafından engellendi."}, status_code=403)

    response = await call_next(request)
    if CLOUD_MODE and origin and _is_allowed_origin(origin, req_host):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Access-Key"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-WAF-Protected"] = "VideoIndirici-Firewall/1.0"
    if CLOUD_MODE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def is_local_client(request: Request) -> bool:
    if CLOUD_MODE:
        return False
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def require_local(request: Request) -> None:
    if not is_local_client(request):
        raise HTTPException(status_code=403, detail="Bu işlem yalnızca bilgisayar üzerinden yapılabilir.")


def _check_rate_limit(client_ip: str) -> None:
    now = time.time()
    with FAILED_LOCK:
        attempts = [t for t in FAILED_TOKEN_ATTEMPTS.get(client_ip, []) if now - t < 300]
        FAILED_TOKEN_ATTEMPTS[client_ip] = attempts
        if len(attempts) >= 10:
            raise HTTPException(status_code=429, detail="Çok fazla hatalı deneme. 5 dakika sonra tekrar deneyin.")


def _record_failed_attempt(client_ip: str) -> None:
    with FAILED_LOCK:
        FAILED_TOKEN_ATTEMPTS.setdefault(client_ip, []).append(time.time())


def require_phone_or_local(request: Request, token: Optional[str] = None) -> None:
    if is_local_client(request):
        return
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    header_key = request.headers.get("x-access-key")
    candidate = token or header_key
    if CLOUD_MODE and candidate and secrets.compare_digest(candidate, CLOUD_ACCESS_KEY):
        return
    if not CLOUD_MODE and candidate and secrets.compare_digest(candidate, PHONE_TOKEN):
        return

    _record_failed_attempt(client_ip)
    raise HTTPException(status_code=403, detail="Yetkisiz erişim. Geçerli erişim anahtarı gerekli.")


def require_action_access(request: Request, token: Optional[str] = None) -> None:
    if CLOUD_MODE:
        require_phone_or_local(request, token)
    else:
        require_local(request)


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


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def detect_platform(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if any(_host_matches(host, d) for d in ("youtube.com", "youtu.be", "youtube-nocookie.com")):
        return "YouTube"
    if any(_host_matches(host, d) for d in ("instagram.com", "instagr.am")):
        return "Instagram"
    return "Diğer"


def validate_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Geçerli bir http:// veya https:// bağlantısı girin.")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Bağlantıda kullanıcı bilgisi bulunamaz.")
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


def _cleanup_old_cloud_files() -> None:
    if not CLOUD_MODE or not DEFAULT_DOWNLOAD_DIR.exists():
        return
    cutoff = time.time() - 7200
    for entry in DEFAULT_DOWNLOAD_DIR.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except Exception:
            pass


def run_download_job(job_id: str, url: str, quality: str) -> None:
    _cleanup_old_cloud_files()

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
def read_phone_page(request: Request, token: Optional[str] = None) -> FileResponse:
    require_phone_or_local(request, token)
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
    require_local(request)
    ip = get_local_ip()
    port = os.environ.get("VIDEO_INDIRICI_PORT") or str(request.url.port or 8765)
    url = f"http://{ip}:{port}/telefon?token={urllib.parse.quote(PHONE_TOKEN)}"

    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    qr_svg = buf.getvalue().decode("utf-8")

    return JSONResponse({"url": url, "ip": ip, "port": port, "qr_svg": qr_svg})


@app.get("/api/file/{filename}")
def serve_downloaded_file(filename: str, request: Request, download: int = 0, token: Optional[str] = None) -> FileResponse:
    require_phone_or_local(request, token)
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
def open_downloaded_file(filename: str, request: Request) -> JSONResponse:
    require_local(request)
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
def delete_downloaded_file(filename: str, request: Request) -> JSONResponse:
    require_local(request)
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
def apply_update(req: ApplyUpdateRequest, request: Request) -> JSONResponse:
    require_local(request)
    if not getattr(sys, "frozen", False):
        raise HTTPException(
            status_code=400,
            detail="Otomatik güncelleme yalnızca paketlenmiş .exe uygulamasında çalışır.",
        )
    allowed_prefix = f"https://github.com/{GITHUB_REPO}/releases/"
    if not req.download_url.startswith(allowed_prefix):
        raise HTTPException(status_code=400, detail="Yalnızca resmi GitHub Releases üzerinden güncelleme yapılabilir.")

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

        with open(new_exe, "rb") as chk:
            header = chk.read(2)
        if header != b"MZ" or new_exe.stat().st_size < 10_000_000:
            new_exe.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="İndirilen güncelleme dosyası geçerli bir Windows uygulaması değil.")

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
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Güncelleme uygulanamadı: {exc}")


@app.get("/health")
def health_check() -> JSONResponse:
    return JSONResponse({"ok": True, "version": APP_VERSION, "cloud_mode": CLOUD_MODE})


@app.get("/api/ping")
def api_ping(request: Request, token: Optional[str] = None) -> JSONResponse:
    require_action_access(request, token)
    return JSONResponse({"ok": True, "version": APP_VERSION, "cloud_mode": CLOUD_MODE})


@app.get("/api/security-status")
def security_status(request: Request, token: Optional[str] = None) -> JSONResponse:
    require_action_access(request, token)
    now = time.time()
    with WAF_LOCK:
        active_bans = sum(1 for until in WAF_BANS.values() if until > now)
        recent = list(WAF_EVENTS[-15:])
        blocked_total = WAF_BLOCKED_TOTAL
    return JSONResponse({
        "firewall": "active",
        "blocked_total": blocked_total,
        "active_bans": active_bans,
        "recent_events": recent,
    })


@app.post("/api/search")
def search_youtube(req: SearchRequest, request: Request) -> JSONResponse:
    require_action_access(request)
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
def get_video_info(req: InfoRequest, request: Request) -> JSONResponse:
    require_action_access(request)
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
def start_download(req: DownloadRequest, request: Request) -> JSONResponse:
    require_action_access(request)
    url = validate_url(req.url)
    quality = req.quality if req.quality in ("best", "1080", "720", "480", "audio") else "best"
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
def get_status(job_id: str, request: Request, token: Optional[str] = None) -> JSONResponse:
    if CLOUD_MODE:
        require_phone_or_local(request, token)
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="İndirme görevi bulunamadı.")
        return JSONResponse(dict(job))


@app.get("/api/downloads")
def list_downloads(request: Request, token: Optional[str] = None) -> JSONResponse:
    require_phone_or_local(request, token)
    files = []
    if DEFAULT_DOWNLOAD_DIR.exists():
        for entry in sorted(DEFAULT_DOWNLOAD_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if entry.is_file() and not entry.name.endswith(".part"):
                size_mb = round(entry.stat().st_size / (1024 * 1024), 2)
                files.append({"name": entry.name, "size_mb": size_mb})
    return JSONResponse({"folder": str(DEFAULT_DOWNLOAD_DIR), "files": files[:15]})


@app.post("/api/open-folder")
def open_folder(request: Request) -> JSONResponse:
    require_local(request)
    DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(DEFAULT_DOWNLOAD_DIR))
    else:
        subprocess.Popen(["xdg-open", str(DEFAULT_DOWNLOAD_DIR)])
    return JSONResponse({"ok": True, "folder": str(DEFAULT_DOWNLOAD_DIR)})
