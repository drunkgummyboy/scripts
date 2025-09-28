#!/usr/bin/env python3
"""
movie_tools.py — Right-click friendly movie/series tools (Info pane + Trailers)
- GUI picker (double-click) or CLI commands
- Instant TMDB lookup on chosen file/folder: title, year, synopsis, poster, trailer button
- Movies: rename via format (default "{ny}/{ny}") + poster + clean + prune
- Series (TMDB TV):
    flat default:    "{n} ({y}) - {s00e00} - {t}"
    folders default: "{ny}/{ny} - Season {s}/{ny} - {s00e00} - {t}" (+ optional poster)
    optional: Season posters
- GUI:
    • Reselect target
    • Movies or Series
    • Poster / Season posters / Trailer download / Clean / Prune / Dry-run
    • Series layout (flat/folders)
    • Custom Movie + Series formats
    • Clickable placeholders that insert into the focused format field
    • Live CLI preview + "Copy" button
    • Clear "Info" section with poster + year + synopsis + "Watch trailer" + "Download trailer"
- CLI:
    • Same options via arguments
    • --format supported on both "rename" and "series"
    • --download-trailer on both subcommands

Requirements:
  pip install requests
  (optional) platformdirs
  (optional) Pillow   (auto-installed for GUI poster preview if missing)
  (optional) yt-dlp   (auto-installed for trailer downloads if missing)
"""

import os
import re
import sys
import json
import shutil
import argparse
import logging
import time
import webbrowser
import subprocess
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

import requests

# ---------------- GUI only (picker, API key, options) ----------------
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

