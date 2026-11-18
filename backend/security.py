import re
import secrets
import threading
import time
import urllib.parse
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from backend.config import PHONE_TOKEN
from backend import storage

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


def _get_client_ip(request: Request) -> str:
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
    ts_str = time.strftime("%H:%M:%S")
    masked = _mask_ip(ip)
    ban_until = 0.0
    with WAF_LOCK:
        WAF_BLOCKED_TOTAL += 1
        WAF_EVENTS.append({
            "time": ts_str,
            "ip": masked,
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
                ban_until = now + WAF_BAN_SECONDS
                WAF_BANS[ip] = ban_until
    storage.record_waf_event(ts_str, masked, reason, path)
    if ban_until > 0.0:
        storage.save_ban(ip, ban_until)


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
    if request_host and parsed_orig.netloc == request_host:
        return True
    return False


async def waf_security_middleware(request: Request, call_next: Any) -> Any:
    origin = request.headers.get("origin")
    req_host = request.headers.get("host")
    client_ip = _get_client_ip(request)
    path = request.url.path
    now = time.time()

    with WAF_LOCK:
        ban_until = WAF_BANS.get(client_ip, 0.0)
        if ban_until == 0.0:
            ban_until = storage.get_ban_until(client_ip)
            if ban_until > now:
                WAF_BANS[client_ip] = ban_until
        if ban_until > now:
            return JSONResponse(
                {"detail": "IP adresiniz güvenlik duvarı tarafından geçici olarak engellendi."},
                status_code=403,
            )
        if client_ip in WAF_BANS:
            WAF_BANS.pop(client_ip, None)

    ua = (request.headers.get("user-agent") or "").lower()
    if any(bot in ua for bot in WAF_SCANNER_AGENTS):
        _record_waf_event(client_ip, "Otomatik tarayıcı/bot engellendi", path, strike=True)
        return JSONResponse({"detail": "Erişim güvenlik duvarı tarafından reddedildi."}, status_code=403)

    if path != "/health":
        with WAF_LOCK:
            reqs = [t for t in WAF_RATE.get(client_ip, []) if now - t < 60]
            reqs.append(now)
            WAF_RATE[client_ip] = reqs
            if len(WAF_RATE) > 500:
                stale = [k for k, v in WAF_RATE.items() if not v or now - v[-1] >= 60]
                for k in stale:
                    WAF_RATE.pop(k, None)
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

    raw_target = path + ("?" + request.url.query if request.url.query else "")
    sig_hit = _waf_check_text(raw_target)
    if sig_hit:
        _record_waf_event(client_ip, sig_hit, path, strike=True)
        return JSONResponse({"detail": "Zararlı istek güvenlik duvarı tarafından engellendi."}, status_code=403)

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
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-WAF-Protected"] = "VideoIndirici-Firewall/1.0"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def get_waf_status() -> Dict[str, Any]:
    summary = storage.get_waf_summary(15)
    return {
        "firewall": "active",
        "persistent_storage": "sqlite",
        "blocked_total": summary["total"],
        "active_bans": storage.count_active_bans(),
        "recent_events": summary["events"],
    }


def is_local_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def require_local(request: Request) -> None:
    if not is_local_client(request):
        raise HTTPException(status_code=403, detail="Bu işlem yalnızca bilgisayar üzerinden yapılabilir.")


def _check_rate_limit(client_ip: str) -> None:
    now = time.time()
    with FAILED_LOCK:
        attempts = [t for t in FAILED_TOKEN_ATTEMPTS.get(client_ip, []) if now - t < 300]
        if attempts:
            FAILED_TOKEN_ATTEMPTS[client_ip] = attempts
        else:
            FAILED_TOKEN_ATTEMPTS.pop(client_ip, None)
        if len(attempts) >= 10:
            raise HTTPException(status_code=429, detail="Çok fazla hatalı deneme. 5 dakika sonra tekrar deneyin.")


def _record_failed_attempt(client_ip: str) -> None:
    with FAILED_LOCK:
        FAILED_TOKEN_ATTEMPTS.setdefault(client_ip, []).append(time.time())


def require_phone_or_local(request: Request, token: Optional[str] = None) -> None:
    if is_local_client(request):
        return
    client_ip = _get_client_ip(request)
    _check_rate_limit(client_ip)

    if token and secrets.compare_digest(token, PHONE_TOKEN):
        return

    _record_failed_attempt(client_ip)
    raise HTTPException(status_code=403, detail="Yetkisiz erişim. Lütfen QR kodu tekrar okutun.")


def require_action_access(request: Request, token: Optional[str] = None) -> None:
    require_local(request)


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def detect_platform(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if any(_host_matches(host, d) for d in ("youtube.com", "youtu.be", "youtube-nocookie.com")):
        return "YouTube"
    if any(_host_matches(host, d) for d in ("instagram.com", "instagr.am")):
        return "Instagram"
    return host.removeprefix("www.") if host else "Web"


def validate_url(url: str) -> str:
    from backend.universal import is_public_http_url

    cleaned = url.strip()
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Geçerli bir http:// veya https:// bağlantısı girin.")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Bağlantıda kullanıcı bilgisi bulunamaz.")
    if not is_public_http_url(cleaned):
        raise HTTPException(
            status_code=400,
            detail="Güvenlik duvarı: Yerel ağ veya özel IP adreslerine erişim engellendi.",
        )
    return cleaned
