"""
Verse Tracker — Star Citizen Playtime, Kills, Economy, Server & Travel Monitor
PyInstaller-ready single-file build.

Build command:
    pyinstaller --onefile --windowed --name "VerseTracker" --icon "VerseTracker.ico" sc_tracker.py

Requirements:
    pip install psutil pystray pillow pyinstaller
"""

import multiprocessing
multiprocessing.freeze_support()

import tkinter as tk
from tkinter import font as tkfont
import threading, time, json, os, re
import psutil
from datetime import datetime, date
from pathlib import Path
from io import BytesIO

# Pystray and Pillow are imported lazily in make_tray_icon_image / make_window_icon
# to avoid slowing down startup — they're only needed once the window is shown
TRAY_AVAILABLE = False
try:
    import pystray as _pystray_test
    from PIL import Image as _pil_test
    TRAY_AVAILABLE = True
except ImportError:
    pass

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
PROCESS_NAME = "StarCitizen.exe"
POLL_INTERVAL = 3

SC_LOG_SEARCH_PATHS = [
    Path("C:/Program Files/Roberts Space Industries/StarCitizen/LIVE/Game.log"),
    Path("C:/Program Files (x86)/Roberts Space Industries/StarCitizen/LIVE/Game.log"),
    Path(os.path.expanduser("~")) / "Roberts Space Industries/StarCitizen/LIVE/Game.log",
]

DATA_FILE = Path(os.path.expanduser("~")) / ".verse_tracker_stats.json"
CFG_FILE  = Path(os.path.expanduser("~")) / ".verse_tracker_cfg.json"

# ─────────────────────────────────────────────────────────────────────────────
# COLOURS
# ─────────────────────────────────────────────────────────────────────────────
C = {
    "bg_root":   "#0a0f18",
    "bg_deep":   "#0d1520",
    "bg_panel":  "#111d2e",
    "bg_card":   "#131f32",
    "bg_card2":  "#162438",
    "bg_input":  "#0f1a28",
    "border":    "#1e3048",
    "border_hi": "#2a4865",
    "border_lo": "#162438",
    "gold":      "#c8922a",
    "gold_lt":   "#daa84a",
    "gold_dim":  "#6a4a14",
    "ice":       "#8ab8d8",
    "ice_dim":   "#3a6888",
    "green":     "#4dbe82",
    "green_dim": "#1e5438",
    "red":       "#c85858",
    "red_dim":   "#6a2828",
    "orange":    "#d4824a",
    "purple":    "#8868c8",
    "teal":      "#4ab8b8",
    "text_hi":   "#e8f0f8",
    "text_mid":  "#8aaac8",
    "text_lo":   "#9ab8cc",
    "text_dim":  "#4a6880",
    "mono":      "#c8dcea",
}

# ─────────────────────────────────────────────────────────────────────────────
# LOG PARSING — server, zone, ship events
# ─────────────────────────────────────────────────────────────────────────────

# Server reroute — picks up server connection events
# Full shard ID from Join PU line — e.g:
#   <Join PU> address[34.76.55.82] port[64349] shard[pub_euw1b_11890775_150]
RE_JOIN_PU = re.compile(
    r'<Join PU>.*?shard\[([^\]]+)\]', re.IGNORECASE)

# Fallback patterns
RE_SHARD_ID  = re.compile(r'ShardId[:\s]+([^\s,\]]+)', re.IGNORECASE)
RE_SERVER_ID = re.compile(r'\bServer[:\s]+(pub[-_][^\s,\]]+)', re.IGNORECASE)
RE_SERVER_REROUTE = re.compile(
    r'Local Route Guard - Server Rerouted.*?\[CL\]\[(\d+)\]', re.IGNORECASE)

# Player handle
RE_HANDLE = re.compile(r'Handle:\s+(\S+)')

def parse_log_line_for_server(line, player_name):
    """
    Returns a dict describing what was detected, or None.
    Types: 'server_id', 'player_name'
    """
    # Player handle detection
    m = RE_HANDLE.search(line)
    if m:
        return {"type": "player_name", "value": m.group(1)}

    # Primary: Join PU shard line — most reliable source
    # e.g. <Join PU> address[34.76.55.82] port[64349] shard[pub_euw1b_11890775_150]
    if "Join PU" in line:
        m = RE_JOIN_PU.search(line)
        if m:
            return {"type": "server_id", "value": m.group(1)}

    # Fallback: ShardId line
    m = RE_SHARD_ID.search(line)
    if m:
        return {"type": "server_id", "value": m.group(1)}

    # Fallback: Server: pub- line
    m = RE_SERVER_ID.search(line)
    if m:
        return {"type": "server_id", "value": m.group(1)}

    # Fallback: Local Route Guard reroute
    if "Local Route Guard" in line and "Server Rerouted" in line:
        m = RE_SERVER_REROUTE.search(line)
        val = m.group(1) if m else "Unknown"
        return {"type": "server_reroute", "value": val}

    return None

def find_log_file():
    for p in SC_LOG_SEARCH_PATHS:
        if p.exists():
            return p
    return None

def load_player_name_from_log(log_path):
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 500: break
                m = RE_HANDLE.search(line)
                if m: return m.group(1)
    except Exception:
        pass
    return None

# ─────────────────────────────────────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_STATS = {
    # time
    "total_seconds": 0, "week_seconds": 0, "month_seconds": 0,
    "year_seconds": 0, "sessions": [], "last_week_reset": None,
    "last_month_reset": None, "last_year_reset": None,
    "today_date": None, "today_seconds": 0,
    # kills (manual)
    "player_kills": 0, "ai_kills": 0,
    "player_ship_kills": 0, "ai_ship_kills": 0,
    # economy
    "session_income": 0, "session_expenses": 0,
    "total_income": 0, "total_expenses": 0,
}

def load_stats():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
            for k, v in DEFAULT_STATS.items():
                data.setdefault(k, v)
            if "player_kills_total" in data:
                data["player_kills"] = data.pop("player_kills_total")
            if "ship_kills_total" in data:
                data["ai_ship_kills"] = data.pop("ship_kills_total")
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

def load_cfg():
    if CFG_FILE.exists():
        try:
            with open(CFG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"log_path": None}