def pick_file_or_folder() -> Optional[Path]:
    """Show a tiny chooser with two buttons: File or Folder. Returns Path or None."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None

    chosen: dict = {"path": None}

    def choose_file():
        p = filedialog.askopenfilename(title="Select a video file")
        chosen["path"] = Path(p) if p else None
        win.destroy()

    def choose_folder():
        p = filedialog.askdirectory(title="Select a folder")
        chosen["path"] = Path(p) if p else None
        win.destroy()

    win = tk.Tk()
    win.title("Movie Tools — Pick target")
    win.geometry("300x140")
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
    """Basic cross-platform quoting for CLI preview."""
    if os.name == "nt":
        # cmd.exe rules: double inner quotes when wrapping in quotes
        return '"' + p.replace('"', '""') + '"'
    else:
        # POSIX: single-quote and escape embedded single quotes
        return "'" + p.replace("'", "'\\''") + "'"

def ensure_pillow_installed() -> bool:
    """Ensure Pillow is available so we can show posters in GUI."""
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "Pillow"])
            import PIL  # noqa: F401
            return True
        except Exception:
            return False

def ensure_yt_dlp_installed() -> bool:
    """Ensure yt-dlp is available (we run it as a module to avoid PATH issues)."""
    try:
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "yt-dlp"])
            import yt_dlp  # noqa: F401
            return True
        except Exception:
            return False
def download_trailer_with_ytdlp(url: str, out_dir: Path, dry_run: bool = False) -> bool:
    """
    Delegate to external trailer_dl.py which runs the exact yt-dlp command.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        logging.info(f"  ↳ trailer: {url}\n      → {out_dir}")
        if dry_run:
            return True
        script = Path(__file__).with_name("trailer_dl.py")
        if not script.exists():
            logging.warning(f"  ! Missing helper script: {script}. Create trailer_dl.py as provided.")
            return False
        res = subprocess.run([sys.executable, str(script), url, str(out_dir)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if res.returncode != 0:
            logging.warning("  ! yt-dlp failed via trailer_dl.py:\n" + res.stdout)
            return False
        return True
    except Exception as e:
        logging.warning(f"  ! trailer download error: {e}")
        return False

        
        
        
        
        
        
def guess_title_year_from_path(p: Path) -> Tuple[str, Optional[int]]:
    if p.is_file():
        stem = p.stem
    else:
        stem = p.name
    t, y = split_stem_year(stem)
    return t, y

def gui_options_dialog(target_path: Path, tmdb_for_lookup: Optional["TMDB"] = None) -> Optional[Dict[str, Any]]:
    """
    Returns dict with:
      target_path: Path (may change due to Reselect)
      mode: 'movies'|'series'
      cover: bool
      season_covers: bool
      download_trailer: bool
      clean: bool
      prune: bool
      dry_run: bool
      layout: 'flat'|'folders'
      movie_format: str
      series_format: str
      cli: str
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return None

    result: Dict[str, Any] = {}

    DEFAULT_MOVIE_FMT = "{ny}/{ny}"
    DEFAULT_SERIES_FLAT = "{n} ({y}) - {s00e00} - {t}"
    DEFAULT_SERIES_FOLDERS = "{ny}/{ny} - Season {s}/{ny} - {s00e00} - {t}"
    PLACEHOLDERS = ["{n}", "{y}", "{ny}", "{s}", "{e}", "{s00e00}", "{t}"]

    win = tk.Tk()
    win.title("Movie Tools — Options")
    win.geometry("860x740")
    try:
        win.attributes("-topmost", True)
        win.after(300, lambda: win.attributes("-topmost", False))
    except Exception:
        pass

    frm = ttk.Frame(win, padding=12)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    # --- variables
    mode_var = tk.IntVar(value=0)  # 0=movies, 1=series
    cover_var = tk.IntVar(value=1)
    season_covers_var = tk.IntVar(value=0)
    trailer_dl_var = tk.IntVar(value=0)
    clean_var = tk.IntVar(value=1)
    prune_var = tk.IntVar(value=1)
    dry_run_var = tk.IntVar(value=0)
    layout_var = tk.IntVar(value=0)  # 0=flat, 1=folders
    movie_fmt_var = tk.StringVar(value=DEFAULT_MOVIE_FMT)
    series_fmt_var = tk.StringVar(value=DEFAULT_SERIES_FLAT)
    cli_preview_var = tk.StringVar(value="")
    current_target = {"path": target_path}
    poster_img_tk = {"img": None}
    has_pillow = ensure_pillow_installed()
    info_title_var = tk.StringVar(value="")
    info_year_var  = tk.StringVar(value="")
    info_overview  = tk.StringVar(value="")
    trailer_url    = {"url": None}

    # --- helpers
    def current_series_default():
        return DEFAULT_SERIES_FLAT if layout_var.get() == 0 else DEFAULT_SERIES_FOLDERS

    def on_layout_change(*_):
        cur = series_fmt_var.get().strip()
        if cur in (DEFAULT_SERIES_FLAT, DEFAULT_SERIES_FOLDERS):
            series_fmt_var.set(current_series_default())
        update_cli_preview()

    def on_mode_change(*_):
        is_series = (mode_var.get() == 1)
        layout_flat_rb.configure(state=("normal" if is_series else "disabled"))
        layout_folders_rb.configure(state=("normal" if is_series else "disabled"))
        series_fmt_entry.configure(state=("normal" if is_series else "disabled"))
        movie_fmt_entry.configure(state=("disabled" if is_series else "normal"))
        season_covers_chk.configure(state=("normal" if is_series else "disabled"))
        update_cli_preview()

    def insert_placeholder(token: str):
        focus = win.focus_get()
        target_entry = None
        if focus in (movie_fmt_entry, series_fmt_entry):
            target_entry = focus
        else:
            target_entry = movie_fmt_entry if mode_var.get() == 0 else series_fmt_entry
        try:
            target_entry.insert("insert", token)
        except Exception:
            var = movie_fmt_var if target_entry is movie_fmt_entry else series_fmt_var
            var.set((var.get() or "") + token)
        update_cli_preview()

    def build_cli_command() -> str:
        script = "movie_tools.py"
        path_q = shell_quote(str(current_target["path"]))
        if mode_var.get() == 0:
            # movies
            args = [sys.executable, script, "rename", path_q]
            if not cover_var.get():
                args.append("--no-cover")
            if trailer_dl_var.get():
                args.append("--download-trailer")
            if not clean_var.get():
                args.append("--no-clean")
            if not prune_var.get():
                args.append("--no-prune")
            if dry_run_var.get():
                args.append("--dry-run")
            mvfmt = movie_fmt_var.get().strip()
            if mvfmt and mvfmt != DEFAULT_MOVIE_FMT:
                args += ["--format", shell_quote(mvfmt)]
            return " ".join(args)
        else:
            # series
            args = [sys.executable, script, "series", path_q]
            if layout_var.get() == 1:
                args += ["--layout", "folders"]
            if cover_var.get():
                args.append("--cover")
            if season_covers_var.get():
                args.append("--season-covers")
            if trailer_dl_var.get():
                args.append("--download-trailer")
            if not clean_var.get():
                args.append("--no-clean")
            if not prune_var.get():
                args.append("--no-prune")
            if dry_run_var.get():
                args.append("--dry-run")
            svfmt = series_fmt_var.get().strip()
            expected_default = current_series_default()
            if svfmt and svfmt != expected_default:
                args += ["--format", shell_quote(svfmt)]
            return " ".join(args)

    def update_cli_preview(*_):
        cli_preview_var.set(build_cli_command())

    def copy_cli():
        cmd = cli_preview_var.get()
        try:
            win.clipboard_clear()
            win.clipboard_append(cmd)
        except Exception:
            pass

    def load_poster_into(label_widget, url: Optional[str]):
        if not has_pillow or not label_widget or not url:
            return
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            from PIL import Image, ImageTk  # type: ignore
            from io import BytesIO
            img = Image.open(BytesIO(r.content))
            img.thumbnail((200, 300))
            poster_img_tk["img"] = ImageTk.PhotoImage(img)
            label_widget.configure(image=poster_img_tk["img"])
        except Exception:
            pass

    def open_trailer():
        if trailer_url["url"]:
            webbrowser.open(trailer_url["url"])

    def download_trailer_gui():
        if not trailer_url["url"]:
            return
        target = current_target["path"]
        out_dir = target.parent if target.is_file() else target
        ok = download_trailer_with_ytdlp(trailer_url["url"], out_dir=out_dir, dry_run=False)
        try:
            from tkinter import messagebox
            if ok:
                messagebox.showinfo("Trailer", f"Trailer saved in:\n{out_dir}")
            else:
                messagebox.showwarning("Trailer", "Trailer download failed. See log for details.")
        except Exception:
            pass

    def do_lookup_and_fill():
        target = current_target["path"]
        if not (tmdb_for_lookup and target):
            return
        t, y = guess_title_year_from_path(target)
        info_title_var.set(t or "")
        info_year_var.set(f"{y or ''}")
        info_overview.set("")
        trailer_url["url"] = None

        # Try movie
        try:
            results = list(tmdb_for_lookup.search_movie_cached(t, y)) if hasattr(tmdb_for_lookup, "search_movie_cached") else tmdb_for_lookup.search_movie(t, y)
        except Exception:
            results = []
        mv = choose_best_match(results, t, y) if results else None

        if mv:
            title = mv.get("title") or mv.get("original_title") or t
            rd = mv.get("release_date") or ""
            yr = int(rd[:4]) if rd[:4].isdigit() else (y or None)
            info_title_var.set(title)
            info_year_var.set(str(yr) if yr else "")
            # details + videos
            try:
                det = tmdb_for_lookup.movie_details(int(mv["id"]))
                info_overview.set((det.get("overview") or "").strip())
            except Exception:
                pass
            try:
                vids = tmdb_for_lookup.movie_videos(int(mv["id"]))
                trailer_url["url"] = best_trailer_url(vids)
            except Exception:
                pass
            load_poster_into(poster_canvas, tmdb_for_lookup.build_poster_url(mv.get("poster_path"), size="w500"))
            return

        # Fallback TV
        try:
            tv_results = list(tmdb_for_lookup.search_tv_cached(t, y)) if hasattr(tmdb_for_lookup, "search_tv_cached") else tmdb_for_lookup.search_tv(t, y)
        except Exception:
            tv_results = []
        show = choose_best_tv(tv_results, t, y) if tv_results else None
        if show:
            name = show.get("name") or show.get("original_name") or t
            fad = show.get("first_air_date") or ""
            yr = int(fad[:4]) if fad[:4].isdigit() else (y or None)
            info_title_var.set(name)
            info_year_var.set(str(yr) if yr else "")
            info_overview.set((show.get("overview") or "").strip())
            try:
                vids = tmdb_for_lookup.tv_videos(int(show["id"]))
                trailer_url["url"] = best_trailer_url(vids)
            except Exception:
                pass
            load_poster_into(poster_canvas, tmdb_for_lookup.build_poster_url(show.get("poster_path"), size="w500"))

    def on_reselect():
        p = pick_file_or_folder()
        if p:
            current_target["path"] = p
            target_lbl_var.set(f"Target: {p}")
            update_cli_preview()
            do_lookup_and_fill()

    def submit():
        result["target_path"] = current_target["path"]
        result["mode"] = "movies" if mode_var.get() == 0 else "series"
        result["cover"] = bool(cover_var.get())
        result["season_covers"] = bool(season_covers_var.get())
        result["download_trailer"] = bool(trailer_dl_var.get())
        result["clean"] = bool(clean_var.get())
        result["prune"] = bool(prune_var.get())
        result["dry_run"] = bool(dry_run_var.get())
        result["layout"] = "flat" if layout_var.get() == 0 else "folders"
        result["movie_format"] = movie_fmt_var.get().strip()
        result["series_format"] = series_fmt_var.get().strip()
        result["cli"] = cli_preview_var.get()
        win.destroy()

    def cancel():
        result.clear()
        win.destroy()

    # --- UI
    row = 0
    target_lbl_var = tk.StringVar(value=f"Target: {target_path}")
    ttk.Label(frm, textvariable=target_lbl_var, foreground="#555").grid(row=row, column=0, sticky="w", pady=(0,6))
    ttk.Button(frm, text="Reselect…", command=on_reselect).grid(row=row, column=1, sticky="e", pady=(0,6))
    row += 1

    # --- INFO SECTION (clearly separated)
    ttk.Separator(frm, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="we", pady=8)
    row += 1
    ttk.Label(frm, text="Info", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w")
    row += 1

    info_frame = ttk.Frame(frm)
    info_frame.grid(row=row, column=0, columnspan=2, sticky="we", pady=(2,8))
    info_frame.columnconfigure(1, weight=1)

    poster_canvas = None
    if has_pillow:
        poster_canvas = ttk.Label(info_frame)
        poster_canvas.grid(row=0, column=0, rowspan=4, sticky="nw", padx=(0,10))

    ttk.Label(info_frame, textvariable=info_title_var, font=("Segoe UI", 11, "bold")).grid(row=0, column=1, sticky="w")
    ttk.Label(info_frame, textvariable=info_year_var, foreground="#777").grid(row=1, column=1, sticky="w")
    overview_lbl = ttk.Label(info_frame, textvariable=info_overview, wraplength=650, justify="left")
    overview_lbl.grid(row=2, column=1, sticky="we", pady=(2,0))
    ttk.Button(info_frame, text="Watch trailer", command=open_trailer).grid(row=3, column=1, sticky="w", pady=(6,0))
    ttk.Button(info_frame, text="Download trailer", command=download_trailer_gui).grid(row=3, column=1, sticky="w", padx=(140,0), pady=(6,0))

    row += 1
    ttk.Separator(frm, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="we", pady=8)
    row += 1

    ttk.Label(frm, text="Mode", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w")
    movies_rb = ttk.Radiobutton(frm, text="Movies", variable=mode_var, value=0, command=on_mode_change)
    series_rb = ttk.Radiobutton(frm, text="Series", variable=mode_var, value=1, command=on_mode_change)
    movies_rb.grid(row=row+1, column=0, sticky="w", pady=(2,0))
    series_rb.grid(row=row+1, column=1, sticky="w", pady=(2,0))
    row += 2

    ttk.Label(frm, text="Attributes", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w", pady=(10,0))
    ttk.Checkbutton(frm, text="Download poster", variable=cover_var, command=update_cli_preview).grid(row=row+1, column=0, sticky="w")
    season_covers_chk = ttk.Checkbutton(frm, text="Season posters (series)", variable=season_covers_var, command=update_cli_preview)
    season_covers_chk.grid(row=row+1, column=1, sticky="w")
    ttk.Checkbutton(frm, text="Download trailer", variable=trailer_dl_var, command=update_cli_preview).grid(row=row+2, column=0, sticky="w")
    ttk.Checkbutton(frm, text="Clean clutter", variable=clean_var, command=update_cli_preview).grid(row=row+3, column=0, sticky="w")
    ttk.Checkbutton(frm, text="Prune empty folders", variable=prune_var, command=update_cli_preview).grid(row=row+3, column=1, sticky="w")
    ttk.Checkbutton(frm, text="Dry run (no changes)", variable=dry_run_var, command=update_cli_preview).grid(row=row+4, column=0, sticky="w")
    row += 5

    # Format helpers
    help_text = "Placeholders: {n} title, {y} year, {ny} title+year, {s} season, {e} episode, {s00e00}, {t} episode title"
    ttk.Label(frm, text="Format (click placeholders to insert)", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w", pady=(12,0), columnspan=2)
    ttk.Label(frm, text=help_text, foreground="#555").grid(row=row+1, column=0, sticky="w", columnspan=2, pady=(0,6))
    row += 2

    chips = ttk.Frame(frm)
    chips.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0,8))
    try:
        style = ttk.Style()
        style.configure("Chip.TButton", padding=(6,2))
    except Exception:
        pass
    for i, ph in enumerate(PLACEHOLDERS):
        ttk.Button(chips, text=ph, style="Chip.TButton", command=lambda t=ph: insert_placeholder(t)).grid(row=0, column=i, padx=(0,6))
    row += 1

    ttk.Label(frm, text="Movie format").grid(row=row, column=0, sticky="w")
    movie_fmt_entry = ttk.Entry(frm, textvariable=movie_fmt_var, width=64)
    movie_fmt_entry.grid(row=row, column=1, sticky="we", padx=(10,0))
    movie_fmt_var.trace_add("write", update_cli_preview)
    row += 1

    ttk.Label(frm, text="Series layout").grid(row=row, column=0, sticky="w", pady=(12,0))
    layout_flat_rb = ttk.Radiobutton(frm, text="Flat", variable=layout_var, value=0, command=on_layout_change)
    layout_folders_rb = ttk.Radiobutton(frm, text="Folders", variable=layout_var, value=1, command=on_layout_change)
    layout_flat_rb.grid(row=row, column=1, sticky="w", pady=(12,0))
    layout_folders_rb.grid(row=row, column=1, sticky="w", padx=(70,0), pady=(12,0))
    row += 1

    ttk.Label(frm, text="Series format").grid(row=row, column=0, sticky="w")
    series_fmt_entry = ttk.Entry(frm, textvariable=series_fmt_var, width=64)
    series_fmt_entry.grid(row=row, column=1, sticky="we", padx=(10,0))
    series_fmt_var.trace_add("write", update_cli_preview)
    row += 1

    ttk.Label(frm, text="CLI command", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w", pady=(12,0))
    cli_row = ttk.Frame(frm)
    cli_row.grid(row=row+1, column=0, columnspan=2, sticky="we")
    cli_entry = ttk.Entry(cli_row, textvariable=cli_preview_var)
    cli_entry.pack(side="left", fill="x", expand=True, padx=(0,8))
    ttk.Button(cli_row, text="Copy", command=copy_cli).pack(side="right")
    row += 2

    btns = ttk.Frame(frm)
    btns.grid(row=row, column=0, columnspan=2, pady=16, sticky="e")
    ttk.Button(btns, text="Cancel", command=cancel).pack(side="right", padx=6)
    ttk.Button(btns, text="Run", command=submit).pack(side="right")

    # Initial state
    on_mode_change()
    update_cli_preview()
    try:
        do_lookup_and_fill()
    except Exception:
        pass

    win.mainloop()
    return result or None

# ---------------- Config (persist API key) ----------------
def config_path() -> Path:
    # Cross-platform user config dir
    try:
        from platformdirs import user_config_dir  # pip install platformdirs
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

def _log_path() -> Path:
    p = config_path().parent / "actions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def log_jsonl(action: str, **fields):
    try:
        rec = {"ts": datetime.utcnow().isoformat(timespec="seconds") + "Z", "action": action, **fields}
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

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

# be conservative with .txt deletions
CLUTTER_FILES = [
    r"(?i)^RARBG.*\.txt$",
    r"(?i)^Sample.*",
    r"(?i)\.nfo$",
    r"(?i)\.sfv$",
    r"(?i)\.nzb$",
    r"(?i)\.torrent$",
    r"(?i)^(readme|thanks|how to|instructions|verify|serial|keygen).*\.(txt)$",
]

WIN_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
RESERVED_WIN_NAMES = {
    "con","prn","aux","nul",*(f"com{i}" for i in range(1,10)),*(f"lpt{i}" for i in range(1,10)),
}

def sanitize_component(name: str) -> str:
    name = WIN_ILLEGAL_RE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    if not name:
        name = "_"
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
    """
    Replace placeholders in fmt using ctx and return a relative Path (no extension).
    Supported keys (if present in ctx): n, y, ny, s, e, s00e00, t
    """
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
    out = re.sub(r"[ ._-]+$", "", out)
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
        self._last_request_ts = 0.0  # light pacing

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        retries = Retry(
            total=5, backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"])
        )
        s.mount("https://", HTTPAdapter(max_retries=retries))
        return s

    def _sleep_if_needed(self):
        # Gentle pacing: at most ~4 req/s (250ms spacing)
        since = time.time() - self._last_request_ts
        if since < 0.25:
            time.sleep(0.25 - since)

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        url = f"https://api.themoviedb.org/3{path}"
        params = {"api_key": self.api_key, "language": self.language, **params}
        self._sleep_if_needed()
        r = self.sess.get(url, params=params, timeout=20)
        if r.status_code == 429:
            try:
                ra = float(r.headers.get("Retry-After", "1"))
            except Exception:
                ra = 1.0
            time.sleep(max(0.5, ra))
            r = self.sess.get(url, params=params, timeout=30)
        r.raise_for_status()
        self._last_request_ts = time.time()
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

    @lru_cache(maxsize=1024)
    def search_movie_cached(self, query: str, year: Optional[int]) -> Tuple[Dict[str, Any], ...]:
        # tuples are cacheable; callers may list() it
        return tuple(self.search_movie(query, year))

    def build_poster_url(self, poster_path: str, size: str = "w500") -> Optional[str]:
        if not poster_path:
            return None
        cfg = self.configuration()
        base = cfg.get("images", {}).get("secure_base_url", "")
        sizes = cfg.get("images", {}).get("poster_sizes", []) or ["w500", "original"]
        target = size if size in sizes else (sizes[-1] if sizes else "w500")
        return f"{base}{target}{poster_path}"

    # details / videos
    def movie_details(self, movie_id: int) -> Dict[str, Any]:
        return self._get(f"/movie/{movie_id}", {})

    def movie_videos(self, movie_id: int) -> List[Dict[str, Any]]:
        data = self._get(f"/movie/{movie_id}/videos", {})
        return data.get("results", [])

# --- TV support (TMDB) ---
class TMDBTV(TMDB):
    def search_tv(self, query: str, year: Optional[int]) -> List[Dict[str, Any]]:
        params = {"query": query, "include_adult": False}
        if year:
            params["first_air_date_year"] = str(year)
        data = self._get("/search/tv", params)
        return data.get("results", [])

    @lru_cache(maxsize=1024)
    def search_tv_cached(self, query: str, year: Optional[int]) -> Tuple[Dict[str, Any], ...]:
        return tuple(self.search_tv(query, year))

    def get_episode(self, tv_id: int, season: int, episode: int) -> Optional[Dict[str, Any]]:
        try:
            return self._get(f"/tv/{tv_id}/season/{season}/episode/{episode}", {})
        except Exception:
            return None

    def tv_videos(self, tv_id: int) -> List[Dict[str, Any]]:
        data = self._get(f"/tv/{tv_id}/videos", {})
        return data.get("results", [])

    def season_details(self, tv_id: int, season: int) -> Dict[str, Any]:
        return self._get(f"/tv/{tv_id}/season/{season}", {})

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
            extra = (p.name[len(src_base.name):-len(p.suffix)] if p.suffix else "").strip()
            new_name = dest_stem.name + extra + p.suffix
            dst = dest_stem.parent / new_name
            logging.info(f"  ↳ sidecar: {p.name} → {dst.name}")
            log_jsonl("sidecar_move", src=str(p), dst=str(dst))
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
                log_jsonl("delete_dir", path=str(p))
                if not dry_run:
                    shutil.rmtree(p, ignore_errors=True)
            continue
        for pat in CLUTTER_FILES:
            if re.search(pat, p.name):
                logging.info(f"  ↳ delete: {p.name}")
                log_jsonl("delete", path=str(p))
                if not dry_run:
                    try:
                        p.unlink(missing_ok=True)
                    except Exception as e:
                        logging.warning(f"  ! delete failed: {e}")
                break

def prune_empty_dirs(root: Path, dry_run: bool):
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        d = Path(dirpath)
        if d == root:
            continue
        try:
            if not any(d.iterdir()):
                logging.info(f"prune: {d}")
                log_jsonl("prune", path=str(d))
                if not dry_run:
                    d.rmdir()
        except Exception:
            pass

# ---------------- Poster download ----------------
def download_poster(tmdb: TMDB, movie: Dict[str, Any], out_dir: Path, dry_run: bool):
    poster_path = movie.get("poster_path")
    if not poster_path:
        return
    url = tmdb.build_poster_url(poster_path, size="w500")
    if not url:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    poster_filename = f"{sanitize_component(out_dir.name)} - poster.jpg"
    target = out_dir / poster_filename
    try:
        logging.info(f"  ↳ cover: {poster_filename}")
        if dry_run:
            return
        with tmdb.sess.get(url, stream=True, timeout=30) as r:
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
        log_jsonl("poster", url=url, path=str(target))
    except Exception as e:
        logging.warning(f"  ! cover download failed: {e}")

def download_season_poster(tmdbtv: "TMDBTV", tv_id: int, season: int, out_dir: Path, dry_run: bool):
    try:
        det = tmdbtv.season_details(tv_id, season)
        poster_path = det.get("poster_path")
        if not poster_path:
            return
        url = tmdbtv.build_poster_url(poster_path, size="w500")
        if not url:
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        poster_filename = f"Season {season:02d} - poster.jpg"
        target = out_dir / poster_filename
        logging.info(f"  ↳ season cover: {poster_filename}")
        if dry_run:
            return
        with tmdbtv.sess.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            if "image" not in (r.headers.get("Content-Type") or ""):
                return
            data = r.content
            if len(data) < 1024:
                return
        with open(target, "wb") as f:
            f.write(data)
        log_jsonl("season_poster", tv_id=tv_id, season=season, path=str(target))
    except Exception:
        pass

def best_trailer_url(video_list: List[Dict[str, Any]]) -> Optional[str]:
    if not video_list:
        return None
    def score(v):
        s = 0
        if (v.get("site") or "").lower() == "youtube": s += 3
        if (v.get("type") or "").lower() == "trailer": s += 2
        if v.get("official"): s += 1
        if (v.get("iso_3166_1") or "").upper() in ("US","GB"): s += 1
        return s
    best = max(video_list, key=score)
    if (best.get("site") or "").lower() == "youtube" and best.get("key"):
        return f"https://www.youtube.com/watch?v={best['key']}"
    if best.get("url"):
        return best["url"]
    return None

def get_movie_trailer_url(tmdb: "TMDB", movie_id: int) -> Optional[str]:
    try:
        vids = tmdb.movie_videos(int(movie_id))
        return best_trailer_url(vids)
    except Exception:
        return None

# ---------------- Renamer core (Movies) ----------------
def process_video(tmdb: TMDB, file_path: Path, dry_run: bool) -> Optional[Tuple[Path, Path, Dict[str, Any]]]:
    stem = file_path.stem
    title_guess, year_guess = split_stem_year(stem)
    logging.info(f"→ {file_path.name}  [guess: '{title_guess}' {year_guess or ''}]")
    try:
        matches = list(tmdb.search_movie_cached(title_guess, year_guess)) if hasattr(tmdb, "search_movie_cached") else tmdb.search_movie(title_guess, year_guess)
    except Exception:
        matches = []
    movie = choose_best_match(matches, title_guess, year_guess)
    if not movie and year_guess:
        try:
            movie = choose_best_match(tmdb.search_movie(title_guess, None), title_guess, None)
        except Exception:
            movie = None
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
    log_jsonl("rename", src=str(file_path), dst=str(dest_path))
    return (file_path, dest_path, movie)

def handle_root(
    root: Path,
    tmdb: TMDB,
    do_cover: bool,
    do_clean: bool,
    do_prune: bool,
    dry_run: bool,
):
    touched_parents = set()
    downloaded_trailer_dirs = set()  # avoid duplicates per folder
    do_trailer = bool(globals().get("CLI_DL_TRAILER", False))

    if root.is_file():
        if root.suffix.lower() in VIDEO_EXTS:
            res = process_video(tmdb, root, dry_run)
            if res:
                old, new, mv = res
                touched_parents.add(old.parent)
                if do_cover and mv:
                    download_poster(tmdb, mv, new.parent, dry_run=dry_run)
                if do_trailer and mv:
                    dest_dir = new.parent
                    key = str(dest_dir)
                    if key not in downloaded_trailer_dirs:
                        url = get_movie_trailer_url(tmdb, mv.get("id"))
                        if url:
                            ok = download_trailer_with_ytdlp(url, dest_dir, dry_run=dry_run)
                            if ok:
                                downloaded_trailer_dirs.add(key)
                                log_jsonl("trailer", url=url, path=str(dest_dir))
        else:
            logging.warning("Not a supported video file.")
    else:
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() not in VIDEO_EXTS:
                    continue
                res = process_video(tmdb, p, dry_run)
                if res:
                    old, new, mv = res
                    touched_parents.add(old.parent)
                    if do_cover and mv:
                        download_poster(tmdb, mv, new.parent, dry_run=dry_run)
                    if do_trailer and mv:
                        dest_dir = new.parent
                        key = str(dest_dir)
                        if key not in downloaded_trailer_dirs:
                            url = get_movie_trailer_url(tmdb, mv.get("id"))
                            if url:
                                ok = download_trailer_with_ytdlp(url, dest_dir, dry_run=dry_run)
                                if ok:
                                    downloaded_trailer_dirs.add(key)
                                    log_jsonl("trailer", url=url, path=str(dest_dir))

    if do_clean:
        for folder in sorted(touched_parents):
            logging.info(f"clean: {folder}")
            clean_clutter(folder, dry_run)
    if do_prune:
        prune_empty_dirs(root if root.is_dir() else root.parent, dry_run)

# ---------------- Series (TV) ----------------
def s00e00(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"

def _safe_int(txt: Optional[str]) -> Optional[int]:
    return int(txt) if txt and str(txt).isdigit() else None

def normalize_show_hint(txt: str) -> str:
    """Clean noisy folder/file names into a decent show title hint."""
    s = txt
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"[\(\[][^)\]]{0,12}[\)\]]", " ", s)
    junk = r"\b(1080p|2160p|720p|4k|webrip|web[- ]?dl|bluray|b[dr]rip|hdtv|x26[45]|h26[45]|hevc|av1|hdr10?|dv|sdr|multi|dubbed|subbed)\b"
    s = re.sub(junk, " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def season_folder_parent(file_path: Path) -> Optional[Path]:
    p = file_path.parent
    if p and re.search(r"(?i)\bseason\b|\bseizoen\b|saison|staffel|temporada", p.name):
        return p.parent
    return None

def _log_debug_match(debug: bool, title: str, candidates: List[Tuple[str, Optional[int]]], results: List[Dict[str, Any]]):
    if not debug:
        return
    print(f"\n[debug] matching for: {title!r}")
    print("[debug] candidates (title, year):")
    for t, y in candidates:
        print(f"         - {t!r}  {y or ''}")
    if results:
        print("[debug] sample TMDB results:")
        for r in results[:5]:
            name = r.get('name') or r.get('original_name')
            fad = (r.get('first_air_date') or '')[:10]
            pop = r.get('popularity')
            print(f"         • {name}  [{fad}]  pop={pop}")

def try_tv_match_with_fallbacks(tmdbtv, file_path: Path, title_guess: str, year_guess: Optional[int],
                                force_show: Optional[str] = None, force_year: Optional[int] = None,
                                debug: bool = False) -> Optional[Dict[str, Any]]:
    # Ordered list of (title, year) candidates
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

    last_results: List[Dict[str, Any]] = []
    for t, y in uniq:
        for qt, qy in [(t, None), (t, y)]:
            try:
                results = list(tmdbtv.search_tv_cached(qt, qy)) if hasattr(tmdbtv, "search_tv_cached") else tmdbtv.search_tv(qt, qy)
            except Exception:
                results = []
            if results and debug and not last_results:
                last_results = results
            show = choose_best_tv(results, qt, qy)
            if show:
                _log_debug_match(debug, t, uniq, last_results)
                return show

    _log_debug_match(debug, "NO MATCH", uniq, last_results)
    return None

def process_series_file(tmdbtv: TMDBTV, file_path: Path, layout: str, do_cover: bool, dry_run: bool) -> Optional[Tuple[Path, Path, Dict[str, Any]]]:
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
    ctx = {
        "n": show_name,
        "y": show_year,
        "ny": ny,
        "s": season,
        "e": episode,
        "s00e00": s00e00(season, episode),
        "t": ep_title,
    }

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
    log_jsonl("rename", src=str(file_path), dst=str(dest))

    if do_cover:
        poster_path = show.get("poster_path")
        if poster_path:
            # flat → same folder as file; folders → Series root
            out_dir = dest.parent if layout == "flat" else dest.parent.parent
            download_poster(tmdbtv, show, out_dir, dry_run=dry_run)

    if do_cover and globals().get("CLI_SEASON_COVERS"):
        try:
            tv_id = int(show["id"])
            # for flat or folders, the Season folder is dest.parent
            out_dir = dest.parent
            download_season_poster(tmdbtv, tv_id, season, out_dir, dry_run=dry_run)
        except Exception:
            pass

    return (file_path, dest, show)

def handle_series_root(root: Path, tmdbtv: TMDBTV, layout: str, do_cover: bool, do_clean: bool, do_prune: bool, dry_run: bool):
    touched = set()
    downloaded_trailer_dirs = set()
    do_trailer = bool(globals().get("CLI_DL_TRAILER", False))

    if root.is_file():
        if root.suffix.lower() in VIDEO_EXTS:
            res = process_series_file(tmdbtv, root, layout, do_cover, dry_run)
            if res:
                old, new, show = res
                touched.add(res[0].parent)
                if do_trailer and show:
                    series_dir = new.parent if layout == "flat" else new.parent.parent
                    key = str(series_dir)
                    if key not in downloaded_trailer_dirs:
                        try:
                            vids = tmdbtv.tv_videos(int(show["id"]))
                            url = best_trailer_url(vids)
                        except Exception:
                            url = None
                        if url:
                            ok = download_trailer_with_ytdlp(url, series_dir, dry_run=dry_run)
                            if ok:
                                downloaded_trailer_dirs.add(key)
                                log_jsonl("trailer", url=url, path=str(series_dir))
        else:
            logging.warning("Not a supported video file.")
    else:
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() not in VIDEO_EXTS:
                    continue
                res = process_series_file(tmdbtv, p, layout, do_cover, dry_run)
                if res:
                    old, new, show = res
                    touched.add(res[0].parent)
                    if do_trailer and show:
                        series_dir = new.parent if layout == "flat" else new.parent.parent
                        key = str(series_dir)
                        if key not in downloaded_trailer_dirs:
                            try:
                                vids = tmdbtv.tv_videos(int(show["id"]))
                                url = best_trailer_url(vids)
                            except Exception:
                                url = None
                            if url:
                                ok = download_trailer_with_ytdlp(url, series_dir, dry_run=dry_run)
                                if ok:
                                    downloaded_trailer_dirs.add(key)
                                    log_jsonl("trailer", url=url, path=str(series_dir))

    if do_clean:
        for folder in sorted(touched):
            logging.info(f"clean: {folder}")
            clean_clutter(folder, dry_run)
    if do_prune:
        prune_empty_dirs(root if root.is_dir() else root.parent, dry_run)

# ---------------- Filename parsing helpers ----------------
def slugify(text: str) -> str:
    text = re.sub(r"[\\/:\*\?\"<>\|]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "Unknown"

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
    title = slugify(cleaned)
    return title, year, s, e

# ---------------- API key helpers ----------------
def validate_api_key(key: str) -> bool:
    try:
        TMDB(api_key=key).configuration()
        return True
    except Exception:
        return False

def ensure_api_key(cli_key: Optional[str]) -> str:
    # Priority: CLI -> ENV -> config -> GUI prompt (only popup here if needed)
    key = (cli_key or os.getenv("TMDB_API_KEY") or load_api_key_from_config() or "").strip()
    if key and validate_api_key(key):
        return key
    key_gui = api_key_popup(prefill=key or "")
    if not key_gui or not validate_api_key(key_gui):
        print("Invalid or missing TMDB API key. Aborting.", file=sys.stderr)
        sys.exit(2)
    save_api_key_to_config(key_gui)
    return key_gui

# ---------------- CLI (Movies + Series) ----------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Movie Tools: rename movies & series (TMDB) and posters.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("rename", help="Movies: format+poster+trailer+clean+prune.")
    pr.add_argument("path", nargs="?", help="File or folder to process.")
    pr.add_argument("--api-key", help="TMDB API key (else ENV/config/prompt).")
    pr.add_argument("--language", default="en-US")
    pr.add_argument("--no-cover", action="store_true")
    pr.add_argument("--download-trailer", action="store_true", help="Download the best trailer into the target folder.")
    pr.add_argument("--no-clean", action="store_true")
    pr.add_argument("--no-prune", action="store_true")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--verbose", action="store_true")
    pr.add_argument("--format", help='Output path format (no extension). Default: {ny}/{ny}')
    pr.add_argument("--pause", action="store_true", help="Wait for Enter before exiting.")
    pr.add_argument("--pause-seconds", type=int, default=0, help="Sleep N seconds before exiting.")

    ps = sub.add_parser("series", help="TV: flat or folder layouts; optional poster; season posters; trailer; clean+prune.")
    ps.add_argument("path", nargs="?", help="File or folder to process.")
    ps.add_argument("--api-key", help="TMDB API key (else ENV/config/prompt).")
    ps.add_argument("--language", default="en-US")
    ps.add_argument("--layout", choices=["flat", "folders"], default="flat",
                    help="flat: {n} ({y}) - {s00e00} - {t}; folders: {ny}/{ny} - Season {s}/{ny} - {s00e00} - {t}")
    ps.add_argument("--cover", action="store_true", help="Download series poster into target folder(s).")
    ps.add_argument("--season-covers", action="store_true", help="Also download season posters into each Season folder.")
    ps.add_argument("--download-trailer", action="store_true", help="Download the best series trailer into the series folder (once).")
    ps.add_argument("--no-clean", action="store_true")
    ps.add_argument("--no-prune", action="store_true")
    ps.add_argument("--dry-run", action="store_true")
    ps.add_argument("--verbose", action="store_true")
    ps.add_argument("--force-show", help="Override show name for matching (e.g., 'The Office').")
    ps.add_argument("--force-year", type=int, help="Override show year (first air year).")
    ps.add_argument("--debug-match", action="store_true", help="Print TV matching candidates and TMDB top results.")
    ps.add_argument("--format", help=("Output path format (no extension). "
                    "Defaults: flat='{n} ({y}) - {s00e00} - {t}', folders='{ny}/{ny} - Season {s}/{ny} - {s00e00} - {t}'"))
    ps.add_argument("--pause", action="store_true", help="Wait for Enter before exiting.")
    ps.add_argument("--pause-seconds", type=int, default=0, help="Sleep N seconds before exiting.")

    return p

# ---------------- Auto flow (after pick) ----------------
def auto_run_on(target: Path):
    setup_logging(verbose=True)

    # build a minimal TMDB client for GUI lookup (non-blocking if key absent/invalid)
    key_hint = (os.getenv("TMDB_API_KEY") or load_api_key_from_config() or "").strip()
    tmdb_lookup = None
    if key_hint:
        try:
            tmdb_lookup = TMDB(api_key=key_hint, language="en-US")
            tmdb_lookup.configuration()  # validate
        except Exception:
            tmdb_lookup = None

    # Gather options via GUI (includes Info pane + Reselect)
    opts = gui_options_dialog(target, tmdb_for_lookup=tmdb_lookup)
    if not opts:
        return

    # Unpack GUI choices
    target = opts.get("target_path", target)
    mode = opts.get("mode", "movies")
    do_cover = bool(opts.get("cover", True))
    do_season_covers = bool(opts.get("season_covers", False))
    do_trailer = bool(opts.get("download_trailer", False))
    do_clean = bool(opts.get("clean", True))
    do_prune = bool(opts.get("prune", True))
    dry_run = bool(opts.get("dry_run", False))
    layout = opts.get("layout", "flat")
    movie_fmt = opts.get("movie_format") or "{ny}/{ny}"
    series_fmt = opts.get("series_format") or ("{n} ({y}) - {s00e00} - {t}" if layout == "flat" else "{ny}/{ny} - Season {s}/{ny} - {s00e00} - {t}")

    # Apply GUI formats via globals the CLI uses
    globals()["CLI_MOVIE_FMT"] = movie_fmt if mode == "movies" else None
    globals()["CLI_SERIES_FMT"] = series_fmt if mode == "series" else None
    globals()["CLI_SEASON_COVERS"] = do_season_covers
    globals()["CLI_DL_TRAILER"] = do_trailer

    api_key = ensure_api_key(None)   # Only popup if missing/invalid
    language = "en-US"               # could be extended to a GUI entry later

    logging.info(f"[Auto] Target: {target}")
    log_jsonl("start", mode=mode, path=str(target))

    if mode == "movies":
        tmdb = TMDB(api_key=api_key, language=language)
        logging.info(f"[Auto] Movies — Processing: {target}")
        logging.info(f"[Auto] Format: {movie_fmt}")
        handle_root(
            root=target, tmdb=tmdb,
            do_cover=do_cover, do_clean=do_clean, do_prune=do_prune, dry_run=dry_run
        )
        logging.info("[Auto] Done.")
    else:
        tmdbtv = TMDBTV(api_key=api_key, language=language)
        logging.info(f"[Auto] Series — Processing: {target}  (layout={layout})")
        logging.info(f"[Auto] Format: {series_fmt}")
        handle_series_root(
            root=target, tmdbtv=tmdbtv, layout=layout,
            do_cover=do_cover, do_clean=do_clean, do_prune=do_prune, dry_run=dry_run
        )
        logging.info("[Auto] Done.")

    log_jsonl("done", mode=mode, path=str(target))

# ---------------- Main ----------------
def main():
    # Double-click (no args): pick a file/folder, then show GUI options dialog + Info pane
    if len(sys.argv) == 1:
        target = pick_file_or_folder()
        if not target:
            return 0  # user canceled silently
        auto_run_on(target)
        return 0

    # CLI
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(verbose=getattr(args, "verbose", False))

    # Capture global-format overrides early (harmless if None)
    globals()["CLI_MOVIE_FMT"]  = getattr(args, "format", None) if args.cmd == "rename" else None
    globals()["CLI_SERIES_FMT"] = getattr(args, "format", None) if args.cmd == "series" else None

    if args.cmd == "rename":
        api_key = ensure_api_key(getattr(args, "api_key", None))
        tmdb = TMDB(api_key=api_key, language=getattr(args, "language", "en-US"))
        target = Path(args.path).expanduser().resolve() if args.path else Path.cwd()
        globals()["CLI_DL_TRAILER"] = bool(getattr(args, "download_trailer", False))
        logging.info(f"[CLI] Movies — Target: {target}")
        log_jsonl("start", mode="movies", path=str(target))
        handle_root(
            root=target,
            tmdb=tmdb,
            do_cover=not args.no_cover,
            do_clean=not args.no_clean,
            do_prune=not args.no_prune,
            dry_run=args.dry_run,
        )
        logging.info("[CLI] Done.")
        log_jsonl("done", mode="movies", path=str(target))
        _do_pause(args)
        return 0

    if args.cmd == "series":
        # expose debug/force flags to the matcher
        globals()["CLI_FORCE_SHOW"]  = getattr(args, "force_show", None)
        globals()["CLI_FORCE_YEAR"]  = getattr(args, "force_year", None)
        globals()["CLI_DEBUG_MATCH"] = getattr(args, "debug_match", False)
        globals()["CLI_SEASON_COVERS"] = bool(getattr(args, "season_covers", False))
        globals()["CLI_DL_TRAILER"] = bool(getattr(args, "download_trailer", False))

        api_key = ensure_api_key(getattr(args, "api_key", None))
        tmdbtv = TMDBTV(api_key=api_key, language=getattr(args, "language", "en-US"))
        target = Path(args.path).expanduser().resolve() if args.path else Path.cwd()
        logging.info(f"[CLI] Series — Target: {target} (layout={args.layout})")
        log_jsonl("start", mode="series", path=str(target))
        handle_series_root(
            root=target,
            tmdbtv=tmdbtv,
            layout=args.layout,
            do_cover=args.cover,
            do_clean=not args.no_clean,
            do_prune=not args.no_prune,
            dry_run=args.dry_run,
        )
        logging.info("[CLI] Done.")
        log_jsonl("done", mode="series", path=str(target))
        _do_pause(args)
        return 0

    parser.print_help()
    return 2

if __name__ == "__main__":
    sys.exit(main())
