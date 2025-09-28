#!/usr/bin/env python3
"""
trailer_dl.py — minimal wrapper that runs your yt-dlp command verbatim.

Usage:
    python trailer_dl.py <VIDEO_URL> <OUT_DIR>

Behavior:
- Prefers system 'yt-dlp' binary (PATH). Falls back to python -m yt_dlp only if binary not found.
- Runs your exact flags first:
    yt-dlp -f "bv*+ba/best" --merge-output-format mp4 --embed-metadata --embed-thumbnail \
           -o "%(dirname)s/%(dirname)s - trailer.%(ext)s" <VIDEO_URL>
- If SABR/nsig triggers and TRAILER_STRICT is NOT set, retries once with:
    --extractor-args youtube:player_client=android
- Exit code mirrors yt-dlp's result.
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

SABR_SIGNS = (
    "sabr streaming",
    "nsig extraction failed",
    "only images are available",
    "requested format is not available",
)

def _run(cmd, cwd):
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

def main():
    if len(sys.argv) < 3:
        print("Usage: trailer_dl.py <VIDEO_URL> <OUT_DIR>", file=sys.stderr)
        sys.exit(2)

    url = sys.argv[1]
    out_dir = Path(sys.argv[2]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) Prefer system yt-dlp executable
    ytdlp_bin = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if ytdlp_bin:
        base_cmd = [ytdlp_bin]
    else:
        # 2) Fallback to python -m yt_dlp (uses whatever version this Python can import)
        base_cmd = [sys.executable, "-m", "yt_dlp"]

    # Your command EXACTLY
    exact = [
        "-f", "bv*+ba/best",
        "--merge-output-format", "mp4",
        "--embed-metadata",
        "--embed-thumbnail",
        "-o", "%(dirname)s/%(dirname)s - trailer.%(ext)s",
        url,
    ]

    # Try exact first
    res = _run(base_cmd + exact, cwd=out_dir)
    if res.returncode == 0:
        print(res.stdout, end="")
        sys.exit(0)

    out_low = (res.stdout or "").lower()
    strict = os.getenv("TRAILER_STRICT", "").strip() not in ("", "0", "false", "False")

    # If not a SABR/nsig case OR user requested strict mode, surface the failure as-is
    if strict or not any(sig in out_low for sig in SABR_SIGNS):
        print(res.stdout, end="")
        sys.exit(res.returncode)

    # Minimal fallback: same command + android player client (keeps your format/quality intent)
    android = base_cmd + [
        "--extractor-args", "youtube:player_client=android",
        *exact
    ]
    res2 = _run(android, cwd=out_dir)
    print((res.stdout or "") + (res2.stdout or ""), end="")
    sys.exit(res2.returncode)

if __name__ == "__main__":
    main()
