"""
Verse Tracker — Star Citizen Playtime & Kills Monitor
PyInstaller-ready single-file build.

Build command (run from the folder containing this file + VerseTracker.ico):
    pyinstaller --onefile --windowed --name "VerseTracker" --icon "VerseTracker.ico" sc_tracker.py

Requirements (install once before building):
    pip install psutil pystray pillow pyinstaller
"""

import multiprocessing
multiprocessing.freeze_support()

import tkinter as tk
from tkinter import font as tkfont
import threading
import time
import json
import os
import sys
import psutil
from datetime import datetime, date
from pathlib import Path
from io import BytesIO

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
PROCESS_NAME  = "StarCitizen.exe"
POLL_INTERVAL = 3
DATA_FILE     = Path(os.path.expanduser("~")) / ".verse_tracker_stats.json"

# ─────────────────────────────────────────────────────────────────────────────
# COLOURS
# ─────────────────────────────────────────────────────────────────────────────
C = {
    "bg_deep":   "#050c14",
    "bg_panel":  "#090f1a",
    "bg_card":   "#0b1421",
    "bg_card2":  "#0d1829",
    "border":    "#1a2d42",
    "border_hi": "#2a4560",
    "gold":      "#c8922a",
    "gold_lt":   "#e8b84b",
    "gold_dim":  "#7a5518",
    "ice":       "#a8cce0",
    "ice_dim":   "#4a7a9b",
    "green":     "#2ecc71",
    "green_dim": "#1a6b3c",
    "red":       "#c0392b",
    "red_dim":   "#7a1a10",
    "text_hi":   "#ffffff",
    "text_mid":  "#a8c8e0",
    "text_lo":   "#c8dce8",
    "mono":      "#d8eaf6",
}

# ─────────────────────────────────────────────────────────────────────────────
# STATS  (time and kills stored completely separately)
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_STATS = {
    # ── time ──
    "total_seconds":    0,
    "week_seconds":     0,
    "month_seconds":    0,
    "year_seconds":     0,
    "sessions":         [],
    "last_week_reset":  None,
    "last_month_reset": None,
    "last_year_reset":  None,
    "today_date":       None,
    "today_seconds":    0,
    # ── kills ──
    "player_kills_total": 0,
    "ship_kills_total":   0,
}

def load_stats():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
            for k, v in DEFAULT_STATS.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return dict(DEFAULT_STATS)

def save_stats(stats):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception:
        pass

def apply_auto_resets(stats):
    now     = datetime.now()
    changed = False

    today_str = date.today().isoformat()
    if stats.get("today_date") != today_str:
        stats["today_seconds"] = 0
        stats["today_date"]    = today_str
        changed = True

    def last_sunday_passed(last_reset_str):
        if not last_reset_str:
            return True
        last = datetime.fromisoformat(last_reset_str)
        dow  = now.weekday()
        days_back = 0 if dow == 6 else (dow + 1)
        last_sun  = now.replace(hour=23, minute=59, second=59, microsecond=0)
        try:
            last_sun = last_sun.replace(day=now.day - days_back)
        except ValueError:
            pass
        return last < last_sun

    if last_sunday_passed(stats.get("last_week_reset")):
        if stats.get("last_week_reset"):
            stats["week_seconds"] = 0
        stats["last_week_reset"] = now.isoformat()
        changed = True

    last_mr = stats.get("last_month_reset")
    if last_mr:
        lm = datetime.fromisoformat(last_mr)
        if lm.month != now.month or lm.year != now.year:
            stats["month_seconds"]    = 0
            stats["last_month_reset"] = now.isoformat()
            changed = True
    else:
        stats["last_month_reset"] = now.isoformat()
        changed = True

    last_yr = stats.get("last_year_reset")
    if last_yr:
        ly = datetime.fromisoformat(last_yr)
        if ly.year != now.year:
            stats["year_seconds"]    = 0
            stats["last_year_reset"] = now.isoformat()
            changed = True
    else:
        stats["last_year_reset"] = now.isoformat()
        changed = True

    return stats, changed

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def fmt_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def fmt_short(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {int(seconds % 60)}s"

def is_game_running():
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and \
               proc.info["name"].lower() == PROCESS_NAME.lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False

def make_tray_icon_image():
    s    = 64
    img  = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, s-2, s-2], fill=(5, 12, 20, 255))
    draw.ellipse([2, 2, s-2, s-2], outline=(42, 69, 96, 255), width=2)
    cx, cy = s//2, s//2
    r  = s * 0.30
    pts = [(cx, cy-r),(cx+r*0.6, cy),(cx, cy+r),(cx-r*0.6, cy)]
    draw.polygon([(cx,cy-r*1.08),(cx+r*0.65,cy),(cx,cy+r*1.08),(cx-r*0.65,cy)],
                 fill=(120, 80, 10, 255))
    draw.polygon(pts, fill=(180, 130, 30, 255))
    draw.polygon([(cx,cy-r),(cx+r*0.6,cy),(cx,cy),(cx-r*0.6,cy)],
                 fill=(232, 184, 60, 255))
    return img

