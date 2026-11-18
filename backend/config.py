import secrets
import sys
from pathlib import Path

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

STATIC_DIR = BASE_DIR / "static"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "VideoIndirici"
DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

PHONE_TOKEN = secrets.token_urlsafe(24)

ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "instagram.com",
    "instagr.am",
)
