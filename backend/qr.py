import io
import os
import socket
import urllib.parse
from typing import Dict

import qrcode
import qrcode.image.svg

from backend.config import PHONE_TOKEN


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def build_phone_link_payload(request_port: int | None) -> Dict[str, str]:
    ip = get_local_ip()
    port = os.environ.get("VIDEO_INDIRICI_PORT") or str(request_port or 8765)
    url = f"http://{ip}:{port}/telefon?token={urllib.parse.quote(PHONE_TOKEN)}"

    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    qr_svg = buf.getvalue().decode("utf-8")

    return {"url": url, "ip": ip, "port": port, "qr_svg": qr_svg}
