import os
import socket
import sys
import threading
import time
import urllib.request

if sys.platform == "win32":
    import _overlapped  # noqa: F401

import uvicorn
import webview

from app import app
from version import APP_NAME, APP_VERSION


def ensure_stdio() -> None:
    """Prevent crashes in PyInstaller --windowed mode where stdout/stderr are None."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_server(port: int) -> None:
    os.environ["VIDEO_INDIRICI_PORT"] = str(port)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_config=None,
        access_log=False,
    )


def wait_for_server(port: int, timeout: float = 12.0) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/api/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.15)


if __name__ == "__main__":
    ensure_stdio()
    port = find_free_port()
    server_thread = threading.Thread(target=start_server, args=(port,), daemon=True)
    server_thread.start()
    wait_for_server(port)

    webview.create_window(
        f"{APP_NAME} v{APP_VERSION}",
        f"http://127.0.0.1:{port}",
        width=900,
        height=800,
        min_size=(680, 620),
    )
    webview.start()
