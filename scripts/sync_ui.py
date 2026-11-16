import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "ui"
TARGETS = [
    ROOT / "static",
    ROOT / "android" / "app" / "src" / "main" / "assets",
    ROOT / "docs",
]
FILES = ("shared_ui.css", "shared_ui.js")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync shared UI assets across Desktop, Android, and iOS.")
    parser.add_argument("--check", action="store_true", help="Verify targets match ui/ without modifying files.")
    args = parser.parse_args()

    out_of_sync = []
    for fname in FILES:
        src = SRC_DIR / fname
        src_bytes = src.read_bytes()
        for target_dir in TARGETS:
            dst = target_dir / fname
            if args.check:
                if not dst.exists() or dst.read_bytes() != src_bytes:
                    out_of_sync.append(str(dst.relative_to(ROOT)))
            else:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    if args.check and out_of_sync:
        print("OUT_OF_SYNC:", ", ".join(out_of_sync))
        return 1
    print("UI_SYNC_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