def save_cfg(cfg):
    try:
        with open(CFG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

def apply_auto_resets(stats):
    now = datetime.now()
    changed = False
    today_str = date.today().isoformat()
    if stats.get("today_date") != today_str:
        stats["today_seconds"] = 0; stats["today_date"] = today_str; changed = True

    def last_sunday_reset_due(last_reset_str):
        if not last_reset_str:
            return True
        last = datetime.fromisoformat(last_reset_str)
        dow = now.weekday()  # Mon=0 … Sun=6
        days_since_sunday = 0 if dow == 6 else (dow + 1)
        try:
            candidate = now.replace(
                hour=23, minute=59, second=59, microsecond=0,
                day=now.day - days_since_sunday)
        except ValueError:
            return False
        # Only reset if the Sunday boundary is strictly in the past AND after last reset
        return candidate < now and last < candidate

    if last_sunday_reset_due(stats.get("last_week_reset")):
        if stats.get("last_week_reset"): stats["week_seconds"] = 0
        stats["last_week_reset"] = now.isoformat(); changed = True

    for key, field in [("last_month_reset","month_seconds"),("last_year_reset","year_seconds")]:
        lv = stats.get(key)
        if lv:
            ldt = datetime.fromisoformat(lv)
            expired = (key == "last_month_reset" and
                       (ldt.month != now.month or ldt.year != now.year)) or \
                      (key == "last_year_reset" and ldt.year != now.year)
            if expired:
                stats[field] = 0; stats[key] = now.isoformat(); changed = True
        else:
            stats[key] = now.isoformat(); changed = True
    return stats, changed

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def fmt_time(s):
    h=int(s//3600); m=int((s%3600)//60); sec=int(s%60)
    return f"{h:02d}:{m:02d}:{sec:02d}"

def fmt_short(s):
    h=int(s//3600); m=int((s%3600)//60)
    return f"{h}h {m}m" if h else f"{m}m {int(s%60)}s"

def fmt_auec(v):
    neg = v < 0
    s = f"{abs(int(v)):,} aUEC"
    return f"-{s}" if neg else s

def is_game_running():
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] and p.info["name"].lower() == PROCESS_NAME.lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied): pass
    return False

def make_tray_icon_image():
    from PIL import Image, ImageDraw
    s=64; img=Image.new("RGBA",(s,s),(0,0,0,0)); d=ImageDraw.Draw(img)
    d.ellipse([2,2,s-2,s-2],fill=(10,15,32,255))
    d.ellipse([2,2,s-2,s-2],outline=(42,72,112,255),width=2)
    cx,cy=s//2,s//2; r=s*0.30
    pts=[(cx,cy-r),(cx+r*0.6,cy),(cx,cy+r),(cx-r*0.6,cy)]
    d.polygon([(cx,cy-r*1.08),(cx+r*0.65,cy),(cx,cy+r*1.08),(cx-r*0.65,cy)],fill=(100,70,10,255))
    d.polygon(pts,fill=(180,130,30,255))
    d.polygon([(cx,cy-r),(cx+r*0.6,cy),(cx,cy),(cx-r*0.6,cy)],fill=(218,168,74,255))
    return img

def make_window_icon(root):
    try:
        from PIL import Image, ImageDraw
        s=32; img=Image.new("RGBA",(s,s),(0,0,0,0)); d=ImageDraw.Draw(img)
        d.ellipse([1,1,s-1,s-1],fill=(10,15,32,255))
        d.ellipse([1,1,s-1,s-1],outline=(42,72,112,255),width=1)
        cx,cy=s//2,s//2; r=s*0.30
        pts=[(cx,cy-r),(cx+r*0.6,cy),(cx,cy+r),(cx-r*0.6,cy)]
        d.polygon([(cx,cy-r*1.08),(cx+r*0.65,cy),(cx,cy+r*1.08),(cx-r*0.65,cy)],fill=(100,70,10,255))
        d.polygon(pts,fill=(180,130,30,255))
        d.polygon([(cx,cy-r),(cx+r*0.6,cy),(cx,cy),(cx-r*0.6,cy)],fill=(218,168,74,255))
        buf=BytesIO(); img.save(buf,format="PNG"); buf.seek(0)
        from tkinter import PhotoImage
        photo=PhotoImage(data=buf.getvalue())
        root.iconphoto(True,photo); root._icon_ref=photo
    except Exception: pass

# ─────────────────────────────────────────────────────────────────────────────
# APPLICATION
# ─────────────────────────────────────────────────────────────────────────────
class VerseTracker:
    def __init__(self):
        self.cfg      = load_cfg()
        self.stats    = load_stats()

        self.tracking      = False
        self.session_start = None
        self.session_secs  = 0
        self._lock         = threading.Lock()
        self._running      = True
        self._log_path     = None
        self._log_pos      = 0
        self._player_name  = None
        self._server_id       = "—"
        self._server_connects = 0

        self._build_ui()
        # Defer everything else until after the window is visible
        self.root.after(50,  self._post_show_init)

    def _post_show_init(self):
        """Runs after first paint — keeps startup snappy."""
        # Auto-resets, log path, threads, tray all happen after window appears
        with self._lock:
            self.stats, changed = apply_auto_resets(self.stats)
            if changed:
                save_stats(self.stats)
        self._resolve_log_path()
        self._start_threads()
        self._schedule_refresh()
        self.root.after(300, self._populate_history)
        if TRAY_AVAILABLE:
            threading.Thread(target=self._build_tray, daemon=True).start()

    def _resolve_log_path(self):
        saved = self.cfg.get("log_path")
        if saved and Path(saved).exists():
            self._log_path = Path(saved); return
        found = find_log_file()
        if found:
            self._log_path = found
            self.cfg["log_path"] = str(found); save_cfg(self.cfg)

    # ─────────────────────────────────────────────────────────────────────────
    # UI ROOT
    # ─────────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Verse Tracker")
        self.root.configure(bg=C["bg_root"])
        self.root.resizable(False, False)
        self.root.geometry("680x940")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if TRAY_AVAILABLE:
            make_window_icon(self.root)

        FN_MONO = "Consolas"
        FN_UI   = "Segoe UI"

        self.f_title  = tkfont.Font(family=FN_UI,   size=18, weight="bold")
        self.f_sub    = tkfont.Font(family=FN_UI,   size=9)
        self.f_label  = tkfont.Font(family=FN_UI,   size=9,  weight="bold")
        self.f_tab    = tkfont.Font(family=FN_UI,   size=9,  weight="bold")
        self.f_btn    = tkfont.Font(family=FN_UI,   size=9,  weight="bold")
        self.f_small  = tkfont.Font(family=FN_MONO, size=8)
        self.f_mono   = tkfont.Font(family=FN_MONO, size=10)
        self.f_mono_s = tkfont.Font(family=FN_MONO, size=9)
        self.f_mono_l = tkfont.Font(family=FN_MONO, size=20, weight="bold")
        self.f_num_xl = tkfont.Font(family=FN_MONO, size=26, weight="bold")
        self.f_num_l  = tkfont.Font(family=FN_MONO, size=17, weight="bold")

        wrap = tk.Frame(self.root, bg=C["border"], padx=1, pady=1)
        wrap.pack(fill="both", expand=True, padx=10, pady=10)
        self.main = tk.Frame(wrap, bg=C["bg_deep"])
        self.main.pack(fill="both", expand=True)

        self._build_header(self.main)
        self._build_tabs(self.main)

    # ── Header ────────────────────────────────────────────────────────────────
    def _build_header(self, parent):
        hdr = tk.Frame(parent, bg=C["bg_deep"])
        hdr.pack(fill="x")
        tk.Frame(hdr, bg=C["gold_dim"], height=2).pack(fill="x")
        inner = tk.Frame(hdr, bg=C["bg_deep"], pady=12)
        inner.pack(fill="x", padx=22)
        tk.Label(inner, text="VERSE TRACKER", font=self.f_title,
                 bg=C["bg_deep"], fg=C["gold_lt"]).pack(side="left")
        right = tk.Frame(inner, bg=C["bg_deep"])
        right.pack(side="right")
        self.status_dot = tk.Label(right, text="●", font=self.f_mono,
                                   bg=C["bg_deep"], fg=C["text_dim"])
        self.status_dot.pack(side="left")
        self.status_lbl = tk.Label(right, text=" AWAITING GAME",
                                   font=self.f_label, bg=C["bg_deep"], fg=C["text_mid"])
        self.status_lbl.pack(side="left")
        tk.Frame(hdr, bg=C["border"], height=1).pack(fill="x")

    # ── Tabs ──────────────────────────────────────────────────────────────────
    def _build_tabs(self, parent):
        bar_outer = tk.Frame(parent, bg=C["bg_panel"])
        bar_outer.pack(fill="x")
        bar_wrap = tk.Frame(bar_outer, bg=C["bg_panel"])
        bar_wrap.pack(expand=True)
        bar = tk.Frame(bar_wrap, bg=C["bg_panel"])
        bar.pack()

        self._tab_btns  = {}
        self._tab_pages = {}
        self._tab_built = set()

        tabs = [
            ("time",    "⏱  PLAYTIME"),
            ("history", "📋  HISTORY"),
            ("kills",   "☠  KILLS"),
            ("economy", "◈  ECONOMY"),
            ("summary", "◆  SUMMARY"),
            ("server",  "⬡  SERVER"),
        ]
        for name, label in tabs:
            btn = tk.Button(bar, text=label, font=self.f_tab,
                            bg=C["bg_panel"], relief="flat", bd=0,
                            padx=14, pady=9, cursor="hand2",
                            command=lambda n=name: self._switch_tab(n))
            btn.pack(side="left")
            self._tab_btns[name] = btn

        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x")
        container = tk.Frame(parent, bg=C["bg_deep"])
        container.pack(fill="both", expand=True)

        for name, _ in tabs:
            page = tk.Frame(container, bg=C["bg_deep"])
            self._tab_pages[name] = page

        self._build_time_page(self._tab_pages["time"])
        self._build_history_page(self._tab_pages["history"])
        self._build_kills_page(self._tab_pages["kills"])
        self._build_economy_page(self._tab_pages["economy"])
        self._build_summary_page(self._tab_pages["summary"])
        self._build_server_page(self._tab_pages["server"])

        # Mark all as built
        for name, _ in tabs:
            self._tab_built.add(name)

        self._switch_tab("time")

    def _switch_tab(self, name):
        for n, p in self._tab_pages.items():
            p.pack_forget()
        self._tab_pages[name].pack(fill="both", expand=True)
        for n, btn in self._tab_btns.items():
            btn.config(fg=C["gold_lt"] if n==name else C["text_dim"],
                       bg=C["bg_card"]  if n==name else C["bg_panel"])

    # ── Shared helpers ─────────────────────────────────────────────────────────
    def _div(self, parent, pad=5):
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", padx=18, pady=pad)

    def _section_label(self, parent, text, pady=(8,4)):
        tk.Label(parent, text=text, font=self.f_label,
                 bg=C["bg_deep"], fg=C["text_mid"]).pack(anchor="w", padx=22, pady=pady)

    def _card_frame(self, parent, accent, row, col, bg=None, colspan=1):
        bg = bg or C["bg_card"]
        card = tk.Frame(parent, bg=bg, highlightbackground=accent, highlightthickness=1)
        card.grid(row=row, column=col, columnspan=colspan, padx=5, pady=5, sticky="nsew")
        return card

    def _reset_btn(self, parent, cmd, bg=None):
        bg = bg or C["bg_card"]
        btn = tk.Button(parent, text="↺  RESET", font=self.f_btn,
                        bg=bg, fg=C["red_dim"],
                        activebackground=C["red_dim"], activeforeground="#fff",
                        relief="flat", bd=0, cursor="hand2",
                        highlightbackground=C["red_dim"], highlightthickness=1,
                        padx=10, pady=3, command=cmd)
        btn.pack(anchor="w", padx=12, pady=(4,10))
        btn.bind("<Enter>", lambda e,b=btn: b.config(fg=C["red"],    highlightbackground=C["red"]))
        btn.bind("<Leave>", lambda e,b=btn: b.config(fg=C["red_dim"],highlightbackground=C["red_dim"]))

    def _danger_btn(self, parent, text, cmd):
        btn = tk.Button(parent, text=text, font=self.f_btn,
                        bg=C["bg_deep"], fg=C["red_dim"],
                        activebackground=C["red_dim"], activeforeground="#fff",
                        relief="flat", bd=0, cursor="hand2",
                        highlightbackground=C["red_dim"], highlightthickness=1,
                        padx=18, pady=6, command=cmd)
        btn.pack(pady=(4,0))
        btn.bind("<Enter>", lambda e,b=btn: b.config(fg=C["red"],    highlightbackground=C["red"]))
        btn.bind("<Leave>", lambda e,b=btn: b.config(fg=C["red_dim"],highlightbackground=C["red_dim"]))

    def _show_confirm(self, title, msg, on_confirm):
        dlg = tk.Toplevel(self.root)
        dlg.title("Confirm"); dlg.configure(bg=C["bg_deep"])
        dlg.resizable(False,False); dlg.grab_set()
        dlg.geometry("380x210"); dlg.transient(self.root)
        border = tk.Frame(dlg, bg=C["gold_dim"], padx=1, pady=1)
        border.pack(fill="both", expand=True, padx=14, pady=14)
        inner = tk.Frame(border, bg=C["bg_panel"], padx=22, pady=18)
        inner.pack(fill="both", expand=True)
        tk.Label(inner, text="⚠  "+title, font=self.f_label,
                 bg=C["bg_panel"], fg=C["gold_lt"]).pack()
        tk.Label(inner, text=msg, font=self.f_sub,
                 bg=C["bg_panel"], fg=C["text_mid"], justify="center").pack(pady=10)
        btns = tk.Frame(inner, bg=C["bg_panel"]); btns.pack()
        def confirm(): dlg.destroy(); on_confirm()
        for text,cmd,fg,hl in [
            ("Cancel",        dlg.destroy, C["text_mid"], C["border"]),
            ("Confirm Reset", confirm,     C["red"],      C["red_dim"]),
        ]:
            tk.Button(btns, text=text, font=self.f_btn,
                      bg=C["bg_card2"], fg=fg,
                      activebackground=hl, activeforeground="#fff",
                      relief="flat", bd=0, highlightbackground=hl,
                      highlightthickness=1, padx=14, pady=5,
                      cursor="hand2", command=cmd).pack(side="left", padx=6)

    # ─────────────────────────────────────────────────────────────────────────
    # PLAYTIME TAB
    # ─────────────────────────────────────────────────────────────────────────
    def _build_time_page(self, parent):
        self._build_session_panel(parent)
        self._div(parent)
        self._section_label(parent, "AUTO-RESET PERIODS")
        self._build_time_grid(parent)
        self._div(parent)
        self._section_label(parent, "PERSISTENT TOTALS")
        self._build_persistent_time(parent)
        self._div(parent, pad=4)
        self._build_time_footer(parent)

    def _build_session_panel(self, parent):
        pnl = tk.Frame(parent, bg=C["bg_panel"],
                       highlightbackground=C["border"], highlightthickness=1)
        pnl.pack(fill="x", padx=18, pady=(12,6))
        inner = tk.Frame(pnl, bg=C["bg_panel"], padx=16, pady=12)
        inner.pack(fill="x")
        row = tk.Frame(inner, bg=C["bg_panel"]); row.pack(fill="x")
        left = tk.Frame(row, bg=C["bg_panel"]); left.pack(side="left")
        tk.Label(left, text="CURRENT SESSION", font=self.f_label,
                 bg=C["bg_panel"], fg=C["text_mid"]).pack(anchor="w")
        self.session_var = tk.StringVar(value="00:00:00")
        tk.Label(left, textvariable=self.session_var, font=self.f_mono_l,
                 bg=C["bg_panel"], fg=C["ice"]).pack(anchor="w")
        right = tk.Frame(row, bg=C["bg_panel"]); right.pack(side="right", anchor="n")
        tk.Label(right, text="PROCESS MONITOR", font=self.f_small,
                 bg=C["bg_panel"], fg=C["text_dim"]).pack(anchor="e")
        self.session_status_lbl = tk.Label(right, text="Awaiting game launch...",
                                           font=self.f_small, bg=C["bg_panel"], fg=C["text_dim"])
        self.session_status_lbl.pack(anchor="e")
        self.last_lbl = tk.Label(inner, text="No sessions recorded yet.",
                                 font=self.f_small, bg=C["bg_panel"], fg=C["text_dim"])
        self.last_lbl.pack(anchor="w", pady=(8,0))
        self._update_last_session_label()

    def _update_last_session_label(self):
        if self.stats["sessions"]:
            last = self.stats["sessions"][-1]
            dt  = datetime.fromtimestamp(last["start"]/1000).strftime("%d %b %Y  %H:%M")
            dur = fmt_short(last["duration"])
            self.last_lbl.config(text=f"Last session:  {dt}  ·  {dur}")
        else:
            self.last_lbl.config(text="No sessions recorded yet.")

    def _build_time_grid(self, parent):
        grid = tk.Frame(parent, bg=C["bg_deep"]); grid.pack(fill="x", padx=18)
        grid.columnconfigure(0, weight=1); grid.columnconfigure(1, weight=1)
        self.stat_vars = {}
        cards = [
            ("today","☀  TODAY",     C["gold"],   "Resets every 24 hours",                   0,0),
            ("week", "◈  THIS WEEK",  C["ice"],    "Resets Sunday 23:59",                     0,1),
            ("month","●  THIS MONTH", C["purple"], "Resets 1st of month",                     1,0),
            ("year", "◎  THIS YEAR",  C["teal"],   f"Resets Jan 1st {datetime.now().year+1}", 1,1),
        ]
        for key,lbl,accent,sub,r,c in cards:
            self.stat_vars[key] = tk.StringVar(value="00:00:00")
            card = self._card_frame(grid, accent, r, c)
            tk.Label(card, text=lbl, font=self.f_label,
                     bg=C["bg_card"], fg=C["text_mid"]).pack(anchor="w", padx=14, pady=(10,0))
            tk.Label(card, textvariable=self.stat_vars[key], font=self.f_mono_l,
                     bg=C["bg_card"], fg=accent).pack(anchor="w", padx=14)
            tk.Label(card, text=sub, font=self.f_small,
                     bg=C["bg_card"], fg=C["text_lo"]).pack(anchor="w", padx=14, pady=(0,10))

    def _build_persistent_time(self, parent):
        grid = tk.Frame(parent, bg=C["bg_deep"]); grid.pack(fill="x", padx=18)
        grid.columnconfigure(0, weight=1); grid.columnconfigure(1, weight=1)
        self.stat_vars["total"] = tk.StringVar(value="00:00:00")
        card = self._card_frame(grid, C["gold_lt"], 0, 0, bg=C["bg_card2"])
        tk.Label(card, text="★  ALL-TIME TOTAL", font=self.f_label,
                 bg=C["bg_card2"], fg=C["text_mid"]).pack(anchor="w", padx=14, pady=(10,0))
        tk.Label(card, textvariable=self.stat_vars["total"], font=self.f_mono_l,
                 bg=C["bg_card2"], fg=C["gold_lt"]).pack(anchor="w", padx=14)
        tk.Label(card, text="Total cumulative playtime", font=self.f_small,
                 bg=C["bg_card2"], fg=C["text_lo"]).pack(anchor="w", padx=14)
        self._reset_btn(card, lambda: self._confirm_reset("total"), bg=C["bg_card2"])
        self.stat_vars["avg"] = tk.StringVar(value="—")
        self.avg_sub_var = tk.StringVar(value="No sessions yet")
        card2 = self._card_frame(grid, C["green"], 0, 1, bg=C["bg_card2"])
        tk.Label(card2, text="⬡  AVG SESSION", font=self.f_label,
                 bg=C["bg_card2"], fg=C["text_mid"]).pack(anchor="w", padx=14, pady=(10,0))
        tk.Label(card2, textvariable=self.stat_vars["avg"], font=self.f_mono_l,
                 bg=C["bg_card2"], fg=C["green"]).pack(anchor="w", padx=14)
        tk.Label(card2, textvariable=self.avg_sub_var, font=self.f_small,
                 bg=C["bg_card2"], fg=C["text_lo"]).pack(anchor="w", padx=14)
        self._reset_btn(card2, lambda: self._confirm_reset("avg"), bg=C["bg_card2"])

    def _build_time_footer(self, parent):
        foot = tk.Frame(parent, bg=C["bg_deep"], pady=10); foot.pack(fill="x", padx=18)
        self._danger_btn(foot, "☠   RESET ALL TIME DATA   ☠", self._confirm_full_time_reset)
        tk.Label(foot, text="Time stats only  ·  Kill, economy & travel data unaffected",
                 font=self.f_small, bg=C["bg_deep"], fg=C["text_dim"]).pack(pady=(4,0))
        tk.Label(foot, text=f"Data: {DATA_FILE}", font=self.f_small,
                 bg=C["bg_deep"], fg=C["text_dim"]).pack(pady=(4,0))

    # ─────────────────────────────────────────────────────────────────────────
    # SESSION HISTORY TAB
    # ─────────────────────────────────────────────────────────────────────────
    def _build_history_page(self, parent):
        # Header row with reset button
        hdr_row = tk.Frame(parent, bg=C["bg_deep"])
        hdr_row.pack(fill="x", padx=18, pady=(14,0))
        tk.Label(hdr_row, text="SESSION HISTORY", font=self.f_label,
                 bg=C["bg_deep"], fg=C["text_mid"]).pack(side="left")
        rst = tk.Button(hdr_row, text="↺  RESET HISTORY", font=self.f_btn,
                        bg=C["bg_deep"], fg=C["red_dim"],
                        activebackground=C["red_dim"], activeforeground="#fff",
                        relief="flat", bd=0, cursor="hand2",
                        highlightbackground=C["red_dim"], highlightthickness=1,
                        padx=10, pady=3,
                        command=self._confirm_history_reset)
        rst.pack(side="right")
        rst.bind("<Enter>", lambda e: rst.config(fg=C["red"],    highlightbackground=C["red"]))
        rst.bind("<Leave>", lambda e: rst.config(fg=C["red_dim"],highlightbackground=C["red_dim"]))

        # Summary cards
        summ = tk.Frame(parent, bg=C["bg_deep"]); summ.pack(fill="x", padx=18, pady=(6,0))
        summ.columnconfigure(0,weight=1); summ.columnconfigure(1,weight=1)
        summ.columnconfigure(2,weight=1)

        self.hist_count_var   = tk.StringVar(value="0")
        self.hist_total_var   = tk.StringVar(value="00:00:00")
        self.hist_longest_var = tk.StringVar(value="—")

        for lbl,var,accent,col in [
            ("TOTAL SESSIONS",  self.hist_count_var,   C["ice"],    0),
            ("TOTAL PLAYTIME",  self.hist_total_var,   C["gold_lt"],1),
            ("LONGEST SESSION", self.hist_longest_var, C["purple"], 2),
        ]:
            f = tk.Frame(summ, bg=C["bg_card2"],
                         highlightbackground=accent, highlightthickness=1)
            f.grid(row=0, column=col, padx=5, pady=5, sticky="nsew")
            tk.Label(f, text=lbl, font=self.f_label,
                     bg=C["bg_card2"], fg=C["text_mid"]).pack(anchor="w", padx=12, pady=(8,0))
            tk.Label(f, textvariable=var, font=self.f_mono_l,
                     bg=C["bg_card2"], fg=accent).pack(anchor="w", padx=12, pady=(0,8))

        self._section_label(parent, "INDIVIDUAL SESSIONS", pady=(8,4))

        # Scrollable session list
        list_outer = tk.Frame(parent, bg=C["bg_panel"],
                              highlightbackground=C["border"], highlightthickness=1)
        list_outer.pack(fill="both", expand=True, padx=18, pady=(0,8))

        canvas = tk.Canvas(list_outer, bg=C["bg_panel"], highlightthickness=0)
        scrollbar = tk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
        self.hist_inner = tk.Frame(canvas, bg=C["bg_panel"])

        def _on_canvas_resize(e):
            canvas.itemconfig(canvas_window, width=e.width)

        self.hist_inner.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0,0), window=self.hist_inner, anchor="nw")
        canvas.bind("<Configure>", _on_canvas_resize)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def _on_mousewheel(e):
            canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self._populate_history()

    def _populate_history(self):
        """Rebuild the session history list from stats."""
        try:
            # Clear existing rows
            for widget in self.hist_inner.winfo_children():
                widget.destroy()

            sessions = self.stats.get("sessions", [])

            if not sessions:
                tk.Label(self.hist_inner, text="No sessions recorded yet.",
                         font=self.f_small, bg=C["bg_panel"],
                         fg=C["text_dim"]).pack(padx=16, pady=16)
                self.hist_count_var.set("0")
                self.hist_total_var.set("00:00:00")
                self.hist_longest_var.set("—")
                return

            # Update summary vars
            total_secs = sum(s["duration"] for s in sessions)
            longest    = max(s["duration"] for s in sessions)
            self.hist_count_var.set(str(len(sessions)))
            self.hist_total_var.set(fmt_time(total_secs))
            self.hist_longest_var.set(fmt_short(longest))

            # Column definitions: (header_text, minsize_px, fg_for_data)
            COL_DEFS = [
                ("#",        40,  C["text_dim"]),
                ("DATE",     160, C["text_mid"]),
                ("START",    110, C["mono"]),
                ("END",      110, C["mono"]),
                ("DURATION", 110, C["gold_lt"]),
            ]

            # Column headers
            hdr = tk.Frame(self.hist_inner, bg=C["bg_panel"])
            hdr.pack(fill="x", padx=8, pady=(6,2))
            for i, (text, px, _) in enumerate(COL_DEFS):
                hdr.columnconfigure(i, minsize=px, weight=1)
                tk.Label(hdr, text=text, font=self.f_label,
                         bg=C["bg_panel"], fg=C["text_dim"],
                         anchor="center").grid(row=0, column=i, sticky="ew", padx=2)

            tk.Frame(self.hist_inner, bg=C["border"], height=1).pack(fill="x", padx=8)

            # Session rows — newest first
            for i, sess in enumerate(reversed(sessions)):
                num     = len(sessions) - i
                start   = datetime.fromtimestamp(sess["start"]/1000)
                end     = datetime.fromtimestamp(sess["end"]/1000)
                dur     = fmt_short(sess["duration"])
                date_s  = start.strftime("%d %b %Y")
                start_s = start.strftime("%H:%M")
                end_s   = end.strftime("%H:%M")

                row_bg = C["bg_card"] if i % 2 == 0 else C["bg_panel"]
                row = tk.Frame(self.hist_inner, bg=row_bg)
                row.pack(fill="x", padx=8, pady=1)

                values = [str(num), date_s, start_s, end_s, dur]
                for j, (_, px, fg) in enumerate(COL_DEFS):
                    row.columnconfigure(j, minsize=px, weight=1)
                    tk.Label(row, text=values[j], font=self.f_small,
                             bg=row_bg, fg=fg,
                             anchor="center").grid(row=0, column=j, sticky="ew",
                                                   padx=2, pady=4)

        except Exception:
            pass

    def _confirm_history_reset(self):
        self._show_confirm("Reset Session History?",
                           "All session records will be cleared.\nTime totals are NOT affected.",
                           self._do_history_reset)

    def _do_history_reset(self):
        with self._lock:
            self.stats["sessions"] = []
            save_stats(self.stats)
        self._populate_history()

    # ─────────────────────────────────────────────────────────────────────────
    # STATS SUMMARY TAB
    # ─────────────────────────────────────────────────────────────────────────
    def _build_summary_page(self, parent):
        self._section_label(parent, "STATS AT A GLANCE", pady=(14,6))

        # 3-column grid of highlight cards
        grid = tk.Frame(parent, bg=C["bg_deep"]); grid.pack(fill="x", padx=18)
        for i in range(3): grid.columnconfigure(i, weight=1)

        self.sum_vars = {}
        cards = [
            # (key, label, sub, accent, row, col)
            ("sum_total_time",       "⏱  TOTAL PLAYTIME",      "All time",               C["gold_lt"], 0, 0),
            ("sum_week",             "◈  THIS WEEK",            "Since Sunday",           C["ice"],     0, 1),
            ("sum_avg",              "⬡  AVG SESSION",          "All sessions",           C["green"],   0, 2),
            ("sum_sessions",         "📋  SESSIONS",            "Total recorded",         C["purple"],  1, 0),
            ("sum_longest",          "★  LONGEST SESSION",      "All time best",          C["teal"],    1, 1),
            ("sum_player_kills",     "👤  PLAYER KILLS",        "FPS",                    C["red"],     1, 2),
            ("sum_ai_kills",         "🤖  AI KILLS",            "FPS",                    C["orange"],  2, 0),
            ("sum_player_ship_kills","🚀  PLAYER SHIP KILLS",   "Ship combat",            C["ice"],     2, 1),
            ("sum_ai_ship_kills",    "🛸  AI SHIP KILLS",       "Ship combat",            C["green"],   2, 2),
            ("sum_income",           "◈  TOTAL INCOME",         "All time",               C["green"],   3, 0),
            ("sum_expenses",         "◈  TOTAL EXPENSES",       "All time",               C["red"],     3, 1),
            ("sum_pl",               "◈  NET P&L",              "Income minus expenses",  C["text_mid"],3, 2),
        ]
        for key,lbl,sub,accent,r,c in cards:
            self.sum_vars[key] = tk.StringVar(value="—")
            card = self._card_frame(grid, accent, r, c)
            tk.Label(card, text=lbl, font=self.f_label,
                     bg=C["bg_card"], fg=C["text_mid"]).pack(anchor="w", padx=12, pady=(8,0))
            lbl_widget = tk.Label(card, textvariable=self.sum_vars[key],
                                  font=self.f_mono_s, bg=C["bg_card"], fg=accent)
            lbl_widget.pack(anchor="w", padx=12)
            # Store P&L label ref for dynamic colouring
            if key == "sum_pl":
                self.sum_pl_lbl = lbl_widget
            tk.Label(card, text=sub, font=self.f_small,
                     bg=C["bg_card"], fg=C["text_lo"]).pack(anchor="w", padx=12, pady=(0,8))

        # Server info strip at bottom
        self._div(parent, pad=6)
        srv_strip = tk.Frame(parent, bg=C["bg_panel"],
                             highlightbackground=C["border"], highlightthickness=1)
        srv_strip.pack(fill="x", padx=18, pady=(0,10))
        inner = tk.Frame(srv_strip, bg=C["bg_panel"], padx=14, pady=10)
        inner.pack(fill="x")
        tk.Label(inner, text="CURRENT SERVER", font=self.f_label,
                 bg=C["bg_panel"], fg=C["text_mid"]).pack(side="left")
        self.sum_server_var = tk.StringVar(value="—")
        tk.Label(inner, textvariable=self.sum_server_var, font=self.f_mono_s,
                 bg=C["bg_panel"], fg=C["ice"]).pack(side="right")

    # ─────────────────────────────────────────────────────────────────────────
    # KILLS TAB  — manual input with easter egg
    # ─────────────────────────────────────────────────────────────────────────
    def _build_kills_page(self, parent):
        self._section_label(parent, "FPS  /  CHARACTER KILLS", pady=(14,4))
        fps = tk.Frame(parent, bg=C["bg_deep"]); fps.pack(fill="x", padx=18)
        fps.columnconfigure(0, weight=1); fps.columnconfigure(1, weight=1)
        self.player_kills_var = tk.StringVar(value="0")
        self.ai_kills_var     = tk.StringVar(value="0")
        self._kill_input_card(fps, "👤  PLAYER KILLS",  C["red"],    self.player_kills_var,
                              lambda ev, ent: self._submit_kills(ev, ent, "player"), 0)
        self._kill_input_card(fps, "🤖  AI KILLS",       C["orange"], self.ai_kills_var,
                              lambda ev, ent: self._submit_kills(ev, ent, "ai"),     1)

        self._section_label(parent, "SHIP  /  VEHICLE KILLS", pady=(8,4))
        ships = tk.Frame(parent, bg=C["bg_deep"]); ships.pack(fill="x", padx=18)
        ships.columnconfigure(0, weight=1); ships.columnconfigure(1, weight=1)
        self.player_ship_kills_var = tk.StringVar(value="0")
        self.ai_ship_kills_var     = tk.StringVar(value="0")
        self._kill_input_card(ships, "🚀  PLAYER SHIP KILLS", C["ice"],   self.player_ship_kills_var,
                              lambda ev, ent: self._submit_kills(ev, ent, "player_ship"), 0)
        self._kill_input_card(ships, "🛸  AI SHIP KILLS",      C["green"], self.ai_ship_kills_var,
                              lambda ev, ent: self._submit_kills(ev, ent, "ai_ship"),    1)

        self._div(parent, pad=4)
        self._build_kills_footer(parent)

    def _kill_input_card(self, parent, label, accent, total_var, submit_fn, col):
        card = self._card_frame(parent, accent, 0, col)
        tk.Label(card, text=label, font=self.f_label,
                 bg=C["bg_card"], fg=C["text_mid"]).pack(anchor="w", padx=14, pady=(10,0))
        tk.Label(card, textvariable=total_var, font=self.f_num_xl,
                 bg=C["bg_card"], fg=accent).pack(anchor="w", padx=14)
        tk.Label(card, text="TOTAL", font=self.f_small,
                 bg=C["bg_card"], fg=C["text_lo"]).pack(anchor="w", padx=14)

        inp_row = tk.Frame(card, bg=C["bg_card"]); inp_row.pack(fill="x", padx=14, pady=(10,4))
        entry_var = tk.StringVar()
        entry = tk.Entry(inp_row, textvariable=entry_var, width=7, font=self.f_mono,
                         bg=C["bg_input"], fg=C["text_hi"], insertbackground=C["text_hi"],
                         highlightbackground=accent, highlightthickness=1,
                         relief="flat", justify="center")
        entry.pack(side="left", padx=(0,6))

        def validate(P):
            return P=="" or (P.isdigit() and 1<=len(P)<=4)
        vcmd = (card.register(validate), "%P")
        entry.config(validate="key", validatecommand=vcmd)
        entry.bind("<Return>", lambda e: submit_fn(entry_var, entry))

        btn = tk.Button(inp_row, text="ADD", font=self.f_btn,
                        bg=C["bg_card2"], fg=accent,
                        activebackground=accent, activeforeground=C["bg_deep"],
                        relief="flat", bd=0, cursor="hand2",
                        highlightbackground=accent, highlightthickness=1,
                        padx=10, pady=3,
                        command=lambda: submit_fn(entry_var, entry))
        btn.pack(side="left")

        # Inline reset button
        reset_key = {"👤  PLAYER KILLS":"player","🤖  AI KILLS":"ai",
                     "🚀  PLAYER SHIP KILLS":"player_ship","🛸  AI SHIP KILLS":"ai_ship"}.get(label,"player")
        rst = tk.Button(card, text="↺  RESET", font=self.f_btn,
                        bg=C["bg_card"], fg=C["red_dim"],
                        activebackground=C["red_dim"], activeforeground="#fff",
                        relief="flat", bd=0, cursor="hand2",
                        highlightbackground=C["red_dim"], highlightthickness=1,
                        padx=8, pady=3,
                        command=lambda k=reset_key: self._confirm_kills_reset(k))
        rst.pack(anchor="w", padx=14, pady=(2,12))
        rst.bind("<Enter>", lambda e,b=rst: b.config(fg=C["red"]))
        rst.bind("<Leave>", lambda e,b=rst: b.config(fg=C["red_dim"]))

    def _build_kills_footer(self, parent):
        foot = tk.Frame(parent, bg=C["bg_deep"], pady=10); foot.pack(fill="x", padx=18)
        self._danger_btn(foot, "☠   RESET ALL KILLS   ☠",
                         lambda: self._confirm_kills_reset("all"))
        tk.Label(foot, text="Kill stats only  ·  All other data unaffected",
                 font=self.f_small, bg=C["bg_deep"], fg=C["text_dim"]).pack(pady=(4,0))

    def _submit_kills(self, entry_var, entry, category):
        raw = entry_var.get().strip()
        if not raw or not raw.isdigit(): return
        value = int(raw)
        if value < 1 or value > 9999: return
        entry_var.set(""); entry.focus_set()
        if value >= 50:
            self._easter_egg_confirm(value, category)
        else:
            self._add_kills(value, category)

    def _easter_egg_confirm(self, value, category):
        dlg = tk.Toplevel(self.root); dlg.title("Really?")
        dlg.configure(bg=C["bg_deep"]); dlg.resizable(False,False)
        dlg.grab_set(); dlg.geometry("340x190"); dlg.transient(self.root)
        outer = tk.Frame(dlg, bg=C["gold_dim"], padx=1, pady=1)
        outer.pack(fill="both", expand=True, padx=12, pady=12)
        inner = tk.Frame(outer, bg=C["bg_deep"], padx=20, pady=16)
        inner.pack(fill="both", expand=True)
        tk.Label(inner, text=f"⚠  {value} kills?", font=self.f_label,
                 bg=C["bg_deep"], fg=C["gold_lt"]).pack()
        tk.Label(inner, text="Are you sure this is accurate?", font=self.f_sub,
                 bg=C["bg_deep"], fg=C["text_mid"]).pack(pady=10)
        btns = tk.Frame(inner, bg=C["bg_deep"]); btns.pack()
        def on_no(): dlg.destroy()
        def on_yes(): dlg.destroy(); self._grass_popup(value, category)
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
        dlg = tk.Toplevel(self.root); dlg.title("Noted")
        dlg.configure(bg=C["bg_deep"]); dlg.resizable(False,False)
        dlg.grab_set(); dlg.geometry("300x160"); dlg.transient(self.root)
        outer = tk.Frame(dlg, bg=C["green_dim"], padx=1, pady=1)
        outer.pack(fill="both", expand=True, padx=12, pady=12)
        inner = tk.Frame(outer, bg=C["bg_deep"], padx=20, pady=18)
        inner.pack(fill="both", expand=True)
        tk.Label(inner, text="🌿  Go touch grass", font=self.f_label,
                 bg=C["bg_deep"], fg=C["green"]).pack(pady=(0,14))
        def on_ok(): dlg.destroy(); self._add_kills(value, category)
        tk.Button(inner, text="OK", font=self.f_btn,
                  bg=C["green_dim"], fg=C["green"],
                  activebackground=C["green"], activeforeground=C["bg_deep"],
                  relief="flat", bd=0, highlightbackground=C["green"],
                  highlightthickness=1, padx=22, pady=5,
                  cursor="hand2", command=on_ok).pack()

    def _add_kills(self, value, category):
        with self._lock:
            key_map = {"player":"player_kills","ai":"ai_kills",
                       "player_ship":"player_ship_kills","ai_ship":"ai_ship_kills"}
            self.stats[key_map[category]] += value
            save_stats(self.stats)
        self._refresh_kills_ui()

    # ─────────────────────────────────────────────────────────────────────────
    # ECONOMY TAB
    # ─────────────────────────────────────────────────────────────────────────
    def _build_economy_page(self, parent):
        pnl = tk.Frame(parent, bg=C["bg_panel"],
                       highlightbackground=C["border"], highlightthickness=1)
        pnl.pack(fill="x", padx=18, pady=(12,4))
        inner = tk.Frame(pnl, bg=C["bg_panel"], padx=16, pady=12); inner.pack(fill="x")
        tk.Label(inner, text="LOG TRANSACTION  —  Enter aUEC amount and submit",
                 font=self.f_label, bg=C["bg_panel"], fg=C["text_mid"]).pack(anchor="w")
        rows = tk.Frame(inner, bg=C["bg_panel"]); rows.pack(fill="x", pady=(10,0))
        self.income_entry_var  = tk.StringVar()
        self.expense_entry_var = tk.StringVar()
        self._econ_input_row(rows, "INCOME  +",  C["green"], self.income_entry_var,
                             lambda: self._submit_econ("income"))
        self._econ_input_row(rows, "EXPENSE  −", C["red"],   self.expense_entry_var,
                             lambda: self._submit_econ("expense"))

        self._section_label(parent, "THIS SESSION")
        sg = tk.Frame(parent, bg=C["bg_deep"]); sg.pack(fill="x", padx=18)
        sg.columnconfigure(0,weight=1); sg.columnconfigure(1,weight=1); sg.columnconfigure(2,weight=1)
        self.sess_income_var  = tk.StringVar(value="0 aUEC")
        self.sess_expense_var = tk.StringVar(value="0 aUEC")
        self.sess_pl_var      = tk.StringVar(value="0 aUEC")
        self._econ_stat_card(sg,"SESSION INCOME",  C["green"],self.sess_income_var,
                             lambda: self._confirm_econ_reset("sess_income"),  0)
        self._econ_stat_card(sg,"SESSION EXPENSES",C["red"],  self.sess_expense_var,
                             lambda: self._confirm_econ_reset("sess_expense"), 1)
        self._econ_pl_card(sg,"SESSION P&L",self.sess_pl_var,2,session=True)

        self._section_label(parent, "CUMULATIVE TOTALS", pady=(8,4))
        cg = tk.Frame(parent, bg=C["bg_deep"]); cg.pack(fill="x", padx=18)
        cg.columnconfigure(0,weight=1); cg.columnconfigure(1,weight=1); cg.columnconfigure(2,weight=1)
        self.total_income_var  = tk.StringVar(value="0 aUEC")
        self.total_expense_var = tk.StringVar(value="0 aUEC")
        self.total_pl_var      = tk.StringVar(value="0 aUEC")
        self._econ_stat_card(cg,"TOTAL INCOME",  C["green"],self.total_income_var,
                             lambda: self._confirm_econ_reset("total_income"),  0)
        self._econ_stat_card(cg,"TOTAL EXPENSES",C["red"],  self.total_expense_var,
                             lambda: self._confirm_econ_reset("total_expense"), 1)
        self._econ_pl_card(cg,"TOTAL P&L",self.total_pl_var,2,session=False)

        self._div(parent, pad=4)
        foot = tk.Frame(parent, bg=C["bg_deep"], pady=10); foot.pack(fill="x", padx=18)
        self._danger_btn(foot, "◈   RESET ALL ECONOMY DATA   ◈",
                         lambda: self._confirm_econ_reset("all"))
        tk.Label(foot, text="Economy stats only  ·  All other data unaffected",
                 font=self.f_small, bg=C["bg_deep"], fg=C["text_dim"]).pack(pady=(4,0))

    def _econ_input_row(self, parent, label, accent, var, cmd):
        row = tk.Frame(parent, bg=C["bg_panel"]); row.pack(fill="x", pady=3)
        tk.Label(row, text=label, font=self.f_label, width=12,
                 bg=C["bg_panel"], fg=accent, anchor="w").pack(side="left")
        entry = tk.Entry(row, textvariable=var, width=16, font=self.f_mono,
                         bg=C["bg_input"], fg=C["text_hi"], insertbackground=C["text_hi"],
                         highlightbackground=accent, highlightthickness=1,
                         relief="flat", justify="right")
        entry.pack(side="left", padx=(0,8))
        entry.bind("<Return>", lambda e: cmd())
        def validate(P): return P=="" or (P.isdigit() and len(P)<=10)
        entry.config(validate="key", validatecommand=(parent.register(validate),"%P"))
        tk.Label(row, text="aUEC", font=self.f_small,
                 bg=C["bg_panel"], fg=C["text_dim"]).pack(side="left", padx=(0,8))
        btn = tk.Button(row, text="ADD", font=self.f_btn,
                        bg=C["bg_card2"], fg=accent,
                        activebackground=accent, activeforeground=C["bg_deep"],
                        relief="flat", bd=0, cursor="hand2",
                        highlightbackground=accent, highlightthickness=1,
                        padx=12, pady=3, command=cmd)
        btn.pack(side="left")

    def _econ_stat_card(self, parent, label, accent, var, reset_cmd, col):
        card = self._card_frame(parent, accent, 0, col)
        tk.Label(card, text=label, font=self.f_label,
                 bg=C["bg_card"], fg=C["text_mid"]).pack(anchor="w", padx=14, pady=(10,0))
        tk.Label(card, textvariable=var, font=self.f_num_l,
                 bg=C["bg_card"], fg=accent).pack(anchor="w", padx=14)
        tk.Label(card, text="aUEC", font=self.f_small,
                 bg=C["bg_card"], fg=C["text_lo"]).pack(anchor="w", padx=14)
        self._reset_btn(card, reset_cmd)

    def _econ_pl_card(self, parent, label, var, col, session=True):
        card = self._card_frame(parent, C["border_hi"], 0, col)
        tk.Label(card, text=label, font=self.f_label,
                 bg=C["bg_card"], fg=C["text_mid"]).pack(anchor="w", padx=14, pady=(10,0))
        lbl = tk.Label(card, textvariable=var, font=self.f_num_l,
                       bg=C["bg_card"], fg=C["text_mid"])
        lbl.pack(anchor="w", padx=14)
        tk.Label(card, text="aUEC", font=self.f_small,
                 bg=C["bg_card"], fg=C["text_lo"]).pack(anchor="w", padx=14)
        if session: self.sess_pl_card_lbl = lbl
        else:       self.total_pl_card_lbl = lbl
        tk.Label(card, text="Calculated automatically", font=self.f_small,
                 bg=C["bg_card"], fg=C["text_dim"]).pack(anchor="w", padx=14, pady=(0,10))

    # ─────────────────────────────────────────────────────────────────────────
    # SERVER TAB
    # ─────────────────────────────────────────────────────────────────────────
    def _build_server_page(self, parent):
        # Status panel
        pnl = tk.Frame(parent, bg=C["bg_panel"],
                       highlightbackground=C["border"], highlightthickness=1)
        pnl.pack(fill="x", padx=18, pady=(12,6))
        inner = tk.Frame(pnl, bg=C["bg_panel"], padx=16, pady=12); inner.pack(fill="x")

        tk.Label(inner, text="LOG FILE STATUS", font=self.f_label,
                 bg=C["bg_panel"], fg=C["text_mid"]).pack(anchor="w")

        row = tk.Frame(inner, bg=C["bg_panel"]); row.pack(fill="x", pady=(6,0))
        self.srv_log_dot = tk.Label(row, text="●", font=self.f_mono,
                                    bg=C["bg_panel"], fg=C["text_dim"])
        self.srv_log_dot.pack(side="left")
        self.srv_log_lbl = tk.Label(row, text="  SEARCHING FOR GAME.LOG",
                                    font=self.f_label, bg=C["bg_panel"], fg=C["text_mid"])
        self.srv_log_lbl.pack(side="left")

        self.srv_path_lbl = tk.Label(inner, text="", font=self.f_small,
                                     bg=C["bg_panel"], fg=C["text_dim"])
        self.srv_path_lbl.pack(anchor="w", pady=(3,0))

        path_row = tk.Frame(inner, bg=C["bg_panel"]); path_row.pack(fill="x", pady=(6,0))
        tk.Label(path_row, text="Log path:", font=self.f_small,
                 bg=C["bg_panel"], fg=C["text_mid"]).pack(side="left")
        self.srv_path_var = tk.StringVar(
            value=str(self._log_path) if self._log_path else "")
        tk.Entry(path_row, textvariable=self.srv_path_var, width=36,
                 font=self.f_small, bg=C["bg_input"], fg=C["text_hi"],
                 insertbackground=C["text_hi"],
                 highlightbackground=C["border"], highlightthickness=1,
                 relief="flat").pack(side="left", padx=(6,4))
        tk.Button(path_row, text="SET", font=self.f_small,
                  bg=C["bg_card2"], fg=C["gold_lt"],
                  activebackground=C["gold_dim"], activeforeground="#fff",
                  relief="flat", bd=0, cursor="hand2",
                  highlightbackground=C["gold_dim"], highlightthickness=1,
                  padx=8, pady=2,
                  command=self._set_manual_log_path).pack(side="left")

        self.srv_player_lbl = tk.Label(inner, text="Player: —", font=self.f_small,
                                       bg=C["bg_panel"], fg=C["ice_dim"])
        self.srv_player_lbl.pack(anchor="w", pady=(4,0))

        # Server info grid
        self._section_label(parent, "CURRENT SERVER INFO")
        grid = tk.Frame(parent, bg=C["bg_deep"]); grid.pack(fill="x", padx=18)
        grid.columnconfigure(0, weight=1); grid.columnconfigure(1, weight=1)

        self.srv_id_var       = tk.StringVar(value="—")
        self.srv_connects_var = tk.StringVar(value="0")

        info_cards = [
            ("SERVER ID",          self.srv_id_var,       C["ice"],   0, 0),
            ("SERVER CONNECTIONS", self.srv_connects_var, C["purple"],0, 1),
        ]
        for lbl,var,accent,r,c in info_cards:
            card = self._card_frame(grid, accent, r, c)
            tk.Label(card, text=lbl, font=self.f_label,
                     bg=C["bg_card"], fg=C["text_mid"]).pack(anchor="w", padx=14, pady=(10,0))
            tk.Label(card, textvariable=var, font=self.f_num_l,
                     bg=C["bg_card"], fg=accent).pack(anchor="w", padx=14)
            tk.Label(card, text="Auto-detected from log", font=self.f_small,
                     bg=C["bg_card"], fg=C["text_lo"]).pack(anchor="w", padx=14, pady=(0,10))

        # Session history
        self._section_label(parent, "SESSION CONNECTION LOG", pady=(8,4))
        log_frame = tk.Frame(parent, bg=C["bg_panel"],
                             highlightbackground=C["border"], highlightthickness=1)
        log_frame.pack(fill="x", padx=18, pady=(0,6))

        self.srv_log_text = tk.Text(log_frame, height=6, font=self.f_small,
                                    bg=C["bg_panel"], fg=C["text_lo"],
                                    insertbackground=C["text_hi"],
                                    relief="flat", state="disabled",
                                    wrap="word")
        self.srv_log_text.pack(fill="x", padx=8, pady=8)

        self._div(parent, pad=4)
        foot = tk.Frame(parent, bg=C["bg_deep"], pady=10); foot.pack(fill="x", padx=18)
        self._danger_btn(foot, "⬡   CLEAR CONNECTION LOG   ⬡",
                         lambda: self._confirm_srv_reset())
        tk.Label(foot, text="Clears session log only  ·  All other data unaffected",
                 font=self.f_small, bg=C["bg_deep"], fg=C["text_dim"]).pack(pady=(4,0))

    def _confirm_srv_reset(self):
        self._show_confirm("Clear Connection Log?",
                           "Session connection history will be cleared.\nThis cannot be undone.",
                           self._do_srv_reset)

    def _do_srv_reset(self):
        self._server_connects = 0
        self.srv_connects_var.set("0")
        self._append_srv_log("— Log cleared —")

    def _append_srv_log(self, text):
        try:
            self.srv_log_text.config(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            self.srv_log_text.insert("end", f"[{ts}]  {text}\n")
            self.srv_log_text.see("end")
            self.srv_log_text.config(state="disabled")
        except Exception: pass

    def _set_manual_log_path(self):
        p = Path(self.srv_path_var.get().strip())
        if p.exists():
            self._log_path = p; self._log_pos = 0
            self.cfg["log_path"] = str(p); save_cfg(self.cfg)
        else:
            self.srv_path_var.set("FILE NOT FOUND")

    # ─────────────────────────────────────────────────────────────────────────
    # ECONOMY LOGIC
    # ─────────────────────────────────────────────────────────────────────────
    def _submit_econ(self, kind):
        var = self.income_entry_var if kind=="income" else self.expense_entry_var
        raw = var.get().strip()
        if not raw or not raw.isdigit(): return
        amount = int(raw)
        if amount <= 0: return
        var.set("")
        with self._lock:
            if kind == "income":
                self.stats["session_income"]  += amount
                self.stats["total_income"]    += amount
            else:
                self.stats["session_expenses"] += amount
                self.stats["total_expenses"]   += amount
            save_stats(self.stats)
        self._refresh_economy_ui()

    def _confirm_econ_reset(self, which):
        labels = {"sess_income":"Session Income","sess_expense":"Session Expenses",
                  "total_income":"Total Income","total_expense":"Total Expenses",
                  "all":"All Economy Data"}
        self._show_confirm(f"Reset {labels.get(which,which)}?",
                           "This cannot be undone.\nAll other data unaffected.",
                           lambda: self._do_econ_reset(which))

    def _do_econ_reset(self, which):
        with self._lock:
            if which=="sess_income":   self.stats["session_income"]  = 0
            elif which=="sess_expense":self.stats["session_expenses"]= 0
            elif which=="total_income":self.stats["total_income"]    = 0
            elif which=="total_expense":self.stats["total_expenses"] = 0
            elif which=="all":
                self.stats["session_income"]=self.stats["session_expenses"]=0
                self.stats["total_income"]=self.stats["total_expenses"]=0
            save_stats(self.stats)
        self._refresh_economy_ui()

    # ─────────────────────────────────────────────────────────────────────────
    # RESET LOGIC — time & kills
    # ─────────────────────────────────────────────────────────────────────────
    def _confirm_reset(self, which):
        labels = {"total":"All-Time Total","avg":"Average Session Length"}
        self._show_confirm(f"Reset {labels.get(which,which)}?","This cannot be undone.",
                           lambda: self._do_time_reset(which))

    def _confirm_full_time_reset(self):
        self._show_confirm("RESET TIME TRACKER",
                           "All time stats will be wiped.\nAll other data unaffected.",
                           self._do_full_time_reset)

    def _do_time_reset(self, which):
        with self._lock:
            if which=="total": self.stats["total_seconds"]=0
            elif which=="avg": self.stats["sessions"]=[]
            save_stats(self.stats)
        self._refresh_ui()

    def _do_full_time_reset(self):
        with self._lock:
            preserved = {k: self.stats[k] for k in (
                "player_kills","ai_kills","player_ship_kills","ai_ship_kills",
                "session_income","session_expenses","total_income","total_expenses")}
            self.stats = dict(DEFAULT_STATS)
            self.stats.update(preserved)
            self.stats["today_date"]       = date.today().isoformat()
            self.stats["last_week_reset"]  = datetime.now().isoformat()
            self.stats["last_month_reset"] = datetime.now().isoformat()
            self.stats["last_year_reset"]  = datetime.now().isoformat()
            save_stats(self.stats)
        self._refresh_ui()

    def _confirm_kills_reset(self, which):
        labels = {"player":"Player Kills","ai":"AI Kills",
                  "player_ship":"Player Ship Kills","ai_ship":"AI Ship Kills","all":"All Kills"}
        self._show_confirm(f"Reset {labels.get(which,which)}?","This cannot be undone.",
                           lambda: self._do_kills_reset(which))

    def _do_kills_reset(self, which):
        with self._lock:
            keys = {"player":["player_kills"],"ai":["ai_kills"],
                    "player_ship":["player_ship_kills"],"ai_ship":["ai_ship_kills"],
                    "all":["player_kills","ai_kills","player_ship_kills","ai_ship_kills"]}
            for k in keys.get(which,[]): self.stats[k]=0
            save_stats(self.stats)
        self._refresh_kills_ui()

    # ─────────────────────────────────────────────────────────────────────────
    # THREADS
    # ─────────────────────────────────────────────────────────────────────────
    def _start_threads(self):
        threading.Thread(target=self._process_watcher, daemon=True).start()
        threading.Thread(target=self._tick_loop,       daemon=True).start()
        threading.Thread(target=self._log_watcher,     daemon=True).start()

    def _process_watcher(self):
        was = False
        while self._running:
            running = is_game_running()
            if running and not was:   self._on_game_start()
            elif not running and was: self._on_game_stop()
            was = running
            time.sleep(POLL_INTERVAL)

    def _tick_loop(self):
        while self._running:
            if self.tracking and self.session_start:
                self.session_secs = int(time.time() - self.session_start)
            time.sleep(1)

    def _log_watcher(self):
        _scanned_existing = False
        while self._running:
            if not self._log_path or not self._log_path.exists():
                self._resolve_log_path()
                if not self._log_path:
                    time.sleep(5); continue

            if not self._player_name:
                n = load_player_name_from_log(self._log_path)
                if n:
                    self._player_name = n
                    self.root.after(0, self._update_player_ui)

            try:
                # On first run, scan the existing log for server ID and player name
                # then set position to end so we only tail new lines going forward
                if not _scanned_existing:
                    _scanned_existing = True
                    try:
                        with open(self._log_path,"r",encoding="utf-8",errors="replace") as f:
                            for line in f:
                                result = parse_log_line_for_server(line, self._player_name)
                                if result and result["type"] == "server_id":
                                    # Take the last server ID found (most recent)
                                    self._server_id = result["value"]
                                elif result and result["type"] == "player_name":
                                    self._player_name = result["value"]
                        # Update UI with what we found
                        if self._server_id != "—":
                            self.root.after(0, self.srv_id_var.set, self._server_id)
                            self.root.after(0, self._append_srv_log,
                                           f"Session server: {self._server_id}")
                        if self._player_name:
                            self.root.after(0, self._update_player_ui)
                    except Exception:
                        pass
                    # Now seek to end for live tailing
                    self._log_pos = self._log_path.stat().st_size

                with open(self._log_path,"r",encoding="utf-8",errors="replace") as f:
                    f.seek(self._log_pos)
                    new_lines = f.readlines()
                    self._log_pos = f.tell()

                path_short = "..."+str(self._log_path)[-38:]
                self.root.after(0, self._update_log_status_ui, True, path_short)

                for line in new_lines:
                    result = parse_log_line_for_server(line, self._player_name)
                    if result:
                        self.root.after(0, self._handle_log_event, result)

            except Exception:
                self.root.after(0, self._update_log_status_ui, False, "")

            time.sleep(1)

    def _handle_log_event(self, event):
        t = event["type"]
        v = event["value"]

        if t == "player_name":
            self._player_name = v
            self._update_player_ui()

        elif t == "server_id":
            if v != self._server_id:
                self._server_id = v
                self._server_connects += 1
                self.srv_id_var.set(v)
                self.srv_connects_var.set(str(self._server_connects))
                self._append_srv_log(f"Connected → {v}")

        elif t == "server_reroute":
            # Fallback if no ShardId line found — just log the connection
            self._server_connects += 1
            self.srv_connects_var.set(str(self._server_connects))
            self._append_srv_log(f"Server connection detected")

    def _update_log_status_ui(self, found, path_short):
        try:
            color = C["green"] if found else C["red_dim"]
            text  = "  LOG MONITORING ACTIVE" if found else "  GAME.LOG NOT FOUND"
            self.srv_log_dot.config(fg=color)
            self.srv_log_lbl.config(text=text, fg=color)
            if found:
                self.srv_path_lbl.config(text=f"Log: {path_short}")
            else:
                self.srv_path_lbl.config(text="Set path manually below")
        except Exception: pass

    def _update_player_ui(self):
        try:
            txt = f"Player: {self._player_name}" if self._player_name else "Player: detecting..."
            clr = C["ice"] if self._player_name else C["ice_dim"]
            self.srv_player_lbl.config(text=txt, fg=clr)
        except Exception: pass

    def _on_game_start(self):
        with self._lock:
            self.session_start = time.time()
            self.session_secs  = 0
            self.tracking      = True
            self._log_pos      = 0
            self._player_name  = None
            self.stats["session_income"]   = 0
            self.stats["session_expenses"] = 0
            save_stats(self.stats)
        self.root.after(0, self._set_active_status)
        self.root.after(0, self._refresh_economy_ui)

    def _on_game_stop(self):
        with self._lock:
            if not self.tracking: return
            duration = int(time.time() - self.session_start)
            self.stats["sessions"].append({
                "start": int(self.session_start*1000),
                "end":   int(time.time()*1000),
                "duration": duration,
            })
            for k in ("total_seconds","week_seconds","month_seconds",
                      "year_seconds","today_seconds"):
                self.stats[k] += duration
            self.tracking = False
            self.session_start = None
            self.session_secs  = 0
            save_stats(self.stats)
        self.root.after(0, self._set_idle_status)
        self.root.after(0, self._update_last_session_label)
        self.root.after(0, self._populate_history)

    # ─────────────────────────────────────────────────────────────────────────
    # UI REFRESH
    # ─────────────────────────────────────────────────────────────────────────
    def _set_active_status(self):
        self.status_dot.config(fg=C["green"])
        self.status_lbl.config(text=" GAME ACTIVE", fg=C["green"])
        self.session_status_lbl.config(text="Tracking session...", fg=C["green"])

    def _set_idle_status(self):
        self.status_dot.config(fg=C["text_dim"])
        self.status_lbl.config(text=" AWAITING GAME", fg=C["text_mid"])
        self.session_status_lbl.config(text="Awaiting game launch...", fg=C["text_dim"])
        self.session_var.set("00:00:00")

    def _refresh_ui(self):
        with self._lock:
            s = self.stats
            live = self.session_secs if self.tracking else 0
        self.session_var.set(fmt_time(live))
        self.stat_vars["today"].set(fmt_time(s["today_seconds"]+live))
        self.stat_vars["week"].set( fmt_time(s["week_seconds"] +live))
        self.stat_vars["month"].set(fmt_time(s["month_seconds"]+live))
        self.stat_vars["year"].set( fmt_time(s["year_seconds"] +live))
        self.stat_vars["total"].set(fmt_time(s["total_seconds"]+live))
        sessions = s["sessions"]
        if sessions:
            avg = sum(x["duration"] for x in sessions)/len(sessions)
            self.stat_vars["avg"].set(fmt_short(avg))
            self.avg_sub_var.set(
                f"Across {len(sessions)} session{'s' if len(sessions)!=1 else ''}")
        else:
            self.stat_vars["avg"].set("—")
            self.avg_sub_var.set("No sessions yet")

    def _refresh_kills_ui(self):
        with self._lock: s = self.stats
        self.player_kills_var.set(str(s["player_kills"]))
        self.ai_kills_var.set(str(s["ai_kills"]))
        self.player_ship_kills_var.set(str(s["player_ship_kills"]))
        self.ai_ship_kills_var.set(str(s["ai_ship_kills"]))

    def _refresh_economy_ui(self):
        with self._lock: s = self.stats
        si=s["session_income"]; se=s["session_expenses"]
        ti=s["total_income"];   te=s["total_expenses"]
        spl=si-se; tpl=ti-te
        self.sess_income_var.set(fmt_auec(si))
        self.sess_expense_var.set(fmt_auec(se))
        self.sess_pl_var.set(fmt_auec(spl))
        self.total_income_var.set(fmt_auec(ti))
        self.total_expense_var.set(fmt_auec(te))
        self.total_pl_var.set(fmt_auec(tpl))
        try:
            self.sess_pl_card_lbl.config(fg=C["green"] if spl>=0 else C["red"])
            self.total_pl_card_lbl.config(fg=C["green"] if tpl>=0 else C["red"])
        except Exception: pass

    def _refresh_summary_ui(self):
        try:
            with self._lock:
                s    = self.stats
                live = self.session_secs if self.tracking else 0

            sessions = s["sessions"]
            total    = s["total_seconds"] + live
            today    = s["today_seconds"] + live
            week     = s["week_seconds"]  + live
            avg      = (sum(x["duration"] for x in sessions) / len(sessions)
                        if sessions else 0)
            longest  = max((x["duration"] for x in sessions), default=0)
            ti       = s["total_income"]
            te       = s["total_expenses"]
            tpl      = ti - te

            self.sum_vars["sum_total_time"].set(fmt_time(total))
            self.sum_vars["sum_week"].set(fmt_time(week))
            self.sum_vars["sum_avg"].set(fmt_short(avg) if avg else "—")
            self.sum_vars["sum_sessions"].set(str(len(sessions)))
            self.sum_vars["sum_longest"].set(fmt_short(longest) if longest else "—")
            self.sum_vars["sum_player_kills"].set(str(s["player_kills"]))
            self.sum_vars["sum_ai_kills"].set(str(s["ai_kills"]))
            self.sum_vars["sum_player_ship_kills"].set(str(s["player_ship_kills"]))
            self.sum_vars["sum_ai_ship_kills"].set(str(s["ai_ship_kills"]))
            self.sum_vars["sum_income"].set(fmt_auec(ti))
            self.sum_vars["sum_expenses"].set(fmt_auec(te))
            self.sum_vars["sum_pl"].set(fmt_auec(tpl))
            self.sum_server_var.set(self._server_id)
            try:
                self.sum_pl_lbl.config(
                    fg=C["green"] if tpl >= 0 else C["red"])
            except Exception: pass
        except Exception: pass

    def _schedule_refresh(self):
        if not self._running: return
        with self._lock:
            self.stats, changed = apply_auto_resets(self.stats)
            if changed: save_stats(self.stats)
        self._refresh_ui()
        self._refresh_kills_ui()
        self._refresh_economy_ui()
        self._refresh_summary_ui()
        self.root.after(2000, self._schedule_refresh)

    # ─────────────────────────────────────────────────────────────────────────
    # TRAY & LIFECYCLE
    # ─────────────────────────────────────────────────────────────────────────
    def _build_tray(self):
        try:
            import pystray
            img  = make_tray_icon_image()
            menu = pystray.Menu(
                pystray.MenuItem("Show", lambda: self.root.after(0, self.root.deiconify)),
                pystray.MenuItem("Quit", lambda: self.root.after(0, self._quit)),
            )
            self.tray = pystray.Icon("VerseTracker", img, "Verse Tracker", menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception: pass

    def _on_close(self): self._quit()

    def _quit(self):
        self._running = False
        if self.tracking: self._on_game_stop()
        try:
            if TRAY_AVAILABLE: self.tray.stop()
        except Exception: pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = VerseTracker()
    app.run()
