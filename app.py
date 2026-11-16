import os
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.config import CLOUD_MODE, DEFAULT_DOWNLOAD_DIR, STATIC_DIR
from backend.downloader import (
    create_download_job,
    extract_video_info,
    get_job_status,
    search_youtube_items,
)
from backend.qr import build_phone_link_payload
from backend.security import (
    get_waf_status,
    require_action_access,
    require_local,
    require_phone_or_local,
    validate_url,
    waf_security_middleware,
)
from backend.updater import apply_windows_update, fetch_update_status
from version import APP_VERSION

app = FastAPI(title="Video İndirici (YouTube & Instagram)")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.middleware("http")(waf_security_middleware)


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


@app.get("/")
def read_root() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/telefon")
def read_phone_page(request: Request, token: Optional[str] = None) -> FileResponse:
    require_phone_or_local(request, token)
    return FileResponse(str(STATIC_DIR / "phone.html"))


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
    return JSONResponse(get_waf_status())


@app.get("/api/phone-link")
def get_phone_link(request: Request) -> JSONResponse:
    require_local(request)
    return JSONResponse(build_phone_link_payload(request.url.port))


@app.get("/api/file/{filename}")
def serve_downloaded_file(
    filename: str, request: Request, download: int = 0, token: Optional[str] = None
) -> FileResponse:
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
    return JSONResponse({"version": APP_VERSION, "frozen": getattr(sys, "frozen", False)})


@app.get("/api/check-update")
def check_update() -> JSONResponse:
    return JSONResponse(fetch_update_status())


@app.post("/api/apply-update")
def apply_update(req: ApplyUpdateRequest, request: Request) -> JSONResponse:
    require_local(request)
    return JSONResponse(apply_windows_update(req.download_url))


@app.post("/api/search")
def search_youtube(req: SearchRequest, request: Request) -> JSONResponse:
    require_action_access(request)
    return JSONResponse({"results": search_youtube_items(req.query, req.limit)})


@app.post("/api/info")
def get_video_info(req: InfoRequest, request: Request) -> JSONResponse:
    require_action_access(request)
    url = validate_url(req.url)
    return JSONResponse(extract_video_info(url))


@app.post("/api/download")
def start_download(req: DownloadRequest, request: Request) -> JSONResponse:
    require_action_access(request)
    url = validate_url(req.url)
    job_id = create_download_job(url, req.quality)
    return JSONResponse({"job_id": job_id})


@app.get("/api/status/{job_id}")
def get_status(job_id: str, request: Request, token: Optional[str] = None) -> JSONResponse:
    if CLOUD_MODE:
        require_phone_or_local(request, token)
    return JSONResponse(get_job_status(job_id))


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