def make_window_icon(root):
    try:
        s    = 32
        img  = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([1,1,s-1,s-1], fill=(5,12,20,255))
        draw.ellipse([1,1,s-1,s-1], outline=(42,69,96,255), width=1)
        cx, cy = s//2, s//2
        r  = s * 0.30
        pts = [(cx,cy-r),(cx+r*0.6,cy),(cx,cy+r),(cx-r*0.6,cy)]
        draw.polygon([(cx,cy-r*1.08),(cx+r*0.65,cy),(cx,cy+r*1.08),(cx-r*0.65,cy)],
                     fill=(120,80,10,255))
        draw.polygon(pts, fill=(180,130,30,255))
        draw.polygon([(cx,cy-r),(cx+r*0.6,cy),(cx,cy),(cx-r*0.6,cy)],
                     fill=(232,184,60,255))
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        from tkinter import PhotoImage
        photo = PhotoImage(data=buf.getvalue())
        root.iconphoto(True, photo)
        root._icon_ref = photo
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# APPLICATION
# ─────────────────────────────────────────────────────────────────────────────
class VerseTracker:
    def __init__(self):
        self.stats = load_stats()
        self.stats, _ = apply_auto_resets(self.stats)
        save_stats(self.stats)

        self.tracking      = False
        self.session_start = None
        self.session_secs  = 0
        self._lock         = threading.Lock()
        self._running      = True

        self._build_ui()
        self._start_threads()
        if TRAY_AVAILABLE:
            self._build_tray()

    # ─────────────────────────────────────────────────────────────────────────
    # UI CONSTRUCTION
    # ─────────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Verse Tracker")
        self.root.configure(bg=C["bg_deep"])
        self.root.resizable(False, False)
        self.root.geometry("620x880")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if TRAY_AVAILABLE:
            make_window_icon(self.root)

        try:
            self.f_mono   = tkfont.Font(family="Consolas",     size=12)
            self.f_mono_l = tkfont.Font(family="Consolas",     size=24, weight="bold")
            self.f_mono_s = tkfont.Font(family="Consolas",     size=11, weight="bold")
            self.f_label  = tkfont.Font(family="Trebuchet MS", size=10, weight="bold")
            self.f_title  = tkfont.Font(family="Trebuchet MS", size=19, weight="bold")
            self.f_sub    = tkfont.Font(family="Trebuchet MS", size=10)
            self.f_btn    = tkfont.Font(family="Trebuchet MS", size=10, weight="bold")
            self.f_num_l  = tkfont.Font(family="Consolas",     size=32, weight="bold")
        except Exception:
            self.f_mono   = tkfont.Font(family="Courier", size=12)
            self.f_mono_l = tkfont.Font(family="Courier", size=24, weight="bold")
            self.f_mono_s = tkfont.Font(family="Courier", size=11, weight="bold")
            self.f_label  = tkfont.Font(family="Arial",   size=10, weight="bold")
            self.f_title  = tkfont.Font(family="Arial",   size=19, weight="bold")
            self.f_sub    = tkfont.Font(family="Arial",   size=10)
            self.f_btn    = tkfont.Font(family="Arial",   size=10, weight="bold")
            self.f_num_l  = tkfont.Font(family="Courier", size=32, weight="bold")

        # ── outer border frame ──
        outer = tk.Frame(self.root, bg=C["border"], padx=1, pady=1)
        outer.pack(fill="both", expand=True, padx=12, pady=12)
        main = tk.Frame(outer, bg=C["bg_deep"])
        main.pack(fill="both", expand=True)

        self._build_header(main)
        self._build_tabs(main)

    # ── Header ────────────────────────────────────────────────────────────────
    def _build_header(self, parent):
        hdr = tk.Frame(parent, bg=C["bg_deep"], pady=16)
        hdr.pack(fill="x", padx=20)
        tk.Label(hdr, text="◆  VERSE TRACKER  ◆",
                 font=self.f_title, bg=C["bg_deep"], fg=C["gold_lt"]).pack()
        tk.Label(hdr, text="STAR CITIZEN  //  SESSION ANALYTICS",
                 font=self.f_sub, bg=C["bg_deep"], fg=C["text_mid"]).pack(pady=(2, 0))
        # Gold line removed as requested

    # ── Tab system ────────────────────────────────────────────────────────────
    def _build_tabs(self, parent):
        # Tab bar
        tab_bar = tk.Frame(parent, bg=C["bg_deep"])
        tab_bar.pack(fill="x", padx=20, pady=(4, 0))

        self._tab_btns  = {}
        self._tab_pages = {}
        self._active_tab = tk.StringVar(value="time")

        for name, label in [("time", "⏱  PLAYTIME"), ("kills", "☠  KILLS")]:
            btn = tk.Button(tab_bar, text=label, font=self.f_btn,
                            bg=C["bg_deep"], relief="flat", bd=0,
                            padx=18, pady=8, cursor="hand2",
                            command=lambda n=name: self._switch_tab(n))
            btn.pack(side="left")
            self._tab_btns[name] = btn

        # Divider under tab bar
        tk.Frame(parent, bg=C["gold_dim"], height=1).pack(fill="x", padx=20)

        # Page container
        container = tk.Frame(parent, bg=C["bg_deep"])
        container.pack(fill="both", expand=True)

        # Build both pages
        time_page  = tk.Frame(container, bg=C["bg_deep"])
        kills_page = tk.Frame(container, bg=C["bg_deep"])
        self._tab_pages["time"]  = time_page
        self._tab_pages["kills"] = kills_page

        self._build_time_page(time_page)
        self._build_kills_page(kills_page)

        self._switch_tab("time")

    def _switch_tab(self, name):
        for n, page in self._tab_pages.items():
            page.pack_forget()
        self._tab_pages[name].pack(fill="both", expand=True)
        self._active_tab.set(name)
        for n, btn in self._tab_btns.items():
            if n == name:
                btn.config(fg=C["gold_lt"],
                           highlightbackground=C["gold_dim"],
                           highlightthickness=1)
            else:
                btn.config(fg=C["text_mid"],
                           highlightbackground=C["bg_deep"],
                           highlightthickness=0)

    # ─────────────────────────────────────────────────────────────────────────
    # PLAYTIME TAB
    # ─────────────────────────────────────────────────────────────────────────
    def _build_time_page(self, parent):
        self._build_status_panel(parent)
        self._div(parent)
        self._build_stats_grid(parent)
        self._div(parent)
        self._build_manual_stats(parent)
        self._div(parent)
        self._build_time_footer(parent)

    def _div(self, parent):
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", padx=20, pady=4)

    def _build_status_panel(self, parent):
        pnl   = tk.Frame(parent, bg=C["bg_panel"],
                         highlightbackground=C["border"], highlightthickness=1)
        pnl.pack(fill="x", padx=20, pady=(10, 12))
        inner = tk.Frame(pnl, bg=C["bg_panel"], padx=18, pady=14)
        inner.pack(fill="x")

        row = tk.Frame(inner, bg=C["bg_panel"])
        row.pack(fill="x")
        self.status_dot = tk.Label(row, text="●", font=self.f_mono,
                                   bg=C["bg_panel"], fg=C["text_lo"])
        self.status_dot.pack(side="left")
        self.status_lbl = tk.Label(row, text="  AWAITING GAME LAUNCH",
                                   font=self.f_label, bg=C["bg_panel"], fg=C["text_mid"])
        self.status_lbl.pack(side="left")
        tk.Label(row, text="AUTO-DETECT: ON",
                 font=self.f_mono_s, bg=C["bg_panel"], fg=C["ice_dim"]).pack(side="right")

        tr = tk.Frame(inner, bg=C["bg_panel"])
        tr.pack(fill="x", pady=(10, 0))
        tk.Label(tr, text="SESSION", font=self.f_label,
                 bg=C["bg_panel"], fg=C["text_mid"]).pack(side="left")
        self.session_var = tk.StringVar(value="00:00:00")
        tk.Label(tr, textvariable=self.session_var,
                 font=self.f_mono_l, bg=C["bg_panel"], fg=C["ice"]).pack(side="right")

        self.last_lbl = tk.Label(inner, text="No sessions recorded yet.",
                                 font=self.f_mono_s, bg=C["bg_panel"], fg=C["text_lo"])
        self.last_lbl.pack(anchor="w", pady=(6, 0))
        self._update_last_session_label()

    def _update_last_session_label(self):
        if self.stats["sessions"]:
            last = self.stats["sessions"][-1]
            dt   = datetime.fromtimestamp(last["start"]/1000).strftime("%d %b %Y  %H:%M")
            dur  = fmt_short(last["duration"])
            self.last_lbl.config(text=f"Last session: {dt}  ·  Duration: {dur}")
        else:
            self.last_lbl.config(text="No sessions recorded yet.")

    def _build_stats_grid(self, parent):
        tk.Label(parent, text="AUTO-RESET STATISTICS",
                 font=self.f_label, bg=C["bg_deep"], fg=C["text_mid"]).pack(
                     anchor="w", padx=22, pady=(8, 6))
        grid = tk.Frame(parent, bg=C["bg_deep"])
        grid.pack(fill="x", padx=20)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        self.stat_vars = {}
        cards = [
            ("today", "☀  TODAY",      C["gold"],  "Resets every 24 hours",                  0, 0),
            ("week",  "◈  THIS WEEK",   C["ice"],   "Resets Sunday 23:59",                    0, 1),
            ("month", "◉  THIS MONTH",  "#9b59b6",  "Resets 1st of month",                    1, 0),
            ("year",  "◎  THIS YEAR",   C["red"],   f"Resets Jan 1st {datetime.now().year+1}", 1, 1),
        ]
        for key, lbl, accent, sub, r, c in cards:
            self.stat_vars[key] = tk.StringVar(value="00:00:00")
            self._stat_card(grid, lbl, self.stat_vars[key], sub, accent, r, c)

    def _stat_card(self, parent, label, var, sub, accent, row, col):
        card = tk.Frame(parent, bg=C["bg_card"],
                        highlightbackground=accent, highlightthickness=1)
        card.grid(row=row, column=col, padx=4, pady=4, sticky="nsew")
        tk.Label(card, text=label, font=self.f_label,
                 bg=C["bg_card"], fg=C["text_mid"]).pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(card, textvariable=var, font=self.f_mono_l,
                 bg=C["bg_card"], fg=accent).pack(anchor="w", padx=12)
        tk.Label(card, text=sub, font=self.f_mono_s,
                 bg=C["bg_card"], fg=C["text_lo"]).pack(anchor="w", padx=12, pady=(0, 10))

    def _build_manual_stats(self, parent):
        tk.Label(parent, text="PERSISTENT STATISTICS",
                 font=self.f_label, bg=C["bg_deep"], fg=C["text_mid"]).pack(
                     anchor="w", padx=22, pady=(8, 6))
        row = tk.Frame(parent, bg=C["bg_deep"])
        row.pack(fill="x", padx=20)
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)

        self.stat_vars["total"] = tk.StringVar(value="00:00:00")
        self._manual_card(row, "★  ALL-TIME TOTAL", self.stat_vars["total"],
                          C["gold_lt"], "Total cumulative playtime",
                          lambda: self._confirm_reset("total"), 0)

        self.stat_vars["avg"] = tk.StringVar(value="—")
        self.avg_sub_var      = tk.StringVar(value="No sessions yet")
        self._manual_card(row, "⬡  AVG SESSION LENGTH", self.stat_vars["avg"],
                          C["green"], "",
                          lambda: self._confirm_reset("avg"), 1,
                          sub_var=self.avg_sub_var)

    def _manual_card(self, parent, label, var, accent, sub, reset_cmd, col, sub_var=None):
        card = tk.Frame(parent, bg=C["bg_card2"],
                        highlightbackground=C["border_hi"], highlightthickness=1)
        card.grid(row=0, column=col, padx=4, pady=4, sticky="nsew")
        tk.Label(card, text=label, font=self.f_label,
                 bg=C["bg_card2"], fg=C["text_mid"]).pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(card, textvariable=var, font=self.f_mono_l,
                 bg=C["bg_card2"], fg=accent).pack(anchor="w", padx=12)
        if sub_var:
            tk.Label(card, textvariable=sub_var, font=self.f_mono_s,
                     bg=C["bg_card2"], fg=C["text_lo"]).pack(anchor="w", padx=12)
        else:
            tk.Label(card, text=sub, font=self.f_mono_s,
                     bg=C["bg_card2"], fg=C["text_lo"]).pack(anchor="w", padx=12)
        btn = tk.Button(card, text="↺  RESET", font=self.f_btn,
                        bg=C["bg_card2"], fg=C["red_dim"],
                        activebackground=C["red_dim"], activeforeground="#fff",
                        relief="flat", bd=0, cursor="hand2",
                        highlightbackground=C["red_dim"], highlightthickness=1,
                        padx=8, pady=3, command=reset_cmd)
        btn.pack(anchor="w", padx=12, pady=(6, 10))
        btn.bind("<Enter>", lambda e, b=btn: b.config(fg=C["red"]))
        btn.bind("<Leave>", lambda e, b=btn: b.config(fg=C["red_dim"]))

    def _build_time_footer(self, parent):
        foot = tk.Frame(parent, bg=C["bg_deep"], pady=14)
        foot.pack(fill="x", padx=20)
        rb = tk.Button(foot, text="☠   RESET TIME TRACKER   ☠",
                       font=self.f_btn,
                       bg=C["bg_deep"], fg=C["red_dim"],
                       activebackground=C["red_dim"], activeforeground="#fff",
                       relief="flat", bd=0, cursor="hand2",
                       highlightbackground=C["red_dim"], highlightthickness=1,
                       padx=16, pady=6, command=self._confirm_full_time_reset)
        rb.pack()
        rb.bind("<Enter>", lambda e: rb.config(fg=C["red"],     highlightbackground=C["red"]))
        rb.bind("<Leave>", lambda e: rb.config(fg=C["red_dim"], highlightbackground=C["red_dim"]))
        tk.Label(foot, text="RESETS ALL TIME DATA ONLY  ·  KILLS UNAFFECTED  ·  CANNOT BE UNDONE",
                 font=self.f_mono_s, bg=C["bg_deep"], fg=C["text_lo"]).pack(pady=(4, 0))
        tk.Label(foot, text=f"Data: {DATA_FILE}",
                 font=self.f_mono_s, bg=C["bg_deep"], fg=C["text_lo"]).pack(pady=(8, 0))

    # ─────────────────────────────────────────────────────────────────────────
    # KILLS TAB
    # ─────────────────────────────────────────────────────────────────────────
    def _build_kills_page(self, parent):
        tk.Label(parent, text="KILL TRACKER",
                 font=self.f_label, bg=C["bg_deep"], fg=C["text_mid"]).pack(
                     anchor="w", padx=22, pady=(14, 6))

        cards_row = tk.Frame(parent, bg=C["bg_deep"])
        cards_row.pack(fill="x", padx=20)
        cards_row.columnconfigure(0, weight=1)
        cards_row.columnconfigure(1, weight=1)

        # Player kills
        self.player_kills_var = tk.StringVar(value="0")
        self._kills_card(cards_row, col=0,
                         title="👤  PLAYER KILLS",
                         accent=C["red"],
                         total_var=self.player_kills_var,
                         submit_cmd=self._submit_player_kills,
                         reset_cmd=lambda: self._confirm_kills_reset("player"))

        # Ship kills
        self.ship_kills_var = tk.StringVar(value="0")
        self._kills_card(cards_row, col=1,
                         title="🚀  SHIP KILLS",
                         accent=C["ice"],
                         total_var=self.ship_kills_var,
                         submit_cmd=self._submit_ship_kills,
                         reset_cmd=lambda: self._confirm_kills_reset("ship"))

        self._div(parent)
        self._build_kills_footer(parent)

    def _kills_card(self, parent, col, title, accent, total_var, submit_cmd, reset_cmd):
        card = tk.Frame(parent, bg=C["bg_card"],
                        highlightbackground=accent, highlightthickness=1)
        card.grid(row=0, column=col, padx=4, pady=4, sticky="nsew")

        tk.Label(card, text=title, font=self.f_label,
                 bg=C["bg_card"], fg=C["text_mid"]).pack(anchor="w", padx=12, pady=(10, 0))

        # Total counter
        tk.Label(card, textvariable=total_var, font=self.f_num_l,
                 bg=C["bg_card"], fg=accent).pack(anchor="w", padx=12)
        tk.Label(card, text="TOTAL KILLS", font=self.f_mono_s,
                 bg=C["bg_card"], fg=C["text_lo"]).pack(anchor="w", padx=12)

        # Input row
        input_row = tk.Frame(card, bg=C["bg_card"])
        input_row.pack(fill="x", padx=12, pady=(12, 4))

        entry_var = tk.StringVar()
        entry = tk.Entry(input_row, textvariable=entry_var, width=7,
                         font=self.f_mono, bg=C["bg_card2"], fg=C["text_hi"],
                         insertbackground=C["text_hi"],
                         highlightbackground=accent, highlightthickness=1,
                         relief="flat", justify="center")
        entry.pack(side="left", padx=(0, 6))

        # Validate integer only 1–9999
        def validate(P):
            if P == "":
                return True
            if P.isdigit() and 1 <= len(P) <= 4:
                return True
            return False
        vcmd = (card.register(validate), "%P")
        entry.config(validate="key", validatecommand=vcmd)

        sub_btn = tk.Button(input_row, text="ADD", font=self.f_btn,
                            bg=C["bg_card2"], fg=accent,
                            activebackground=accent, activeforeground=C["bg_deep"],
                            relief="flat", bd=0, cursor="hand2",
                            highlightbackground=accent, highlightthickness=1,
                            padx=10, pady=3,
                            command=lambda: submit_cmd(entry_var, entry))
        sub_btn.pack(side="left")

        # Allow Enter key to submit
        entry.bind("<Return>", lambda e: submit_cmd(entry_var, entry))

        # Reset button
        rst_btn = tk.Button(card, text="↺  RESET", font=self.f_btn,
                            bg=C["bg_card"], fg=C["red_dim"],
                            activebackground=C["red_dim"], activeforeground="#fff",
                            relief="flat", bd=0, cursor="hand2",
                            highlightbackground=C["red_dim"], highlightthickness=1,
                            padx=8, pady=3, command=reset_cmd)
        rst_btn.pack(anchor="w", padx=12, pady=(4, 12))
        rst_btn.bind("<Enter>", lambda e, b=rst_btn: b.config(fg=C["red"]))
        rst_btn.bind("<Leave>", lambda e, b=rst_btn: b.config(fg=C["red_dim"]))

    def _build_kills_footer(self, parent):
        foot = tk.Frame(parent, bg=C["bg_deep"], pady=14)
        foot.pack(fill="x", padx=20)
        rb = tk.Button(foot, text="☠   RESET ALL KILLS   ☠",
                       font=self.f_btn,
                       bg=C["bg_deep"], fg=C["red_dim"],
                       activebackground=C["red_dim"], activeforeground="#fff",
                       relief="flat", bd=0, cursor="hand2",
                       highlightbackground=C["red_dim"], highlightthickness=1,
                       padx=16, pady=6,
                       command=lambda: self._confirm_kills_reset("all"))
        rb.pack()
        rb.bind("<Enter>", lambda e: rb.config(fg=C["red"],     highlightbackground=C["red"]))
        rb.bind("<Leave>", lambda e: rb.config(fg=C["red_dim"], highlightbackground=C["red_dim"]))
        tk.Label(foot, text="RESETS ALL KILL DATA ONLY  ·  TIME STATS UNAFFECTED  ·  CANNOT BE UNDONE",
                 font=tkfont.Font(family="Consolas", size=8, weight="bold"),
                 bg=C["bg_deep"], fg=C["text_lo"]).pack(pady=(4, 0))

    # ─────────────────────────────────────────────────────────────────────────
    # KILLS SUBMIT LOGIC  (with easter egg)
    # ─────────────────────────────────────────────────────────────────────────
    def _submit_kills(self, entry_var, entry, category):
        raw = entry_var.get().strip()
        if not raw or not raw.isdigit():
            return
        value = int(raw)
        if value < 1 or value > 9999:
            return
        entry_var.set("")
        entry.focus_set()

        if value >= 50:
            self._easter_egg_confirm(value, category)
        else:
            self._add_kills(value, category)

    def _submit_player_kills(self, entry_var, entry):
        self._submit_kills(entry_var, entry, "player")

    def _submit_ship_kills(self, entry_var, entry):
        self._submit_kills(entry_var, entry, "ship")

    def _easter_egg_confirm(self, value, category):
        """Two-step easter egg popup for values >= 50."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Really?")
        dlg.configure(bg=C["bg_deep"])
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.geometry("340x190")
        dlg.transient(self.root)

        outer = tk.Frame(dlg, bg=C["gold_dim"], padx=1, pady=1)
        outer.pack(fill="both", expand=True, padx=12, pady=12)
        inner = tk.Frame(outer, bg=C["bg_deep"], padx=20, pady=16)
        inner.pack(fill="both", expand=True)

        tk.Label(inner, text=f"⚠  {value} kills?", font=self.f_label,
                 bg=C["bg_deep"], fg=C["gold_lt"]).pack()
        tk.Label(inner, text="Are you sure this is accurate?",
                 font=self.f_sub, bg=C["bg_deep"], fg=C["text_mid"]).pack(pady=10)

        btns = tk.Frame(inner, bg=C["bg_deep"])
        btns.pack()

        def on_no():
            dlg.destroy()   # discard — do NOT add

        def on_yes():
            dlg.destroy()
            self._grass_popup(value, category)

        tk.Button(btns, text="No", font=self.f_btn,
                  bg=C["bg_deep"], fg=C["text_mid"],
                  activebackground=C["border"], activeforeground="#fff",
                  relief="flat", bd=0, highlightbackground=C["border"],
                  highlightthickness=1, padx=18, pady=5,
                  cursor="hand2", command=on_no).pack(side="left", padx=6)
        tk.Button(btns, text="Yes", font=self.f_btn,
                  bg=C["green_dim"], fg=C["green"],
                  activebackground=C["green"], activeforeground=C["bg_deep"],
                  relief="flat", bd=0, highlightbackground=C["green"],
                  highlightthickness=1, padx=18, pady=5,
                  cursor="hand2", command=on_yes).pack(side="left", padx=6)

    def _grass_popup(self, value, category):
        dlg = tk.Toplevel(self.root)
        dlg.title("Noted")
        dlg.configure(bg=C["bg_deep"])
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.geometry("300x160")
        dlg.transient(self.root)

        outer = tk.Frame(dlg, bg=C["green_dim"], padx=1, pady=1)
        outer.pack(fill="both", expand=True, padx=12, pady=12)
        inner = tk.Frame(outer, bg=C["bg_deep"], padx=20, pady=18)
        inner.pack(fill="both", expand=True)

        tk.Label(inner, text="🌿  Go touch grass",
                 font=self.f_label, bg=C["bg_deep"], fg=C["green"]).pack(pady=(0, 14))

        def on_ok():
            dlg.destroy()
            self._add_kills(value, category)

        tk.Button(inner, text="OK", font=self.f_btn,
                  bg=C["green_dim"], fg=C["green"],
                  activebackground=C["green"], activeforeground=C["bg_deep"],
                  relief="flat", bd=0, highlightbackground=C["green"],
                  highlightthickness=1, padx=22, pady=5,
                  cursor="hand2", command=on_ok).pack()

    def _add_kills(self, value, category):
        with self._lock:
            if category == "player":
                self.stats["player_kills_total"] += value
            elif category == "ship":
                self.stats["ship_kills_total"] += value
            save_stats(self.stats)
        self._refresh_kills_ui()

    # ─────────────────────────────────────────────────────────────────────────
    # CONFIRM DIALOGS
    # ─────────────────────────────────────────────────────────────────────────
    def _show_confirm(self, title, msg, on_confirm):
        dlg = tk.Toplevel(self.root)
        dlg.title("Confirm")
        dlg.configure(bg=C["bg_deep"])
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.geometry("360x200")
        dlg.transient(self.root)
        outer = tk.Frame(dlg, bg=C["gold_dim"], padx=1, pady=1)
        outer.pack(fill="both", expand=True, padx=12, pady=12)
        inner = tk.Frame(outer, bg=C["bg_deep"], padx=20, pady=16)
        inner.pack(fill="both", expand=True)
        tk.Label(inner, text="⚠  " + title, font=self.f_label,
                 bg=C["bg_deep"], fg=C["gold_lt"]).pack()
        tk.Label(inner, text=msg, font=self.f_sub,
                 bg=C["bg_deep"], fg=C["text_mid"], justify="center").pack(pady=10)
        btns = tk.Frame(inner, bg=C["bg_deep"])
        btns.pack()
        def confirm():
            dlg.destroy()
            on_confirm()
        tk.Button(btns, text="Cancel", font=self.f_btn,
                  bg=C["bg_deep"], fg=C["text_mid"],
                  activebackground=C["border"], activeforeground="#fff",
                  relief="flat", bd=0, highlightbackground=C["border"],
                  highlightthickness=1, padx=14, pady=5,
                  cursor="hand2", command=dlg.destroy).pack(side="left", padx=6)
        tk.Button(btns, text="Confirm Reset", font=self.f_btn,
                  bg=C["red_dim"], fg=C["red"],
                  activebackground=C["red"], activeforeground="#fff",
                  relief="flat", bd=0, highlightbackground=C["red"],
                  highlightthickness=1, padx=14, pady=5,
                  cursor="hand2", command=confirm).pack(side="left", padx=6)

    # ── Time resets ───────────────────────────────────────────────────────────
    def _confirm_reset(self, which):
        labels = {"total": "All-Time Total", "avg": "Average Session Length"}
        self._show_confirm(f"Reset {labels.get(which, which)}?",
                           "This cannot be undone.",
                           lambda: self._do_time_reset(which))

    def _confirm_full_time_reset(self):
        self._show_confirm("RESET TIME TRACKER",
                           "All time stats will be wiped.\nKill stats will NOT be affected.\nThis cannot be undone.",
                           self._do_full_time_reset)

    def _do_time_reset(self, which):
        """Isolated per-stat time resets — does not touch kills."""
        with self._lock:
            if which == "total":
                # Only reset total — sessions and avg stay intact
                self.stats["total_seconds"] = 0
            elif which == "avg":
                # Only reset session history — total is unaffected
                self.stats["sessions"] = []
            save_stats(self.stats)
        self._refresh_ui()

    def _do_full_time_reset(self):
        """Wipe all time stats only — kill stats preserved."""
        with self._lock:
            kills_p = self.stats["player_kills_total"]
            kills_s = self.stats["ship_kills_total"]
            self.stats = dict(DEFAULT_STATS)
            self.stats["player_kills_total"] = kills_p
            self.stats["ship_kills_total"]   = kills_s
            self.stats["today_date"]        = date.today().isoformat()
            self.stats["last_week_reset"]   = datetime.now().isoformat()
            self.stats["last_month_reset"]  = datetime.now().isoformat()
            self.stats["last_year_reset"]   = datetime.now().isoformat()
            save_stats(self.stats)
        self._refresh_ui()

    # ── Kill resets ───────────────────────────────────────────────────────────
    def _confirm_kills_reset(self, which):
        labels = {"player": "Player Kills", "ship": "Ship Kills", "all": "All Kills"}
        self._show_confirm(f"Reset {labels.get(which, which)}?",
                           "This cannot be undone.",
                           lambda: self._do_kills_reset(which))

    def _do_kills_reset(self, which):
        """Isolated kill resets — does not touch time stats."""
        with self._lock:
            if which == "player":
                self.stats["player_kills_total"] = 0
            elif which == "ship":
                self.stats["ship_kills_total"] = 0
            elif which == "all":
                self.stats["player_kills_total"] = 0
                self.stats["ship_kills_total"]   = 0
            save_stats(self.stats)
        self._refresh_kills_ui()

    # ─────────────────────────────────────────────────────────────────────────
    # BACKGROUND THREADS
    # ─────────────────────────────────────────────────────────────────────────
    def _start_threads(self):
        threading.Thread(target=self._process_watcher, daemon=True).start()
        threading.Thread(target=self._tick_loop,       daemon=True).start()

    def _process_watcher(self):
        was_running = False
        while self._running:
            running = is_game_running()
            if running and not was_running:
                self._on_game_start()
            elif not running and was_running:
                self._on_game_stop()
            was_running = running
            time.sleep(POLL_INTERVAL)

    def _tick_loop(self):
        while self._running:
            if self.tracking and self.session_start:
                self.session_secs = int(time.time() - self.session_start)
            time.sleep(1)

    def _on_game_start(self):
        with self._lock:
            self.session_start = time.time()
            self.session_secs  = 0
            self.tracking      = True
        self.root.after(0, self._set_active_status)

    def _on_game_stop(self):
        with self._lock:
            if not self.tracking:
                return
            duration = int(time.time() - self.session_start)
            self.stats["sessions"].append({
                "start":    int(self.session_start * 1000),
                "end":      int(time.time() * 1000),
                "duration": duration,
            })
            for key in ("total_seconds", "week_seconds", "month_seconds",
                        "year_seconds", "today_seconds"):
                self.stats[key] += duration
            self.tracking      = False
            self.session_start = None
            self.session_secs  = 0
            save_stats(self.stats)
        self.root.after(0, self._set_idle_status)
        self.root.after(0, self._update_last_session_label)

    # ─────────────────────────────────────────────────────────────────────────
    # UI REFRESH
    # ─────────────────────────────────────────────────────────────────────────
    def _set_active_status(self):
        self.status_dot.config(fg=C["green"])
        self.status_lbl.config(text="  GAME CLIENT ACTIVE — TRACKING", fg=C["green"])

    def _set_idle_status(self):
        self.status_dot.config(fg=C["text_lo"])
        self.status_lbl.config(text="  AWAITING GAME LAUNCH", fg=C["text_mid"])
        self.session_var.set("00:00:00")

    def _refresh_ui(self):
        """Refresh time tab stats only."""
        with self._lock:
            s    = self.stats
            live = self.session_secs if self.tracking else 0
        self.session_var.set(fmt_time(live))
        self.stat_vars["today"].set(fmt_time(s["today_seconds"]  + live))
        self.stat_vars["week"].set( fmt_time(s["week_seconds"]   + live))
        self.stat_vars["month"].set(fmt_time(s["month_seconds"]  + live))
        self.stat_vars["year"].set( fmt_time(s["year_seconds"]   + live))
        self.stat_vars["total"].set(fmt_time(s["total_seconds"]  + live))
        sessions = s["sessions"]
        if sessions:
            avg = sum(x["duration"] for x in sessions) / len(sessions)
            self.stat_vars["avg"].set(fmt_short(avg))
            self.avg_sub_var.set(
                f"Across {len(sessions)} session{'s' if len(sessions)!=1 else ''}")
        else:
            self.stat_vars["avg"].set("—")
            self.avg_sub_var.set("No sessions yet")

    def _refresh_kills_ui(self):
        """Refresh kills tab stats only."""
        with self._lock:
            s = self.stats
        self.player_kills_var.set(str(s["player_kills_total"]))
        self.ship_kills_var.set(str(s["ship_kills_total"]))

    def _schedule_refresh(self):
        if not self._running:
            return
        with self._lock:
            self.stats, changed = apply_auto_resets(self.stats)
            if changed:
                save_stats(self.stats)
        self._refresh_ui()
        self._refresh_kills_ui()
        self.root.after(1000, self._schedule_refresh)

    # ─────────────────────────────────────────────────────────────────────────
    # SYSTEM TRAY
    # ─────────────────────────────────────────────────────────────────────────
    def _build_tray(self):
        try:
            img  = make_tray_icon_image()
            menu = pystray.Menu(
                pystray.MenuItem("Show", lambda: self.root.after(0, self.root.deiconify)),
                pystray.MenuItem("Quit", lambda: self.root.after(0, self._quit)),
            )
            self.tray = pystray.Icon("VerseTracker", img, "Verse Tracker", menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # LIFECYCLE
    # ─────────────────────────────────────────────────────────────────────────
    def _on_close(self):
        self._quit()

    def _quit(self):
        self._running = False
        if self.tracking:
            self._on_game_stop()
        try:
            if TRAY_AVAILABLE:
                self.tray.stop()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self._schedule_refresh()
        self.root.mainloop()

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = VerseTracker()
    app.run()
