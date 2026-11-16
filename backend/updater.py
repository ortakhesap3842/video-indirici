import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException

from version import APP_VERSION, GITHUB_REPO, UPDATE_MANIFEST_URL


def parse_version(v: str) -> tuple:
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts) if parts else (0,)


def fetch_update_status() -> Dict[str, Any]:
    if "OWNER/" in UPDATE_MANIFEST_URL:
        return {
            "current_version": APP_VERSION,
            "update_available": False,
            "configured": False,
            "message": "Güncelleme sunucusu henüz yapılandırılmadı (version.py içinde GITHUB_REPO ayarlayın).",
        }
    try:
        req = urllib.request.Request(
            UPDATE_MANIFEST_URL,
            headers={"User-Agent": f"VideoIndirici/{APP_VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = str(data.get("version", APP_VERSION)).lstrip("v")
        update_available = parse_version(latest) > parse_version(APP_VERSION)
        return {
            "current_version": APP_VERSION,
            "latest_version": latest,
            "update_available": update_available,
            "configured": True,
            "download_url": data.get("download_url", ""),
            "notes": data.get("notes", ""),
        }
    except Exception as exc:
        return {
            "current_version": APP_VERSION,
            "update_available": False,
            "configured": True,
            "error": f"Güncelleme kontrolü başarısız: {exc}",
        }


def apply_windows_update(download_url: str) -> Dict[str, Any]:
    if not getattr(sys, "frozen", False):
        raise HTTPException(
            status_code=400,
            detail="Otomatik güncelleme yalnızca paketlenmiş .exe uygulamasında çalışır.",
        )
    allowed_prefix = f"https://github.com/{GITHUB_REPO}/releases/"
    if not download_url.startswith(allowed_prefix):
        raise HTTPException(status_code=400, detail="Yalnızca resmi GitHub Releases üzerinden güncelleme yapılabilir.")

    current_exe = Path(sys.executable).resolve()
    temp_dir = Path(tempfile.gettempdir())
    new_exe = temp_dir / f"VideoIndirici_new_{uuid.uuid4().hex[:6]}.exe"
    updater_bat = temp_dir / f"update_video_indirici_{uuid.uuid4().hex[:6]}.bat"

    try:
        url_req = urllib.request.Request(
            download_url,
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
            "timeout /t 2 /nobreak >nul\n"
            ":waitloop\n"
            f"tasklist /FI \"PID eq {pid}\" 2>NUL | find /I \"{pid}\" >NUL\n"
            "if not errorlevel 1 (\n"
            "  timeout /t 1 /nobreak >nul\n"
            "  goto waitloop\n"
            ")\n"
            "timeout /t 1 /nobreak >nul\n"
            f"move /Y \"{new_exe}\" \"{current_exe}\"\n"
            f"start \"\" \"{current_exe}\"\n"
            "del \"%~f0\"\n"
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
            time.sleep(0.6)
            os._exit(0)

        threading.Thread(target=exit_soon, daemon=True).start()
        return {"ok": True, "message": "Güncelleme indirildi, uygulama yeniden başlatılıyor..."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Güncelleme uygulanamadı: {exc}")
