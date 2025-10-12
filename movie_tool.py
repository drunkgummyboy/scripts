#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
movie_tools.py — Right-click friendly movie/series tools (Trailer+Info UI)
- GUI with two tabs:
    1) Options: quick run, mode, layout, formats, posters, clean/prune, dry-run, trailer toggle.
       • Info panel: Title + Year + Synopsis + Poster preview + Trailer link + "Download trailer now"
       • "Change target..." to reselect file/folder and refresh info
       • "Run" button to apply actions
    2) CLI Builder: every flag with explanation; live command + Copy
- CLI subcommands:
    • rename  → movies
    • series  → tv episodes

Extra:
  - TMDB rate-limit friendliness (requests Retry/backoff)
  - Optional season posters (Series)
  - .jsonl log of actions next to the script (movie_tools.log.jsonl)
  - Auto-install Pillow for poster preview; auto-install yt-dlp for trailer download (fallback)
  - Best-trailer picking from TMDB /videos

Requirements:
  pip install requests
  (optional) platformdirs
"""

import os
import re
import sys
import json
import shutil
import argparse
import logging
import time
import subprocess
import webbrowser
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

import requests

# ------------ small helpers: ensure modules & jsonl log ------------
def ensure_pillow_installed() -> bool:
    try:
        import PIL  # noqa
        return True
    except Exception:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "Pillow"], check=False)
            import PIL  # noqa
            return True
        except Exception:
            return False

def ensure_ytdlp_installed() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except Exception:
        pass
    try:
        import subprocess, sys
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "yt-dlp"], check=False)
        # Verify import
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        return False

def jsonl_log_path() -> Path:
    return Path(__file__).with_name("movie_tools.log.jsonl")

def log_jsonl(event: str, payload: Dict[str, Any]):
    try:
        rec = {"ts": int(time.time()), "event": event, **payload}
        with open(jsonl_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

# ---------------- GUI only (picker) ----------------
def api_key_popup(prefill: str = "") -> Optional[str]:
    try:
        import tkinter as tk
        from tkinter import simpledialog
    except Exception:
        return None
    root = tk.Tk(); root.withdraw()
    try:
        key = simpledialog.askstring("TMDB API Key", "Enter your TMDB API key", initialvalue=prefill, parent=root)
        return key.strip() if key and key.strip() else None
    except Exception:
        return None

def pick_file_or_folder(title_file="Select a video file", title_folder="Select a folder") -> Optional[Path]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None

    chosen: dict = {"path": None}

    def choose_file():
        p = filedialog.askopenfilename(title=title_file)
        chosen["path"] = Path(p) if p else None
        win.destroy()

    def choose_folder():
        p = filedialog.askdirectory(title=title_folder)
        chosen["path"] = Path(p) if p else None
        win.destroy()

    win = tk.Tk()
    win.title("Movie Tools — Pick target")
    win.geometry("300x150")
    try:
        win.attributes("-topmost", True)
        win.after(300, lambda: win.attributes("-topmost", False))
    except Exception:
        pass
    tk.Label(win, text="What do you want to process?", font=("Segoe UI", 11)).pack(pady=12)
    tk.Button(win, text="Pick File", width=18, command=choose_file).pack(pady=4)
    tk.Button(win, text="Pick Folder", width=18, command=choose_folder).pack(pady=4)
    tk.Button(win, text="Cancel", width=18, command=win.destroy).pack(pady=6)
    win.mainloop()
    return chosen["path"]

def shell_quote(p: str) -> str:
    if os.name == "nt":
        return '"' + p.replace('"', '\\"') + '"'
    else:
        return "'" + p.replace("'", "'\\''") + "'"

# ---------------- Config (persist API key) ----------------
def config_path() -> Path:
    try:
        from platformdirs import user_config_dir
        base = Path(user_config_dir("MovieRenamer", "MovieTools"))
    except Exception:
        if os.name == "nt":
            base = Path(os.getenv("APPDATA") or Path.home() / "AppData/Roaming") / "MovieRenamer"
        elif sys.platform == "darwin":
            base = Path.home() / "Library/Application Support" / "MovieRenamer"
        else:
            base = Path.home() / ".config" / "MovieRenamer"
    return base / "config.json"

def load_api_key_from_config() -> Optional[str]:
    cfg = config_path()
    try:
        if cfg.is_file():
            with open(cfg, "r", encoding="utf-8") as f:
                data = json.load(f)
            key = (data or {}).get("tmdb_api_key", "")
            return key.strip() or None
    except Exception:
        pass
    return None

def save_api_key_to_config(api_key: str):
    cfg = config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"tmdb_api_key": api_key}, f, indent=2)

# ---------------- Logging ----------------
def setup_logging(verbose: bool):
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])

# ---------------- Pause helpers ----------------
def _should_pause(args) -> bool:
    if args and getattr(args, "pause", False):
        return True
    return bool(os.getenv("MOVIETOOLS_PAUSE", "").strip())

def _do_pause(args):
    secs = max(0, int(getattr(args, "pause_seconds", 0) or 0))
    if secs > 0:
        try:
            print(f"\n(Waiting {secs} seconds before exit...)")
            time.sleep(secs)
        except Exception:
            pass
    elif _should_pause(args):
        try:
            input("\nPress Enter to exit...")
        except Exception:
            pass

# ---------------- Patterns ----------------
VIDEO_EXTS = {".mkv",".mp4",".avi",".mov",".wmv",".m4v",".mpg",".mpeg",".ts",".m2ts",".flv",".webm"}
SUB_EXTS   = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}

NOISE_PATTERNS = [
    r"\b(?:480p|720p|1080p|2160p|4k|8k)\b",
    r"\b(?:hdr10|dolby[\s\-]?vision|dv|hdr|sdr)\b",
    r"\b(?:webrip|web\-?dl|bluray|b[dr]rip|hdtv|dvdrip|dvdscr|cam|h?dcam|r[56])\b",
    r"\b(?:x264|x265|h264|h265|hevc|av1|xvid|divx)\b",
    r"\b(?:yts|yify|rarbg|evo|etrg|spark[s]?|amiable|ntb|tge|ctrlhd|fg[t]?|galaxyrg)\b",
    r"\b(?:proper|real|repack|extended|unrated|directors\.? cut|remastered|imax)\b",
    r"\b(?:multi(?:lang)?|dubbed?|subbed?)\b",
    r"\b(?:aac|dts(?:\-hd)?|truehd|atmos|ddp?[\- ]?\d\.\d)\b",
    r"\b(?:sample)\b",
    r"\[(?:.*?)\]|\((?:sample)\)",
]

CLUTTER_FILES = [
    r"(?i)^RARBG.*\.txt$",
    r"(?i)^Sample.*",
    r"(?i)\.nfo$",
    r"(?i)\.sfv$",
    r"(?i)\.nzb$",
    r"(?i)\.torrent$",
    r"(?i)^(readme|thanks|how to|instructions|verify|serial|keygen).*\.(txt)$",
]

WIN_ILLEGAL_RE = re.compile(r'[<>:"/\\\|?*\x00-\x1F]')
RESERVED_WIN_NAMES = {"con","prn","aux","nul",*(f"com{i}" for i in range(1,10)),*(f"lpt{i}" for i in range(1,10))}

def sanitize_component(name: str) -> str:
    name = WIN_ILLEGAL_RE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    if name.lower() in RESERVED_WIN_NAMES:
        name = f"{name}_"
    return name

def split_stem_year(stem: str) -> Tuple[str, Optional[int]]:
    s = re.sub(r"[._]+", " ", stem)
    for pat in NOISE_PATTERNS:
        s = re.sub(pat, " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    year_match = list(re.finditer(r"(?<!\d)((18(8|9)\d|19\d{2}|20\d{2}))(?!\d)", s))
    year = int(year_match[-1].group(1)) if year_match else None
    title = s
    if year:
        idx = s.rfind(str(year))
        if idx != -1:
            title = s[:idx].strip(" -._()[]{}").strip()
    return (title if title else stem, year)

def build_ny(title: str, year: Optional[int]) -> str:
    return sanitize_component(f"{title}{f' ({year})' if year else ''}")

# --------- Formatting helpers ----------
def _pad2(val: Optional[int]) -> str:
    return f"{int(val):02d}" if val is not None else ""

def _sanitize_path_components(rel_path: str) -> Path:
    parts = re.split(r"[\\/]+", rel_path.strip().strip("/\\"))
    parts = [sanitize_component(p) for p in parts if p]
    return Path(*parts)

def render_format(fmt: str, ctx: Dict[str, Any]) -> Path:
    safe = {
        "n": sanitize_component(str(ctx.get("n", "") or "")),
        "y": str(ctx.get("y", "") or ""),
        "ny": sanitize_component(str(ctx.get("ny", "") or "")),
        "s": _pad2(ctx.get("s")),
        "e": _pad2(ctx.get("e")),
        "s00e00": str(ctx.get("s00e00", "") or ""),
        "t": sanitize_component(str(ctx.get("t", "") or "")),
    }
    out = fmt
    for k, v in safe.items():
        out = out.replace("{"+k+"}", v)
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"\s*-\s*$", "", out)
    out = re.sub(r"\(\s*\)", "", out)
    return _sanitize_path_components(out)

# ---------------- TMDB ----------------
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

class TMDB:
    def __init__(self, api_key: str, language: str = "en-US", session: Optional[requests.Session] = None):
        self.api_key = api_key
        self.language = language
        self.sess = session or self._build_session()
        self._cfg = None

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        retries = Retry(total=5, backoff_factor=0.5,
                        status_forcelist=(429, 500, 502, 503, 504),
                        allowed_methods=frozenset(["GET"]))
        s.mount("https://", HTTPAdapter(max_retries=retries))
        return s

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        url = f"https://api.themoviedb.org/3{path}"
        params = {"api_key": self.api_key, "language": self.language, **params}
        r = self.sess.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def configuration(self) -> Dict[str, Any]:
        if not self._cfg:
            self._cfg = self._get("/configuration", {})
        return self._cfg

    def search_movie(self, query: str, year: Optional[int]) -> List[Dict[str, Any]]:
        params = {"query": query, "include_adult": False}
        if year:
            params["primary_release_year"] = str(year)
        data = self._get("/search/movie", params)
        return data.get("results", [])

    def build_poster_url(self, poster_path: str, size: str = "w500") -> Optional[str]:
        if not poster_path:
            return None
        cfg = self.configuration()
        base = cfg.get("images", {}).get("secure_base_url", "")
        sizes = cfg.get("images", {}).get("poster_sizes", []) or ["w500", "original"]
        target = size if size in sizes else (sizes[-2] if len(sizes) >= 2 else sizes[-1])
        return f"{base}{target}{poster_path}"

    def get_movie_videos(self, movie_id: int) -> List[Dict[str, Any]]:
        data = self._get(f"/movie/{movie_id}/videos", {
            "language": self.language,
            "include_video_language": "en,null",
        })
        return data.get("results", []) or []

class TMDBTV(TMDB):
    def search_tv(self, query: str, year: Optional[int]) -> List[Dict[str, Any]]:
        params = {"query": query, "include_adult": False}
        if year:
            params["first_air_date_year"] = str(year)
        data = self._get("/search/tv", params)
        return data.get("results", [])

    def get_episode(self, tv_id: int, season: int, episode: int) -> Optional[Dict[str, Any]]:
        try:
            return self._get(f"/tv/{tv_id}/season/{season}/episode/{episode}", {})
        except Exception:
            return None

    def get_tv_videos(self, tv_id: int) -> List[Dict[str, Any]]:
        data = self._get(f"/tv/{tv_id}/videos", {
            "language": self.language,
            "include_video_language": "en,null",
        })
        return data.get("results", []) or []

    def get_season_details(self, tv_id: int, season_num: int) -> Optional[Dict[str, Any]]:
        try:
            return self._get(f"/tv/{tv_id}/season/{season_num}", {})
        except Exception:
            return None

# ---------------- Trailer picking ----------------
YOUTUBE_WATCH = "https://www.youtube.com/watch?v="

def _norm_lang(s: Optional[str]) -> str:
    return (s or "").lower()

def pick_best_trailer(videos: List[Dict[str, Any]], prefer_langs: List[str]) -> Optional[str]:
    if not videos:
        return None
    prefer_norm = [_norm_lang(x) for x in (prefer_langs or []) if x] or ["en-us", "en"]

    def score(v: Dict[str, Any]) -> float:
        typ = (v.get("type") or "").lower()
        site = (v.get("site") or "").lower()
        name = (v.get("name") or "").lower()
        size = int(v.get("size") or 0)
        off  = bool(v.get("official"))
        lang = _norm_lang(v.get("iso_639_1"))
        pub  = v.get("published_at") or ""
        s = 0.0
        if typ == "trailer": s += 3.0
        elif typ == "teaser": s += 1.0
        if off: s += 3.0
        if site == "youtube": s += 2.0
        if size >= 1080: s += 2.0
        elif size >= 720: s += 1.0
        if "official trailer" in name: s += 2.0
        elif "trailer" in name: s += 1.0
        if lang:
            try:
                idx = prefer_norm.index(lang)
                s += 2.0 if idx == 0 else 1.0
            except ValueError:
                if any((lang == "en" and p.startswith("en")) or (p == "en" and lang.startswith("en")) for p in prefer_norm):
                    s += 1.0
        else:
            s += 0.2
        s += (hash(pub) % 1000) / 1000.0 * 1.5
        return s

    yt = [v for v in videos if (v.get("site") or "").lower() == "youtube" and v.get("key")]
    pool = yt if yt else videos
    best = max(pool, key=score)
    if (best.get("site") or "").lower() == "youtube" and best.get("key"):
        return YOUTUBE_WATCH + best["key"]
    return None

# ---------------- Match helpers ----------------
def jaccard(a: str, b: str) -> float:
    def norm(s: str) -> List[str]:
        s = re.sub(r"[^\w\s]", " ", s.lower())
        s = re.sub(r"\s+", " ", s).strip()
        return [t for t in s.split(" ") if t]
    A, B = set(norm(a)), set(norm(b))
    return (len(A & B) / len(A | B)) if A and B else 0.0

def choose_best_match(cands: List[Dict[str, Any]], want_title: str, want_year: Optional[int]) -> Optional[Dict[str, Any]]:
    if not cands: return None
    scored = []
    for c in cands:
        title = c.get("title") or c.get("original_title") or ""
        rd = c.get("release_date") or ""
        year = int(rd[:4]) if rd[:4].isdigit() else None
        sim = jaccard(title, want_title)
        year_score = 0.0
        if want_year and year:
            diff = abs(want_year - year)
            year_score = 1.0 if diff == 0 else (0.7 if diff == 1 else (0.4 if diff == 2 else 0.0))
        pop = min(float(c.get("popularity") or 0.0) / 200.0, 0.5)
        score = sim * 0.65 + year_score * 0.25 + pop * 0.10
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    top_score, top = scored[0]
    gate = 0.20 if re.fullmatch(r"(18(8|9)\d|19\d{2}|20\d{2})", want_title.strip()) else 0.25
    return top if top_score >= gate else None

def choose_best_tv(cands: List[Dict[str, Any]], want_title: str, want_year: Optional[int]) -> Optional[Dict[str, Any]]:
    if not cands: return None
    scored = []
    for c in cands:
        name = c.get("name") or c.get("original_name") or ""
        fad = c.get("first_air_date") or ""
        year = int(fad[:4]) if fad[:4].isdigit() else None
        sim = jaccard(name, want_title)
        year_score = 0.0
        if want_year and year:
            diff = abs(want_year - year)
            year_score = 1.0 if diff == 0 else (0.6 if diff == 1 else (0.3 if diff == 2 else 0.0))
        pop = min(float(c.get("popularity") or 0.0) / 200.0, 0.5)
        score = sim * 0.72 + year_score * 0.18 + pop * 0.10
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    top_score, top = scored[0]
    return top if top_score >= 0.18 else None

# ---------------- File ops ----------------
def ensure_unique_path(dst: Path) -> Path:
    if not dst.exists():
        return dst
    base, ext, parent, n = dst.stem, dst.suffix, dst.parent, 2
    while True:
        cand = parent / f"{base} ({n}){ext}"
        if not cand.exists():
            return cand
        n += 1

def move_sidecars(src_file: Path, dest_stem: Path, dry_run: bool):
    src_base = src_file.with_suffix("")
    parent = src_file.parent
    for p in parent.iterdir():
        if p == src_file or not p.is_file():
            continue
        if p.name.lower().startswith(src_base.name.lower()) and p.suffix.lower() in SUB_EXTS:
            extra = p.name[len(src_base.name):-len(p.suffix)] if len(p.suffix) != 0 else ""
            new_name = dest_stem.name + extra + p.suffix
            dst = dest_stem.parent / new_name
            logging.info(f"  ↳ sidecar: {p.name} → {dst.name}")
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(dst))

def clean_clutter(folder: Path, dry_run: bool):
    if not folder.exists() or not folder.is_dir():
        return
    for p in list(folder.iterdir()):
        if p.is_dir():
            if re.search(r"(?i)\bsample\b", p.name):
                logging.info(f"  ↳ remove dir: {p.name}")
                if not dry_run:
                    shutil.rmtree(p, ignore_errors=True)
            continue
        for pat in CLUTTER_FILES:
            if re.search(pat, p.name):
                logging.info(f"  ↳ delete: {p.name}")
                if not dry_run:
                    try:
                        p.unlink(missing_ok=True)
                    except Exception as e:
                        logging.warning(f"  ! delete failed: {e}")
                break

def prune_empty_dirs(root: Path, dry_run: bool):
    for dirpath, _, _ in os.walk(root, topdown=False):
        d = Path(dirpath)
        if d == root:
            continue
        try:
            if not any(d.iterdir()):
                logging.info(f"prune: {d}")
                if not dry_run:
                    d.rmdir()
        except Exception:
            pass

# ---------------- Poster download ----------------
def download_poster(tmdb: TMDB, item: Dict[str, Any], out_dir: Path, dry_run: bool, kind: str = "movie"):
    poster_path = item.get("poster_path")
    if not poster_path:
        return
    url = tmdb.build_poster_url(poster_path, size="w500")
    if not url:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    base = sanitize_component(out_dir.name)
    suffix = "poster.jpg" if kind == "movie" else "season poster.jpg"
    poster_filename = f"{base} - {suffix}"
    target = out_dir / poster_filename
    try:
        logging.info(f"  ↳ cover: {poster_filename}")
        if dry_run:
            log_jsonl("poster.dry", {"dir": str(out_dir), "url": url, "kind": kind})
            return
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            if "image" not in (r.headers.get("Content-Type") or ""):
                logging.warning("  ! poster is not an image; skipping")
                return
            data = r.content
            if len(data) < 1024:
                logging.warning("  ! poster too small; skipping")
                return
        with open(target, "wb") as f:
            f.write(data)
        log_jsonl("poster.ok", {"file": str(target), "url": url, "kind": kind})
    except Exception as e:
        logging.warning(f"  ! cover download failed: {e}")
        log_jsonl("poster.err", {"dir": str(out_dir), "err": str(e), "kind": kind})

def download_season_poster(tmdbtv: TMDBTV, tv_id: int, season_num: int, out_dir: Path, dry_run: bool):
    det = tmdbtv.get_season_details(tv_id, season_num) or {}
    if not det.get("poster_path"):
        return
    item = {"poster_path": det.get("poster_path")}
    download_poster(tmdbtv, item, out_dir, dry_run, kind="season")

# ---------------- Trailer download helper ----------------

def download_trailer_with_ytdlp(url: str, out_dir: Path, dry_run: bool = False) -> bool:
    """
    Integrated trailer downloader (inlined from ):
    - Tries local cookies.txt next to this script; if missing, generates one from a browser (browser-cookie3).
    - Falls back across several yt-dlp client modes to dodge throttling.
    - Embeds metadata and thumbnail.
    Returns True on success, False on failure.
    """
    import os, sys, shutil, subprocess
    from pathlib import Path

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        logging.info(f"  ↳ trailer: {url}\n      → {out_dir}")
        if dry_run:
            log_jsonl("trailer.dry", {"url": url, "dir": str(out_dir)})
            return True

        # Ensure yt-dlp present
        if not ensure_ytdlp_installed():
            logging.warning("  ! yt-dlp missing and could not be installed automatically.")
            return False

        # Local helper utilities (light copies from )
        SABR_SIGNS = (
            "sabr streaming",
            "nsig extraction failed",
            "only images are available",
            "requested format is not available",
        )
        Y_DOMAINS = (
            "youtube.com","www.youtube.com","m.youtube.com","studio.youtube.com",
            "accounts.youtube.com","google.com","www.google.com","apis.google.com",
            "ytimg.com","i.ytimg.com","ggpht.com",
        )

        def _ytdlp_base():
            # Always use module mode to ensure we use the freshly pip-installed yt-dlp
            return [sys.executable, "-m", "yt_dlp"]

        def _run(cmd, cwd):
            return subprocess.run(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def _sanitize(name: str) -> str:
            return "".join("_" if c in '<>:\"/\\|?*\x00' else c for c in name).strip().rstrip(".")

        def _ensure_browser_cookie3(verbose: bool=False) -> bool:
            try:
                import browser_cookie3  # noqa: F401
                return True
            except Exception:
                try:
                    if verbose:
                        logging.info("[cookies] installing browser-cookie3…")
                    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "browser-cookie3"])
                    import browser_cookie3  # noqa: F401
                    return True
                except Exception as e:
                    if verbose:
                        logging.info(f"[cookies] failed to install browser-cookie3: {e}")
                    return False

        def _cookiejar_from_browser(browser: str, profile_hint: str|None, verbose: bool=False):
            try:
                import browser_cookie3 as bc3
            except Exception:
                return None
            try:
                if browser == "firefox":
                    cj = bc3.firefox(profile=profile_hint) if profile_hint else bc3.firefox()
                elif browser == "chrome":
                    cj = bc3.chrome(domain_name=None, keyring=None, cookie_file=None, profile=profile_hint)
                elif browser == "edge":
                    cj = bc3.edge(domain_name=None, cookie_file=None, profile=profile_hint)
                elif browser == "chromium":
                    cj = bc3.chromium(domain_name=None, cookie_file=None, profile=profile_hint)
                else:
                    if verbose:
                        logging.info(f"[cookies] unknown browser '{browser}', falling back to firefox")
                    cj = bc3.firefox(profile=profile_hint) if profile_hint else bc3.firefox()
                return cj
            except Exception as e:
                if verbose:
                    logging.info(f"[cookies] failed to read cookies from {browser} ({profile_hint or 'default'}): {e}")
                return None

        def _write_netscape_cookies(cj, out_path: Path, verbose: bool=False) -> bool:
            try:
                lines = ["# Netscape HTTP Cookie File"]
                count = 0
                for c in cj:
                    if not any(d for d in Y_DOMAINS if getattr(c, "domain", "") and c.domain.endswith(d)):
                        continue
                    domain = c.domain or ""
                    include_sub = "TRUE" if domain.startswith(".") else "FALSE"
                    path = c.path or "/"
                    secure = "TRUE" if getattr(c, "secure", False) else "FALSE"
                    expires = int(getattr(c, "expires", 0) or 0)
                    name = c.name or ""
                    value = c.value or ""
                    line = f"{domain}	{include_sub}	{path}	{secure}	{expires}	{name}	{value}"
                    lines.append(line)
                    count += 1
                if count == 0:
                    if verbose:
                        logging.info("[cookies] no relevant cookies found to write")
                    return False
                out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                if verbose:
                    logging.info(f"[cookies] wrote {count} cookies → {out_path}")
                return True
            except Exception as e:
                if verbose:
                    logging.info(f"[cookies] failed to write cookies.txt: {e}")
                return False

        def _ensure_local_cookies(script_dir: Path, verbose: bool=False):
            cookies_path = script_dir / "cookies.txt"
            if cookies_path.is_file():
                return cookies_path
            browser = (os.getenv("YT_COOKIE_BROWSER") or "firefox").strip().lower()
            profile_hint = (os.getenv("YT_COOKIE_PROFILE") or "").strip() or None
            if verbose:
                logging.info(f"[cookies] cookies.txt not found; attempting to extract from {browser} (profile={profile_hint or 'auto'})")
            if not _ensure_browser_cookie3(verbose=verbose):
                return None
            cj = _cookiejar_from_browser(browser, profile_hint, verbose=verbose)
            if not cj:
                return None
            return cookies_path if _write_netscape_cookies(cj, cookies_path, verbose=verbose) else None

        verbose = os.getenv("YT_VERBOSE", "").strip() not in ("", "0", "false", "False")
        strict  = os.getenv("TRAILER_STRICT", "").strip() not in ("", "0", "false", "False")
        cookies_from_browser = (os.getenv("YT_COOKIES_FROM_BROWSER") or "").strip()
        po_android = (os.getenv("YT_PO_TOKEN_ANDROID") or "").strip()
        po_ios     = (os.getenv("YT_PO_TOKEN_IOS") or "").strip()

        base_name = _sanitize(out_dir.name) or "Trailer"
        out_tpl = f"{base_name} - trailer.%(ext)s"

        base = _ytdlp_base()
        common = ["-f", "bestvideo*+bestaudio/best", "--embed-metadata", "--embed-thumbnail", "-o", out_tpl, url]

        script_dir = Path(__file__).resolve().parent
        local_cookies = _ensure_local_cookies(script_dir, verbose=verbose)

        attempts: list[tuple[str, list[str]]] = []
        if local_cookies and local_cookies.is_file():
            if verbose:
                logging.info(f"[trailer_dl] Using cookies file: {local_cookies}")
            attempts.append(("web+cookies-file(local)", base + ["--cookies", str(local_cookies), *common]))
        attempts.append(("web", base + common))
        if cookies_from_browser:
            attempts.append((f"web+cookies({cookies_from_browser})", base + ["--cookies-from-browser", cookies_from_browser, *common]))
        if po_android:
            attempts.append(("android+po", base + ["--extractor-args", f"youtube:player_client=android,po_token={po_android}", *common]))
        if po_ios:
            attempts.append(("ios+po", base + ["--extractor-args", f"youtube:player_client=ios,po_token={po_ios}", *common]))
        attempts.append(("tv", base + ["--extractor-args", "youtube:player_client=tv", *common]))
        attempts.append(("tv_embedded", base + ["--extractor-args", "youtube:player_client=tv_embedded", *common]))

        if strict:
            attempts = [a for a in attempts if a[0] == "web"]

        combined_out = ""
        for label, cmd in attempts:
            res = _run(cmd, cwd=out_dir)
            out = res.stdout or ""
            combined_out += (f"\n--- attempt: {label} ---\n{out}" if verbose else out)

            if res.returncode == 0:
                if combined_out.strip():
                    for line in combined_out.splitlines():
                        if line.strip():
                            logging.info(line)
                log_jsonl("trailer.ok", {"url": url, "dir": str(out_dir), "via": "inline"})
                return True

            low = out.lower()
            if (("--cookies " in " ".join(cmd)) or ("--cookies-from-browser" in " ".join(cmd))) and not any(s in low for s in SABR_SIGNS):
                # Cookies-based failure that doesn't look like SABR throttling: stop early.
                if combined_out.strip():
                    for line in combined_out.splitlines():
                        if line.strip():
                            logging.info(line)
                log_jsonl("trailer.err", {"url": url, "dir": str(out_dir), "via": "inline", "out": combined_out})
                return False

        if combined_out.strip():
            for line in combined_out.splitlines():
                if line.strip():
                    logging.info(line)
        logging.warning("  ! yt-dlp failed for all attempts"); logging.warning("    (Hint) Try setting env YT_COOKIES_FROM_BROWSER=chrome|firefox and ensure you are logged in, or delete cookies.txt to refresh.")
        log_jsonl("trailer.err", {"url": url, "dir": str(out_dir), "via": "inline", "out": combined_out})
        return False

    except Exception as e:
        logging.warning(f"  ! trailer download error: {e}")
        log_jsonl("trailer.exc", {"url": url, "dir": str(out_dir), "err": str(e)})
        return False

# ---------------- Info builder ----------------
# ---------------- Info builder ----------------
def first_video_under(folder: Path) -> Optional[Path]:
    try:
        for dirpath, _, filenames in os.walk(folder):
            for n in filenames:
                p = Path(dirpath) / n
                if p.suffix.lower() in VIDEO_EXTS:
                    return p
    except Exception:
        pass
    return None

def get_best_media_info(target: Path, tmdb_movie: TMDB, tmdb_tv: TMDBTV) -> Dict[str, Any]:
    title_guess, year_guess = split_stem_year(target.stem if target.is_file() else (target.name))
    s_e = re.search(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})", target.name)
    prefer_tv = bool(s_e)

    def pick_trailer(videos_func, id_val) -> Optional[str]:
        try:
            ui = getattr(tmdb_movie, "language", "en-US")
            langs = [ui, ui.split("-")[0] if "-" in ui else ui, "en-US", "en"]
            vids = videos_func(int(id_val))
            return pick_best_trailer(vids, langs)
        except Exception:
            return None

    if prefer_tv:
        tv_cands = tmdb_tv.search_tv(title_guess, year_guess)
        show = choose_best_tv(tv_cands, title_guess, year_guess)
        if show:
            fad = (show.get("first_air_date") or "")[:4]
            poster_url = tmdb_tv.build_poster_url(show.get("poster_path") or "", "w500")
            trailer_url = pick_trailer(tmdb_tv.get_tv_videos, show.get("id"))
            return {
                "kind": "tv",
                "title": show.get("name") or show.get("original_name") or title_guess,
                "year": int(fad) if fad.isdigit() else year_guess,
                "overview": show.get("overview") or "",
                "poster_url": poster_url,
                "trailer_url": trailer_url,
                "tmdb_id": show.get("id"),
            }

    mv_cands = tmdb_movie.search_movie(title_guess, year_guess)
    mv = choose_best_match(mv_cands, title_guess, year_guess)
    if mv:
        rd = (mv.get("release_date") or "")[:4]
        poster_url = tmdb_movie.build_poster_url(mv.get("poster_path") or "", "w500")
        trailer_url = pick_trailer(tmdb_movie.get_movie_videos, mv.get("id"))
        return {
            "kind": "movie",
            "title": mv.get("title") or mv.get("original_title") or title_guess,
            "year": int(rd) if rd.isdigit() else year_guess,
            "overview": mv.get("overview") or "",
            "poster_url": poster_url,
            "trailer_url": trailer_url,
            "tmdb_id": mv.get("id"),
        }

    tv_cands = tmdb_tv.search_tv(title_guess, year_guess)
    show = choose_best_tv(tv_cands, title_guess, year_guess)
    if show:
        fad = (show.get("first_air_date") or "")[:4]
        poster_url = tmdb_tv.build_poster_url(show.get("poster_path") or "", "w500")
        trailer_url = pick_trailer(tmdb_tv.get_tv_videos, show.get("id"))
        return {
            "kind": "tv",
            "title": show.get("name") or show.get("original_name") or title_guess,
            "year": int(fad) if fad.isdigit() else year_guess,
            "overview": show.get("overview") or "",
            "poster_url": poster_url,
            "trailer_url": trailer_url,
            "tmdb_id": show.get("id"),
        }

    return {"kind": "unknown", "title": title_guess, "year": year_guess, "overview": "", "poster_url": None, "trailer_url": None}

# ---------------- Series helpers ----------------
def s00e00(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"

def _safe_int(txt: Optional[str]) -> Optional[int]:
    return int(txt) if txt and str(txt).isdigit() else None

def normalize_show_hint(txt: str) -> str:
    s = txt
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"[\(\[][^)\]]{0,12}[\)\]]", " ", s)
    junk = r"\b(1080p|2160p|720p|4k|webrip|web[- ]?dl|bluray|b[dr]rip|hdtv|x26[45]|h26[45]|hevc|av1|hdr10?|dv|sdr|multi|dubbed|subbed)\b"
    s = re.sub(junk, " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def season_folder_parent(file_path: Path) -> Optional[Path]:
    p = file_path.parent
    if p and re.search(r"(?i)\bseason\b|\bseizoen\b", p.name):
        return p.parent
    return None

# ---------------- Core rename/series flows ----------------
def parse_filename_basic(path: str) -> Tuple[str, Optional[str], Optional[int], Optional[int]]:
    base = os.path.splitext(os.path.basename(path))[0]
    m_year = re.search(r"\b(19|20)\d{2}\b", base)
    year = m_year.group(0) if m_year else None
    s = e = None
    m1 = re.search(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})", base)
    if m1:
        s, e = int(m1.group(1)), int(m1.group(2))
    else:
        m2 = re.search(r"(\d{1,2})x(\d{1,2})", base)
        if m2:
            s, e = int(m2.group(1)), int(m2.group(2))
    cleaned = re.sub(r"\b(S\d+E\d+|\d+x\d+|(19|20)\d{2}|480p|720p|1080p|2160p|WEB[-.]DL|BluRay|HDR|x264|x265)\b.*",
                     "", base, flags=re.I)
    title = re.sub(r"\s+", " ", re.sub(r"[\\/:\*\?\"<>\\|]", "", cleaned)).strip() or "Unknown"
    return title, year, s, e

def try_tv_match_with_fallbacks(tmdbtv: TMDBTV, file_path: Path, title_guess: str, year_guess: Optional[int],
                                force_show: Optional[str] = None, force_year: Optional[int] = None,
                                debug: bool = False) -> Optional[Dict[str, Any]]:
    cands: List[Tuple[str, Optional[int]]] = []
    if force_show:
        cands.append((normalize_show_hint(force_show), force_year))

    title_from_parse, year_from_parse, _, _ = parse_filename_basic(str(file_path))
    if title_from_parse and re.search(r"[A-Za-z]", title_from_parse):
        cands.append((normalize_show_hint(title_from_parse), _safe_int(year_from_parse)))

    if title_guess and re.search(r"[A-Za-z]", title_guess):
        cands.append((normalize_show_hint(title_guess), year_guess))

    sfp = season_folder_parent(file_path)
    parent_chain = [sfp] if sfp else []
    parent_chain += [file_path.parent, file_path.parent.parent]

    for parent in parent_chain:
        if parent and parent != file_path and parent.name:
            t, y = split_stem_year(parent.name)
            t = normalize_show_hint(t)
            if t and re.search(r"[A-Za-z]", t):
                cands.append((t, y))

    seen = set(); uniq: List[Tuple[str, Optional[int]]] = []
    for t, y in cands:
        key = t.lower()
        if key not in seen:
            seen.add(key); uniq.append((t, y))

    for t, y in uniq:
        for qt, qy in [(t, None), (t, y)]:
            results = tmdbtv.search_tv(qt, qy)
            show = choose_best_tv(results, qt, qy)
            if show:
                if debug:
                    print(f"[debug] matched: {show.get('name')} ({(show.get('first_air_date') or '')[:4]})")
                return show
    return None

def process_video(tmdb: TMDB, file_path: Path, dry_run: bool, want_trailer: bool) -> Optional[Tuple[Path, Path]]:
    stem = file_path.stem
    title_guess, year_guess = split_stem_year(stem)
    logging.info(f"→ {file_path.name}  [guess: '{title_guess}' {year_guess or ''}]")
    matches = tmdb.search_movie(title_guess, year_guess)
    movie = choose_best_match(matches, title_guess, year_guess)
    if not movie and year_guess:
        movie = choose_best_match(tmdb.search_movie(title_guess, None), title_guess, None)
    if not movie:
        logging.warning("  ! no confident TMDB match; skipping")
        return None

    title = movie.get("title") or movie.get("original_title") or title_guess
    rd = movie.get("release_date") or ""
    year = int(rd[:4]) if rd[:4].isdigit() else (year_guess or None)

    ny = build_ny(title, year)
    fmt = globals().get("CLI_MOVIE_FMT") or "{ny}/{ny}"
    ctx = {"n": title, "y": year, "ny": ny}
    rel = render_format(fmt, ctx)
    dest_path_wo_ext = file_path.parent / rel
    dest_path = ensure_unique_path(dest_path_wo_ext.with_suffix(file_path.suffix.lower()))

    logging.info(f"  ↳ rename: {file_path.name} → {dest_path.relative_to(file_path.parent)}")
    if not dry_run:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(file_path), str(dest_path))
        move_sidecars(file_path, dest_path.with_suffix(""), dry_run=False)
    log_jsonl("rename.movie", {"src": str(file_path), "dst": str(dest_path)})

    try:
        ui_lang = getattr(tmdb, "language", "en-US")
        prefer_langs = [ui_lang, ui_lang.split("-")[0] if "-" in ui_lang else ui_lang, "en-US", "en"]
        vids = tmdb.get_movie_videos(int(movie["id"]))
        trailer_url = pick_best_trailer(vids, prefer_langs)
        if trailer_url:
            logging.info(f"  ↳ best trailer: {trailer_url}")
            if want_trailer:
                download_trailer_with_ytdlp(trailer_url, dest_path.parent, dry_run=dry_run)
        else:
            logging.info("  ↳ no trailer found")
    except Exception as e:
        logging.warning(f"  ! trailer lookup failed: {e}")

    return (file_path, dest_path)

def handle_root(root: Path, tmdb: TMDB, do_cover: bool, do_clean: bool, do_prune: bool, dry_run: bool, want_trailer: bool):
    touched_parents = set()
    if root.is_file():
        if root.suffix.lower() in VIDEO_EXTS:
            res = process_video(tmdb, root, dry_run, want_trailer)
            if res:
                old, new = res
                touched_parents.add(old.parent)
                if do_cover:
                    tguess, yguess = split_stem_year(new.stem)
                    mv = choose_best_match(tmdb.search_movie(tguess, yguess), tguess, yguess) or \
                         choose_best_match(tmdb.search_movie(tguess, None), tguess, None)
                    if mv:
                        download_poster(tmdb, mv, new.parent, dry_run=dry_run, kind="movie")
        else:
            logging.warning("Not a supported video file.")
    else:
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() not in VIDEO_EXTS:
                    continue
                res = process_video(tmdb, p, dry_run, want_trailer)
                if res:
                    old, new = res
                    touched_parents.add(old.parent)
                    if do_cover:
                        tguess, yguess = split_stem_year(new.stem)
                        mv = choose_best_match(tmdb.search_movie(tguess, yguess), tguess, yguess) or \
                             choose_best_match(tmdb.search_movie(tguess, None), tguess, None)
                        if mv:
                            download_poster(tmdb, mv, new.parent, dry_run=dry_run, kind="movie")

    if do_clean:
        for folder in sorted(touched_parents):
            logging.info(f"clean: {folder}")
            clean_clutter(folder, dry_run)
            log_jsonl("clean", {"dir": str(folder)})
    if do_prune:
        prune_empty_dirs(root if root.is_dir() else root.parent, dry_run)
        log_jsonl("prune", {"root": str(root)})

def process_series_file(tmdbtv: TMDBTV, file_path: Path, layout: str, do_cover: bool, do_season_covers: bool, dry_run: bool, want_trailer: bool) -> Optional[Tuple[Path, Path, Dict[str, Any]]]:
    stem = file_path.stem
    title_guess, year_guess = split_stem_year(stem)
    _, _, season, episode = parse_filename_basic(str(file_path))
    if not (season and episode):
        logging.warning(f"  ! no SxxEyy detected in '{file_path.name}'; skipping")
        return None

    show = try_tv_match_with_fallbacks(
        tmdbtv, file_path, title_guess, year_guess,
        force_show=getattr(sys.modules.get(__name__), "CLI_FORCE_SHOW", None),
        force_year=getattr(sys.modules.get(__name__), "CLI_FORCE_YEAR", None),
        debug=getattr(sys.modules.get(__name__), "CLI_DEBUG_MATCH", False),
    )
    if not show:
        logging.warning("  ! no confident TMDB TV match; skipping")
        return None

    show_name = show.get("name") or show.get("original_name") or title_guess
    fad = show.get("first_air_date") or ""
    show_year = int(fad[:4]) if fad[:4].isdigit() else (year_guess or None)

    ep = tmdbtv.get_episode(int(show["id"]), season, episode) or {}
    ep_title = ep.get("name") or f"Episode {episode}"

    ny = build_ny(show_name, show_year)
    ctx = {"n": show_name, "y": show_year, "ny": ny, "s": season, "e": episode, "s00e00": s00e00(season, episode), "t": ep_title}

    if globals().get("CLI_SERIES_FMT"):
        fmt = globals()["CLI_SERIES_FMT"]
    else:
        fmt = "{n} ({y}) - {s00e00} - {t}" if layout == "flat" else "{ny}/{ny} - Season {s}/{ny} - {s00e00} - {t}"

    rel = render_format(fmt, ctx)
    dest_wo_ext = file_path.parent / rel
    dest = ensure_unique_path(dest_wo_ext.with_suffix(file_path.suffix.lower()))

    if "/" in fmt or "\\" in fmt or layout == "folders":
        logging.info(f"  ↳ rename: {file_path.name} → {dest.relative_to(file_path.parent)}")
    else:
        logging.info(f"  ↳ rename: {file_path.name} → {dest.name}")

    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(file_path), str(dest))
        move_sidecars(file_path, dest.with_suffix(""), dry_run=False)
    log_jsonl("rename.episode", {"src": str(file_path), "dst": str(dest), "show": show_name, "season": season, "episode": episode})

    if do_cover:
        if show.get("poster_path"):
            download_poster(tmdbtv, show, dest.parent if layout == "flat" else dest.parent, dry_run=dry_run, kind="movie")
    if do_season_covers:
        season_dir = dest.parent
        download_season_poster(tmdbtv, int(show["id"]), season, season_dir, dry_run)

    try:
        ui_lang = getattr(tmdbtv, "language", "en-US")
        prefer_langs = [ui_lang, ui_lang.split("-")[0] if "-" in ui_lang else ui_lang, "en-US", "en"]
        vids = tmdbtv.get_tv_videos(int(show["id"]))
        trailer_url = pick_best_trailer(vids, prefer_langs)
        if trailer_url:
            logging.info(f"  ↳ best trailer: {trailer_url}")
            if want_trailer:
                download_trailer_with_ytdlp(trailer_url, dest.parent, dry_run=dry_run)
        else:
            logging.info("  ↳ no trailer found")
    except Exception as e:
        logging.warning(f"  ! trailer lookup failed: {e}")

    return (file_path, dest, {"show": show, "season": season})

def handle_series_root(root: Path, tmdbtv: TMDBTV, layout: str, do_cover: bool, do_season_covers: bool, do_clean: bool, do_prune: bool, dry_run: bool, want_trailer: bool):
    touched = set()
    if root.is_file():
        if root.suffix.lower() in VIDEO_EXTS:
            res = process_series_file(tmdbtv, root, layout, do_cover, do_season_covers, dry_run, want_trailer)
            if res: touched.add(res[0].parent)
        else:
            logging.warning("Not a supported video file.")
    else:
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() not in VIDEO_EXTS:
                    continue
                res = process_series_file(tmdbtv, p, layout, do_cover, do_season_covers, dry_run, want_trailer)
                if res: touched.add(res[0].parent)

    if do_clean:
        for folder in sorted(touched):
            logging.info(f"clean: {folder}")
            clean_clutter(folder, dry_run)
            log_jsonl("clean", {"dir": str(folder)})
    if do_prune:
        prune_empty_dirs(root if root.is_dir() else root.parent, dry_run)
        log_jsonl("prune", {"root": str(root)})

# ---------------- API key helpers ----------------
def validate_api_key(key: str) -> bool:
    try:
        TMDB(api_key=key).configuration()
        return True
    except Exception:
        return False

def ensure_api_key(cli_key: Optional[str]) -> str:
    key = (cli_key or os.getenv("TMDB_API_KEY") or load_api_key_from_config() or "").strip()
    if key and validate_api_key(key):
        return key
    key_gui = api_key_popup(prefill=key or "")
    if not key_gui or not validate_api_key(key_gui):
        print("Invalid or missing TMDB API key. Aborting.", file=sys.stderr)
        sys.exit(2)
    save_api_key_to_config(key_gui)
    return key_gui

# ---------------- GUI: big dialog ----------------
def gui_options_dialog(target_path: Path) -> Optional[Dict[str, Any]]:
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except Exception:
        return None

    have_pillow = ensure_pillow_installed()
    if have_pillow:
        from PIL import Image, ImageTk  # type: ignore

    DEFAULT_MOVIE_FMT = "{ny}/{ny}"
    DEFAULT_SERIES_FLAT = "{n} ({y}) - {s00e00} - {t}"
    DEFAULT_SERIES_FOLDERS = "{ny}/{ny} - Season {s}/{ny} - {s00e00} - {t}"
    PLACEHOLDERS = ["{n}", "{y}", "{ny}", "{s}", "{e}", "{s00e00}", "{t}"]

    win = tk.Tk()
    win.title("Movie Tools")
    win.geometry("1000x720")
    try:
        win.attributes("-topmost", True)
        win.after(300, lambda: win.attributes("-topmost", False))
    except Exception:
        pass

    # model
    mode_var = tk.IntVar(value=0)  # 0=movies, 1=series
    cover_var = tk.IntVar(value=1)
    season_cover_var = tk.IntVar(value=0)
    trailer_var = tk.IntVar(value=0)
    clean_var = tk.IntVar(value=1)
    prune_var = tk.IntVar(value=1)
    dry_run_var = tk.IntVar(value=0)
    layout_var = tk.IntVar(value=0)  # 0=flat, 1=folders
    movie_fmt_var = tk.StringVar(value=DEFAULT_MOVIE_FMT)
    series_fmt_var = tk.StringVar(value=DEFAULT_SERIES_FLAT)
    language_var = tk.StringVar(value="en-US")
    target_var = tk.StringVar(value=str(target_path))
    cli_cmd_var = tk.StringVar(value="")

    info_title_var = tk.StringVar(value="")
    info_year_var = tk.StringVar(value="")
    info_overview = tk.Text(win, height=8, wrap="word")
    info_trailer_url: Optional[str] = None
    poster_img_obj = {"img": None}

    tmdb_movie = None
    tmdb_tv = None

    nb = ttk.Notebook(win)
    nb.pack(fill="both", expand=True)

    # ---- TAB: Options ----
    tab_opts = ttk.Frame(nb)
    nb.add(tab_opts, text="Options")
    tab_opts.columnconfigure(0, weight=3)
    tab_opts.columnconfigure(1, weight=2)

    left = ttk.Frame(tab_opts, padding=12)
    left.grid(row=0, column=0, sticky="nsew")

    # Target + change
    top_row = ttk.Frame(left)
    top_row.pack(fill="x", pady=(0,8))
    ttk.Label(top_row, text="Target:", font=("Segoe UI", 10, "bold")).pack(side="left")
    ttk.Label(top_row, textvariable=target_var, foreground="#555").pack(side="left", padx=(6,0))
    def change_target():
        p = pick_file_or_folder()
        if not p:
            return
        pv = first_video_under(p) if p.is_dir() else p
        target_var.set(str(pv))
        log_jsonl("pick.target", {"path": str(pv)})
        refresh_info()
        cli_path_var.set(target_var.get())
        cli_cmd_var.set(build_cli_from_builder())
    ttk.Button(top_row, text="Change target…", command=change_target).pack(side="right")

    # Mode
    ttk.Label(left, text="Mode", font=("Segoe UI", 10, "bold")).pack(anchor="w")
    mode_row = ttk.Frame(left); mode_row.pack(fill="x", pady=(2,8))
    ttk.Radiobutton(mode_row, text="Movies (rename)", variable=mode_var, value=0, command=lambda: [set_series_defaults(), refresh_info(), sync_builder_from_options()]).pack(side="left", padx=(0,16))
    ttk.Radiobutton(mode_row, text="Series (tv)", variable=mode_var, value=1, command=lambda: [set_series_defaults(), refresh_info(), sync_builder_from_options()]).pack(side="left")

    # Language only (API key handled automatically via ENV/config/popup)
    lang_row = ttk.Frame(left); lang_row.pack(fill="x", pady=(6,0))
    ttk.Label(lang_row, text="Language (TMDB):").pack(side="left")
    ttk.Entry(lang_row, textvariable=language_var, width=12).pack(side="left", padx=(6,16))

    # Attributes
    ttk.Label(left, text="Attributes", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(6,0))
    ttk.Checkbutton(left, text="Download poster (Movies) / Enable poster (Series via --cover)", variable=cover_var).pack(anchor="w")
    ttk.Checkbutton(left, text="Season posters (Series)", variable=season_cover_var).pack(anchor="w")
    ttk.Checkbutton(left, text="Download trailer", variable=trailer_var).pack(anchor="w")
    ttk.Checkbutton(left, text="Clean clutter", variable=clean_var).pack(anchor="w")
    ttk.Checkbutton(left, text="Prune empty folders", variable=prune_var).pack(anchor="w")
    ttk.Checkbutton(left, text="Dry run (no changes)", variable=dry_run_var).pack(anchor="w")

    # Formats + layout
    help_text = "Placeholders: {n} title, {y} year, {ny} title+year, {s} season, {e} episode, {s00e00}, {t} episode title"
    ttk.Label(left, text="Format", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(10,0))
    ttk.Label(left, text=help_text, foreground="#555").pack(anchor="w")
    chips = ttk.Frame(left); chips.pack(anchor="w", pady=(2,6))
    def insert_token(token: str):
        if mode_var.get() == 0:
            movie_fmt_var.set(movie_fmt_var.get()+token)
        else:
            series_fmt_var.set(series_fmt_var.get()+token)
        sync_builder_from_options()
    for ph in ["{n}","{y}","{ny}","{s}","{e}","{s00e00}","{t}"]:
        ttk.Button(chips, text=ph, command=lambda t=ph: insert_token(t)).pack(side="left", padx=3)

    mf = ttk.Frame(left); mf.pack(fill="x", pady=(4,0))
    ttk.Label(mf, text="Movie format").pack(side="left")
    ttk.Entry(mf, textvariable=movie_fmt_var).pack(side="left", fill="x", expand=True, padx=(8,0))

    sl = ttk.Frame(left); sl.pack(fill="x", pady=(8,0))
    ttk.Label(sl, text="Series layout").pack(side="left")
    layout_cb = ttk.Combobox(sl, values=["flat", "folders"], state="readonly", width=10)
    layout_cb.set("flat")
    layout_cb.bind("<<ComboboxSelected>>", lambda *_: [set_series_defaults(), sync_builder_from_options()])
    layout_cb.pack(side="left", padx=(8,0))
    sf = ttk.Frame(left); sf.pack(fill="x", pady=(4,0))
    ttk.Label(sf, text="Series format").pack(side="left")
    ttk.Entry(sf, textvariable=series_fmt_var).pack(side="left", fill="x", expand=True, padx=(8,0))

    # Quick CLI preview
    quick_cli = ttk.Frame(left); quick_cli.pack(fill="x", pady=(12,0))
    ttk.Label(quick_cli, text="Quick CLI preview", font=("Segoe UI", 10, "bold")).pack(anchor="w")
    cli_entry_quick = ttk.Entry(quick_cli, textvariable=cli_cmd_var); cli_entry_quick.pack(side="left", fill="x", expand=True)
    ttk.Button(quick_cli, text="Copy", command=lambda: (win.clipboard_clear(), win.clipboard_append(cli_cmd_var.get()))).pack(side="left", padx=(8,0))

    # Trailer now
    def download_trailer_now():
        nonlocal info_trailer_url
        url = info_trailer_url
        if not url:
            messagebox.showinfo("Trailer", "No trailer URL found yet.")
            return
        if not ensure_ytdlp_installed():
            messagebox.showwarning("Trailer", "yt-dlp is not installed and could not be installed automatically.")
            return
        out_dir = Path(target_var.get()).parent if Path(target_var.get()).is_file() else Path(target_var.get())
        ok = download_trailer_with_ytdlp(url, out_dir, dry_run=bool(dry_run_var.get()))
        messagebox.showinfo("Trailer", "Downloaded." if ok else "Failed. See log.")

    ttk.Button(left, text="Download trailer now", command=download_trailer_now).pack(anchor="w", pady=(8,0))

    # Right column (Info panel)
    right = ttk.Frame(tab_opts, padding=12, relief="groove", borderwidth=2)
    right.grid(row=0, column=1, sticky="nsew")
    ttk.Label(right, text="Info", font=("Segoe UI", 11, "bold")).pack(anchor="w")

    info_top = ttk.Frame(right); info_top.pack(fill="x", pady=(6,2))
    ttk.Label(info_top, textvariable=info_title_var, font=("Segoe UI", 10, "bold")).pack(anchor="w")
    ttk.Label(info_top, textvariable=info_year_var, foreground="#555").pack(anchor="w")

    poster_lbl = None
    if have_pillow:
        poster_lbl = ttk.Label(right)
        poster_lbl.pack(anchor="w", pady=(6,6))

    ttk.Label(right, text="Overview", font=("Segoe UI", 10, "bold")).pack(anchor="w")
    info_overview.pack(fill="both", expand=False)
    info_overview.configure(state="disabled")

    trailer_row = ttk.Frame(right); trailer_row.pack(fill="x", pady=(8,0))
    trailer_link_var = tk.StringVar(value="")
    def open_trailer():
        url = trailer_link_var.get().strip()
        if url:
            webbrowser.open(url)
    link = ttk.Label(trailer_row, textvariable=trailer_link_var, foreground="#0a79d6", cursor="hand2")
    link.bind("<Button-1>", lambda e: open_trailer())
    ttk.Label(trailer_row, text="Trailer:").pack(side="left")
    link.pack(side="left", padx=(6,0))

    # ---- TAB: CLI Builder ----
    tab_cli = ttk.Frame(nb)
    nb.add(tab_cli, text="CLI Builder")
    cb = ttk.Frame(tab_cli, padding=12); cb.pack(fill="both", expand=True)
    for i in range(4): cb.columnconfigure(i, weight=1)

    cli_mode_var     = tk.IntVar(value=0)
    cli_path_var     = tk.StringVar(value=str(target_path))
    cli_api_key_var  = tk.StringVar(value="")  # kept internal; not shown
    cli_lang_var     = tk.StringVar(value="en-US")
    cli_movie_fmt_var= tk.StringVar(value=DEFAULT_MOVIE_FMT)
    cli_series_fmt_var= tk.StringVar(value=DEFAULT_SERIES_FLAT)
    cli_trailer_var  = tk.IntVar(value=0)
    cli_verbose_var  = tk.IntVar(value=0)
    cli_dryrun_var   = tk.IntVar(value=0)
    cli_pause_var    = tk.IntVar(value=0)
    cli_pause_sec_var= tk.StringVar(value="0")
    cli_movies_no_cover_var = tk.IntVar(value=0)
    cli_movies_no_clean_var = tk.IntVar(value=0)
    cli_movies_no_prune_var = tk.IntVar(value=0)
    cli_series_layout_var   = tk.StringVar(value="flat")
    cli_series_cover_var    = tk.IntVar(value=0)
    cli_series_season_cover_var = tk.IntVar(value=0)
    cli_series_no_clean_var = tk.IntVar(value=0)
    cli_series_no_prune_var = tk.IntVar(value=0)
    cli_series_force_name_var = tk.StringVar(value="")
    cli_series_force_year_var = tk.StringVar(value="")
    cli_series_debug_match_var = tk.IntVar(value=0)

    r = 0
    ttk.Label(cb, text="Build a command with all flags (explanations inline).", foreground="#555").grid(row=r, column=0, columnspan=4, sticky="w"); r+=1

    ttk.Label(cb, text="Mode").grid(row=r, column=0, sticky="w", pady=(10,0))
    ttk.Radiobutton(cb, text="Movies (rename)", variable=cli_mode_var, value=0).grid(row=r, column=1, sticky="w", pady=(10,0))
    ttk.Radiobutton(cb, text="Series (tv)", variable=cli_mode_var, value=1).grid(row=r, column=2, sticky="w", pady=(10,0)); r+=1

    ttk.Label(cb, text="Target path").grid(row=r, column=0, sticky="w")
    ttk.Entry(cb, textvariable=cli_path_var, width=64).grid(row=r, column=1, columnspan=3, sticky="we", padx=(8,0)); r+=1

    ttk.Label(cb, text="Common flags", font=("Segoe UI", 10, "bold")).grid(row=r, column=0, sticky="w", pady=(12,0)); r+=1
    ttk.Label(cb, text="--language LANG").grid(row=r, column=0, sticky="w")
    ttk.Entry(cb, textvariable=cli_lang_var, width=12).grid(row=r, column=1, sticky="w", padx=(8,16))
    ttk.Label(cb, text="Locale for titles/overviews + trailer language preference.", foreground="#555").grid(row=r, column=2, columnspan=2, sticky="w"); r+=1

    ttk.Checkbutton(cb, text="--trailer", variable=cli_trailer_var).grid(row=r, column=0, sticky="w")
    ttk.Label(cb, text="Download the best YouTube trailer.", foreground="#555").grid(row=r, column=1, columnspan=3, sticky="w"); r+=1

    ttk.Checkbutton(cb, text="--dry-run", variable=cli_dryrun_var).grid(row=r, column=0, sticky="w")
    ttk.Label(cb, text="Show actions only; do not move/rename or download.", foreground="#555").grid(row=r, column=1, columnspan=3, sticky="w"); r+=1

    ttk.Checkbutton(cb, text="--verbose", variable=cli_verbose_var).grid(row=r, column=0, sticky="w")
    ttk.Label(cb, text="More logging.", foreground="#555").grid(row=r, column=1, columnspan=3, sticky="w"); r+=1

    ttk.Checkbutton(cb, text="--pause", variable=cli_pause_var).grid(row=r, column=0, sticky="w")
    ttk.Label(cb, text="Wait for Enter before exit.", foreground="#555").grid(row=r, column=1, columnspan=3, sticky="w"); r+=1

    ttk.Label(cb, text="--pause-seconds N").grid(row=r, column=0, sticky="w")
    ttk.Entry(cb, textvariable=cli_pause_sec_var, width=8).grid(row=r, column=1, sticky="w", padx=(8,16))
    ttk.Label(cb, text="Sleep N seconds before exit.", foreground="#555").grid(row=r, column=2, columnspan=2, sticky="w"); r+=1

    ttk.Label(cb, text="Movies-only", font=("Segoe UI", 10, "bold")).grid(row=r, column=0, sticky="w", pady=(12,0)); r+=1
    ttk.Checkbutton(cb, text="--no-cover", variable=cli_movies_no_cover_var).grid(row=r, column=0, sticky="w")
    ttk.Checkbutton(cb, text="--no-clean", variable=cli_movies_no_clean_var).grid(row=r, column=1, sticky="w")
    ttk.Checkbutton(cb, text="--no-prune", variable=cli_movies_no_prune_var).grid(row=r, column=2, sticky="w"); r+=1
    ttk.Label(cb, text='--format "FMT"').grid(row=r, column=0, sticky="w")
    ttk.Entry(cb, textvariable=cli_movie_fmt_var).grid(row=r, column=1, columnspan=3, sticky="we", padx=(8,0)); r+=1
    ttk.Label(cb, text="Default: {ny}/{ny}    Placeholders: {n} {y} {ny}", foreground="#555").grid(row=r, column=0, columnspan=4, sticky="w"); r+=1

    ttk.Label(cb, text="Series-only", font=("Segoe UI", 10, "bold")).grid(row=r, column=0, sticky="w", pady=(12,0)); r+=1
    ttk.Label(cb, text="--layout {flat|folders}").grid(row=r, column=0, sticky="w")
    ttk.Combobox(cb, textvariable=cli_series_layout_var, values=["flat", "folders"], width=10, state="readonly").grid(row=r, column=1, sticky="w", padx=(8,16))
    ttk.Checkbutton(cb, text="--cover", variable=cli_series_cover_var).grid(row=r, column=2, sticky="w"); r+=1
    ttk.Checkbutton(cb, text="--season-posters", variable=cli_series_season_cover_var).grid(row=r, column=0, sticky="w")
    ttk.Label(cb, text="Download per-season poster(s).", foreground="#555").grid(row=r, column=1, columnspan=3, sticky="w"); r+=1
    ttk.Checkbutton(cb, text="--no-clean", variable=cli_series_no_clean_var).grid(row=r, column=0, sticky="w")
    ttk.Checkbutton(cb, text="--no-prune", variable=cli_series_no_prune_var).grid(row=r, column=1, sticky="w"); r+=1
    ttk.Label(cb, text='--force-show "NAME"').grid(row=r, column=0, sticky="w")
    ttk.Entry(cb, textvariable=cli_series_force_name_var).grid(row=r, column=1, columnspan=3, sticky="we", padx=(8,0)); r+=1
    ttk.Label(cb, text='--force-year YYYY').grid(row=r, column=0, sticky="w")
    ttk.Entry(cb, textvariable=cli_series_force_year_var, width=10).grid(row=r, column=1, sticky="w", padx=(8,16))
    ttk.Checkbutton(cb, text="--debug-match", variable=cli_series_debug_match_var).grid(row=r, column=2, sticky="w"); r+=1
    ttk.Label(cb, text='--format "FMT"').grid(row=r, column=0, sticky="w")
    ttk.Entry(cb, textvariable=cli_series_fmt_var).grid(row=r, column=1, columnspan=3, sticky="we", padx=(8,0)); r+=1
    ttk.Label(cb, text="Defaults: flat={n} ({y}) - {s00e00} - {t}   folders={ny}/{ny} - Season {s}/{ny} - {s00e00} - {t}", foreground="#555").grid(row=r, column=0, columnspan=4, sticky="w"); r+=1

    ttk.Label(cb, text="Generated command", font=("Segoe UI", 10, "bold")).grid(row=r, column=0, sticky="w", pady=(16,0)); r+=1
    cli_preview = ttk.Entry(cb)
    cli_preview.grid(row=r, column=0, columnspan=3, sticky="we")
    ttk.Button(cb, text="Copy", command=lambda: (win.clipboard_clear(), win.clipboard_append(cli_preview.get()))).grid(row=r, column=3, sticky="e", padx=(8,0)); r+=1

    def build_cli_from_builder() -> str:
        cmd = [sys.executable, "movie_tools.py"]
        path_raw = cli_path_var.get().strip()
        path_q = shell_quote(path_raw if path_raw else target_var.get())

        if cli_mode_var.get() == 0:
            cmd += ["rename", path_q]
            if cli_lang_var.get().strip():
                cmd += ["--language", cli_lang_var.get().strip()]
            if cli_movies_no_cover_var.get():
                cmd.append("--no-cover")
            if cli_movies_no_clean_var.get():
                cmd.append("--no-clean")
            if cli_movies_no_prune_var.get():
                cmd.append("--no-prune")
            if cli_trailer_var.get():
                cmd.append("--trailer")
            if cli_dryrun_var.get():
                cmd.append("--dry-run")
            if cli_verbose_var.get():
                cmd.append("--verbose")
            fmt = cli_movie_fmt_var.get().strip()
            if fmt and fmt != DEFAULT_MOVIE_FMT:
                cmd += ["--format", shell_quote(fmt)]
            if cli_pause_var.get():
                cmd.append("--pause")
            try:
                ps = int(cli_pause_sec_var.get().strip() or "0")
                if ps > 0:
                    cmd += ["--pause-seconds", str(ps)]
            except Exception:
                pass
        else:
            cmd += ["series", path_q]
            if cli_lang_var.get().strip():
                cmd += ["--language", cli_lang_var.get().strip()]
            if cli_series_layout_var.get() == "folders":
                cmd += ["--layout", "folders"]
            if cli_series_cover_var.get():
                cmd.append("--cover")
            if cli_series_season_cover_var.get():
                cmd.append("--season-posters")
            if cli_series_no_clean_var.get():
                cmd.append("--no-clean")
            if cli_series_no_prune_var.get():
                cmd.append("--no-prune")
            if cli_trailer_var.get():
                cmd.append("--trailer")
            if cli_dryrun_var.get():
                cmd.append("--dry-run")
            if cli_verbose_var.get():
                cmd.append("--verbose")
            if cli_series_force_name_var.get().strip():
                cmd += ["--force-show", shell_quote(cli_series_force_name_var.get().strip())]
            if cli_series_force_year_var.get().strip():
                cmd += ["--force-year", cli_series_force_year_var.get().strip()]
            if cli_series_debug_match_var.get():
                cmd.append("--debug-match")
            fmt = cli_series_fmt_var.get().strip()
            expected_default = DEFAULT_SERIES_FLAT if cli_series_layout_var.get() == "flat" else DEFAULT_SERIES_FOLDERS
            if fmt and fmt != expected_default:
                cmd += ["--format", shell_quote(fmt)]
            if cli_pause_var.get():
                cmd.append("--pause")
            try:
                ps = int(cli_pause_sec_var.get().strip() or "0")
                if ps > 0:
                    cmd += ["--pause-seconds", str(ps)]
            except Exception:
                pass

        out = " ".join(cmd)
        cli_preview.delete(0, "end")
        cli_preview.insert(0, out)
        return out

    # Builder state
    cli_mode_var.set(0 if mode_var.get()==0 else 1)
    cli_lang_var.set(language_var.get())

    def sync_builder_from_options():
        cli_mode_var.set(mode_var.get())
        cli_lang_var.set(language_var.get())
        cli_movie_fmt_var.set(movie_fmt_var.get())
        cli_series_fmt_var.set(series_fmt_var.get())
        cli_path_var.set(target_var.get())
        if mode_var.get() == 0:
            cli_movies_no_cover_var.set(0 if cover_var.get() else 1)
        else:
            cli_series_cover_var.set(1 if cover_var.get() else 0)
            cli_series_season_cover_var.set(1 if season_cover_var.get() else 0)
        cli_trailer_var.set(trailer_var.get())
        cli_dryrun_var.set(dry_run_var.get())
        cli_series_layout_var.set("folders" if layout_var.get() == 1 else "flat")
        cli_cmd_var.set(build_cli_from_builder())
        build_cli_from_builder()

    def set_series_defaults():
        if layout_cb.get() == "flat":
            series_fmt_var.set(DEFAULT_SERIES_FLAT)
        else:
            series_fmt_var.set(DEFAULT_SERIES_FOLDERS)

    def refresh_info():
        nonlocal tmdb_movie, tmdb_tv, info_trailer_url
        # We try to use ENV/config silently; if missing, only prompt on Run/explicit Refresh
        key = (os.getenv("TMDB_API_KEY") or load_api_key_from_config() or "").strip()
        if not key:
            # Do not block UI; just clear info
            info_title_var.set("TMDB key not set yet (will prompt on Run/Refresh).")
            info_year_var.set("")
            info_overview.configure(state="normal"); info_overview.delete("1.0", "end"); info_overview.insert("1.0", "")
            info_overview.configure(state="disabled")
            trailer_link_var.set("")
            if poster_lbl: poster_lbl.configure(image="")
            return
        tmdb_movie = TMDB(api_key=key, language=language_var.get().strip() or "en-US")
        tmdb_tv = TMDBTV(api_key=key, language=language_var.get().strip() or "en-US")
        tpath = Path(target_var.get())
        if tpath.is_dir():
            vid = first_video_under(tpath)
            if vid: tpath = vid
        info = get_best_media_info(tpath, tmdb_movie, tmdb_tv)
        title = info.get("title") or ""
        year = info.get("year") or ""
        info_title_var.set(title)
        info_year_var.set(str(year) if year else "")
        info_overview.configure(state="normal"); info_overview.delete("1.0", "end"); info_overview.insert("1.0", info.get("overview") or "")
        info_overview.configure(state="disabled")
        poster_url = info.get("poster_url")
        if have_pillow and poster_url:
            try:
                with requests.get(poster_url, timeout=15) as r:
                    r.raise_for_status()
                    from PIL import Image, ImageTk  # type: ignore
                    im = Image.open(BytesIO(r.content))
                    im.thumbnail((360, 540))
                    poster = ImageTk.PhotoImage(im)
                    poster_img_obj["img"] = poster
                    poster_lbl.configure(image=poster)
            except Exception:
                poster_img_obj["img"] = None
                if poster_lbl: poster_lbl.configure(image="")
        info_trailer_url = info.get("trailer_url")
        trailer_link_var.set(info_trailer_url or "")

    ttk.Button(left, text="Refresh info", command=lambda: (ensure_api_key(None), refresh_info())).pack(anchor="w", pady=(8,0))

    # Init + traces
    def init_layout_from_combo():
        layout_var.set(0 if layout_cb.get()=="flat" else 1)
    layout_cb.bind("<<ComboboxSelected>>", lambda *_: init_layout_from_combo())
    init_layout_from_combo()
    sync_builder_from_options()
    # Try to load info silently if key is already configured
    if os.getenv("TMDB_API_KEY") or load_api_key_from_config():
        refresh_info()

    # Buttons (bottom action row)
    btns = ttk.Frame(win); btns.pack(fill="x", pady=10)
    from tkinter import messagebox
    def submit_and_close():
        # Ensure we have a key (this may prompt once)
        api_key = ensure_api_key(None)
        # Build result
        result = {
            "mode": "movies" if mode_var.get() == 0 else "series",
            "cover": bool(cover_var.get()),
            "season_cover": bool(season_cover_var.get()),
            "trailer": bool(trailer_var.get()),
            "clean": bool(clean_var.get()),
            "prune": bool(prune_var.get()),
            "dry_run": bool(dry_run_var.get()),
            "layout": "flat" if layout_var.get() == 0 else "folders",
            "movie_format": movie_fmt_var.get().strip(),
            "series_format": series_fmt_var.get().strip(),
            "language": language_var.get().strip() or "en-US",
            "cli": cli_cmd_var.get(),
            "target": target_var.get(),
        }
        win.result = result
        win.destroy()

    ttk.Button(btns, text="Cancel", command=lambda: (setattr(win, "result", None), win.destroy())).pack(side="right", padx=6)
    ttk.Button(btns, text="Run", command=submit_and_close).pack(side="right")

    win.mainloop()
    return getattr(win, "result", None)

# ---------------- CLI (Movies + Series) ----------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Movie Tools: rename movies & series (TMDB), posters, and trailers.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("rename", help="Movies: format+poster+clean+prune.")
    pr.add_argument("path", nargs="?", help="File or folder to process.")
    pr.add_argument("--language", default="en-US")
    pr.add_argument("--no-cover", action="store_true")
    pr.add_argument("--no-clean", action="store_true")
    pr.add_argument("--no-prune", action="store_true")
    pr.add_argument("--trailer", dest="trailer", action="store_true", help="Download best trailer (uses yt-dlp or ).")
    pr.add_argument("--download-trailer", dest="trailer", action="store_true", help=argparse.SUPPRESS)  # legacy alias
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--verbose", action="store_true")
    pr.add_argument("--format", help="Output path format (no extension). Default: {ny}/{ny}")
    pr.add_argument("--pause", action="store_true", help="Wait for Enter before exiting.")
    pr.add_argument("--pause-seconds", type=int, default=0, help="Sleep N seconds before exiting.")

    ps = sub.add_parser("series", help="TV: flat or folder layouts; optional poster; clean+prune.")
    ps.add_argument("path", nargs="?", help="File or folder to process.")
    ps.add_argument("--language", default="en-US")
    ps.add_argument("--layout", choices=["flat", "folders"], default="flat",
                    help="flat: {n} ({y}) - {s00e00} - {t}; folders: {ny}/{ny} - Season {s}/{ny} - {s00e00} - {t}")
    ps.add_argument("--cover", action="store_true", help="Download series poster into target folder(s).")
    ps.add_argument("--season-posters", action="store_true", help="Download per-season poster(s).")
    ps.add_argument("--no-clean", action="store_true")
    ps.add_argument("--no-prune", action="store_true")
    ps.add_argument("--trailer", dest="trailer", action="store_true", help="Download best trailer (uses yt-dlp or ).")
    ps.add_argument("--download-trailer", dest="trailer", action="store_true", help=argparse.SUPPRESS)
    ps.add_argument("--dry-run", action="store_true")
    ps.add_argument("--verbose", action="store_true")
    ps.add_argument("--force-show", help="Override show name for matching (e.g., 'The Office').")
    ps.add_argument("--force-year", type=int, help="Override show year (first air year).")
    ps.add_argument("--debug-match", action="store_true", help="Print TV matching candidates and TMDB top results.")
    ps.add_argument("--format", help="Output path format (no extension). Defaults: flat='{n} ({y}) - {s00e00} - {t}', folders='{ny}/{ny} - Season {s}/{ny} - {s00e00} - {t}'")
    ps.add_argument("--pause", action="store_true", help="Wait for Enter before exiting.")
    ps.add_argument("--pause-seconds", type=int, default=0, help="Sleep N seconds before exiting.")

    return p

# ---------------- Auto flow (after pick) ----------------
def auto_run_on(target: Path):
    setup_logging(verbose=True)
    opts = gui_options_dialog(target)
    if not opts:
        return

    mode = opts.get("mode", "movies")
    do_cover = bool(opts.get("cover", True))
    do_season_cover = bool(opts.get("season_cover", False))
    want_trailer = bool(opts.get("trailer", False))
    do_clean = bool(opts.get("clean", True))
    do_prune = bool(opts.get("prune", True))
    dry_run = bool(opts.get("dry_run", False))
    layout = opts.get("layout", "flat")
    movie_fmt = opts.get("movie_format") or "{ny}/{ny}"
    series_fmt = opts.get("series_format") or ("{n} ({y}) - {s00e00} - {t}" if layout == "flat" else "{ny}/{ny} - Season {s}/{ny} - {s00e00} - {t}")
    language = opts.get("language", "en-US") or "en-US"
    target = Path(opts.get("target") or target)

    globals()["CLI_MOVIE_FMT"] = movie_fmt if mode == "movies" else None
    globals()["CLI_SERIES_FMT"] = series_fmt if mode == "series" else None

    api_key = ensure_api_key(None)

    if mode == "movies":
        tmdb = TMDB(api_key=api_key, language=language)
        logging.info(f"[Auto] Movies — Processing: {target}")
        logging.info(f"[Auto] Format: {movie_fmt}")
        handle_root(
            root=target, tmdb=tmdb,
            do_cover=do_cover, do_clean=do_clean, do_prune=do_prune,
            dry_run=dry_run, want_trailer=want_trailer
        )
        logging.info("[Auto] Done.")
    else:
        tmdbtv = TMDBTV(api_key=api_key, language=language)
        logging.info(f"[Auto] Series — Processing: {target}  (layout={layout})")
        logging.info(f"[Auto] Format: {series_fmt}")
        handle_series_root(
            root=target, tmdbtv=tmdbtv, layout=layout,
            do_cover=do_cover, do_season_covers=do_season_cover,
            do_clean=do_clean, do_prune=do_prune,
            dry_run=dry_run, want_trailer=want_trailer
        )
        logging.info("[Auto] Done.")

# ---------------- Main ----------------
def main():
    if len(sys.argv) == 1:
        picked = pick_file_or_folder()
        if not picked:
            return 0
        auto_run_on(picked)
        return 0

    parser = build_parser()
    args = parser.parse_args()
    setup_logging(verbose=getattr(args, "verbose", False))

    globals()["CLI_MOVIE_FMT"]  = getattr(args, "format", None) if args.cmd == "rename" else None
    globals()["CLI_SERIES_FMT"] = getattr(args, "format", None) if args.cmd == "series" else None

    if args.cmd == "rename":
        api_key = ensure_api_key(None)
        tmdb = TMDB(api_key=api_key, language=getattr(args, "language", "en-US"))
        target = Path(args.path).expanduser().resolve() if args.path else Path.cwd()
        handle_root(
            root=target,
            tmdb=tmdb,
            do_cover=not args.no_cover,
            do_clean=not args.no_clean,
            do_prune=not args.no_prune,
            dry_run=args.dry_run,
            want_trailer=bool(getattr(args, "trailer", False)),
        )
        _do_pause(args)
        return 0

    if args.cmd == "series":
        globals()["CLI_FORCE_SHOW"]  = getattr(args, "force_show", None)
        globals()["CLI_FORCE_YEAR"]  = getattr(args, "force_year", None)
        globals()["CLI_DEBUG_MATCH"] = getattr(args, "debug_match", False)

        api_key = ensure_api_key(None)
        tmdbtv = TMDBTV(api_key=api_key, language=getattr(args, "language", "en-US"))
        target = Path(args.path).expanduser().resolve() if args.path else Path.cwd()
        handle_series_root(
            root=target,
            tmdbtv=tmdbtv,
            layout=args.layout,
            do_cover=args.cover,
            do_season_covers=args.season_posters,
            do_clean=not args.no_clean,
            do_prune=not args.no_prune,
            dry_run=args.dry_run,
            want_trailer=bool(getattr(args, "trailer", False)),
        )
        _do_pause(args)
        return 0

    parser.print_help()
    return 2

if __name__ == "__main__":
    sys.exit(main())
