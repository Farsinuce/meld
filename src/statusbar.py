"""The Meld status bar: a frameless strip you glance at while doing something else.

A window with a title bar and an X is the wrong shape for this. You do not "use" it, you keep it
in the corner of the screen for hours and look over at it - so it has no frame, no close button
and no taskbar button, sits above other windows, and is dismissed from its own right-click menu.
Nothing about it can be closed by accident, and closing it never stops a render.

Drawn with tkinter rather than in a browser window, for three reasons that all point the same
way: it is in the standard library, so a HUD costs no new dependency; a frameless always-on-top
window is one call (`overrideredirect`) where Chromium cannot do it from flags at all; and it
paints in milliseconds instead of starting a browser engine to draw eight coloured squares.

It runs as its OWN PROCESS (`Meld.exe --statusbar`), talking to the server over the same HTTP API
the web UI uses. That is not indirection for its own sake: tkinter and the tray icon both want to
own the main thread - macOS *requires* it for AppKit - and a separate process is the only
arrangement where neither has to give way. It also means a crash in the HUD cannot take the
render down with it.

The pool is the point. One block per worker, coloured by the stage that worker is in, so a
sideways glance answers "is it moving, and where is it stuck" without reading a word.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from collections import deque
from pathlib import Path

# Palette shared with the web UI (index.html's own values), plus one colour per worker stage.
BG = "#0b0a08"
PANEL = "#16130d"
FG = "#f0e9da"
MUT = "#9a9079"
ACC = "#e3a417"
ACC2 = "#f0bf3a"
HAIR = "#2a2620"

STAGE_COLOURS = {
    "idle":    "#2b2721",   # a slot with nothing in it - visible, but recedes
    "queued":  "#5a5140",
    "fetch":   "#4a86c5",   # pulling OSM / elevation: network, blue
    "prepare":  "#3fa9a0",  # ground + terrain prep: teal
    "build":   ACC,         # the gold: actually generating. "yellow when working"
    "save":    "#86b45a",   # writing regions: green
    "merge":   "#b7d17a",   # folding the cell into the master world
    "failed":  "#cf5a3e",
    # Governor-era stages. Both are kinds of waiting, and both are coloured as the neighbour they
    # wait on: admission is queued-but-alive (a worker the governor is holding back), and the
    # merge drain is the tail of merge, one shade paler.
    "waiting for admission": "#7b6f52",
    "finishing merges":      "#cfe3a0",
}
STAGE_ORDER = ("fetch", "prepare", "build", "save", "merge")

#: Shorter spellings a server might use for the same thing. Kept separate from STAGE_COLOURS so
#: the palette stays one colour per stage rather than one per synonym.
STAGE_ALIASES = {
    "admit":            "waiting for admission",
    "admission":        "waiting for admission",
    "waiting":          "waiting for admission",
    "finishing":        "finishing merges",
    "finishing merge":  "finishing merges",
    "draining":         "finishing merges",
    "drain":            "finishing merges",
}

#: Any stage this build has never heard of. Deliberately NOT the idle colour: a stage we cannot
#: name is not the same as an empty slot, and painting it as one would hide a whole phase of a
#: newer server behind "nothing is happening".
STAGE_UNKNOWN = "#6b6354"

#: Governor state -> colour. Moving states wear the working gold, a settled one goes green, and
#: anything held or unknown stays in the muted grey so it does not pull the eye.
GOV_COLOURS = {
    "WARMSTART": ACC2,
    "CALIBRATE": ACC2,
    "CONVERGE":  ACC2,
    "RECAL":     ACC2,
    "STEADY":    "#86b45a",
    "FROZEN":    "#3fa9a0",
}

POLL_MS = 1000
CONSOLE_LINES = 8
SITE = "meldmc.com"


def truncate_label(text: str, limit: int = 16) -> str:
    """Collapse whitespace and cut to `limit` characters, ellipsis included in the count.

    Used for text this build did not choose - a stage name invented by a newer server - so the
    only guarantee that matters is that it cannot run past the space budgeted for it.
    """
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"


def stage_style(stage) -> tuple[str, str]:
    """(colour, label) for one worker's stage.

    `label` is "" for every stage this build knows how to colour: the block says it all, and the
    bar stays wordless. For an unrecognised stage the block goes neutral and the raw name comes
    back truncated, so a newer server's vocabulary shows up as words rather than as a slot that
    silently looks idle. Never raises and never returns an empty colour - this runs once per
    worker per second inside the paint loop.
    """
    raw = " ".join(str(stage or "").split())
    # Separators are not vocabulary: "waiting_for_admission" and "waiting for admission" are the
    # same stage, and a build that painted one of them neutral-with-a-caption would be reporting
    # on punctuation. Normalised for lookup only - an unknown stage still shows its raw spelling.
    key = raw.lower().replace("_", " ").replace("-", " ")
    key = " ".join(key.split())
    if not key:
        return STAGE_COLOURS["idle"], ""
    if key in STAGE_COLOURS:
        return STAGE_COLOURS[key], ""
    alias = STAGE_ALIASES.get(key)
    if alias:
        return STAGE_COLOURS[alias], ""
    return STAGE_UNKNOWN, truncate_label(raw)


def gov_segment(mini: dict):
    """'gov 8→12' and the colour to draw it in, or None when there is nothing to say.

    None on three counts, all of which must paint exactly the bar that shipped before the
    governor existed: an older server that never sends the key, a malformed block, and
    governor_mode="off". A worker count identical to the target drops the arrow - "gov 8" - so
    the segment only shows a direction while the pool is actually moving towards one.
    """
    gov = (mini or {}).get("gov")
    if not isinstance(gov, dict):
        return None
    state = str(gov.get("state") or "").strip().upper()
    if state == "OFF":
        return None
    raw_w = gov.get("w", gov.get("workers"))
    try:
        workers = int(raw_w)
    except (TypeError, ValueError):
        return None
    try:
        target = int(gov.get("target"))
    except (TypeError, ValueError):
        target = workers
    text = f"gov {workers}" if target == workers else f"gov {workers}→{target}"
    return text, GOV_COLOURS.get(state, MUT)


def place_segments(segments, right_x: float, floor_x: float, measure, gap: int = 8):
    """Lay annotations out right-to-left from `right_x`, dropping any that would cross `floor_x`.

    Returns (placements, left_edge), where each placement is (text, colour, x) for an anchor="e"
    draw. The floor is what keeps this decorative: the segments annotate the worker blocks, so
    when a newer server sends a stage name long enough to reach the title, the RIGHT answer is
    that the segment goes away, not that it paints over the line telling you which cell is
    rendering. Right-to-left order also means the governor - always first in the list - is the
    last thing to be dropped.
    """
    placed = []
    x = float(right_x)
    for text, colour in segments:
        try:
            width = float(measure(text))
        except Exception:
            width = len(str(text)) * 6.0
        if x - width < floor_x:
            break
        placed.append((text, colour, x))
        x -= width + gap
    return placed, (x + gap if placed else float(right_x))


def _settings_path() -> Path:
    from .paths import data_dir
    return data_dir() / "statusbar.json"


def load_settings() -> dict:
    try:
        return json.loads(_settings_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(d: dict) -> None:
    try:
        p = _settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except Exception:
        pass


class StatusBar:
    """One strip: mark, task, worker blocks, progress, and an optional console drawer."""

    WIDTH = 430
    BAR_H = 78          # rows at 18 / 38 / 60, with matching space above and below
    GRAPH_H = 78
    CONSOLE_H = 130
    # Left limit for the annotation segments: the title and the cell detail both start at x=50,
    # and they outrank anything the segments have to say.
    SEG_FLOOR_X = 56

    def __init__(self, url: str, token: str = "") -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.cfg = load_settings()
        self.show_console = bool(self.cfg.get("console", False))
        self.console_source = self.cfg.get("console_source", "meld")
        self.cursor = 0
        self.locked = bool(self.cfg.get("locked", False))
        self.alpha = float(self.cfg.get("alpha", 1.0))
        self.show_graph = bool(self.cfg.get("graph", False))
        self._drag = None
        self._fails = 0
        self._icon_img = None
        # Mirrored off /api/mini each tick. Empty until the server's background check has run, so
        # no pill and no menu item for the first few seconds - the right failure. A surface that
        # flashed a notice on every launch would be trained away inside a week.
        self.update_state = ""
        self.update_latest = ""
        # One sample per tick. 120 of them is two minutes of history at a 1 s poll, which is the
        # span where "did that spike settle" is still a question worth answering.
        self.history: deque = deque(maxlen=120)

    # ── server ───────────────────────────────────────────────────────────────
    def _get(self, path: str) -> dict:
        req = urllib.request.Request(self.url + path)
        if self.token:
            req.add_header("X-Meld-Token", self.token)
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read() or b"{}")

    def _post(self, path: str) -> None:
        req = urllib.request.Request(self.url + path, data=b"", method="POST")
        req.add_header("Origin", self.url)
        if self.token:
            req.add_header("X-Meld-Token", self.token)
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass

    # ── window ───────────────────────────────────────────────────────────────
    def build(self):
        import tkinter as tk

        self.tk = tk
        root = tk.Tk()
        self.root = root
        root.title("Meld")
        # No frame, no title bar, no X, and no taskbar button. The only way to dismiss it is its
        # own menu, so it cannot be closed by reflex while a six-hour render is running.
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=BG)
        self.apply_alpha(self.alpha)

        # The real app icon, not a drawing of it. tkinter reads PNG directly (Tk 8.6+), so this
        # costs no image library at runtime and the bar always wears exactly what the exe wears.
        for name in ("meld-24.png", "meld-32.png", "meld-16.png"):
            try:
                from .paths import resource
                p = resource("assets", "icons", name)
                if p.is_file():
                    img = tk.PhotoImage(file=str(p))
                    if img.width() > 26:                    # subsample, never scale up: keeping
                        img = img.subsample(2, 2)           # whole pixels keeps the edges crisp
                    self._icon_img = img
                    break
            except Exception:
                continue

        w = self.WIDTH
        h = (self.BAR_H + (self.GRAPH_H if self.show_graph else 0)
             + (self.CONSOLE_H if self.show_console else 0))
        x = int(self.cfg.get("x", root.winfo_screenwidth() - w - 24))
        y = int(self.cfg.get("y", root.winfo_screenheight() - h - 80))
        root.geometry(f"{w}x{h}+{x}+{y}")

        self.canvas = tk.Canvas(root, width=w, height=self.BAR_H, bg=BG,
                                highlightthickness=1, highlightbackground=HAIR)
        self.canvas.pack(fill="x", side="top")

        self.graph = tk.Canvas(root, width=w, height=self.GRAPH_H, bg="#0d0c09",
                               highlightthickness=1, highlightbackground=HAIR)
        if self.show_graph:
            self.graph.pack(fill="x", side="top")

        self.console = tk.Text(root, height=8, bg="#08070500" if False else "#0d0c09",
                               fg="#c9c0aa", insertbackground=FG, relief="flat",
                               font=("Consolas", 8), wrap="none", padx=6, pady=4,
                               highlightthickness=1, highlightbackground=HAIR)
        self.console.configure(state="disabled")
        if self.show_console:
            self.console.pack(fill="both", expand=True, side="bottom")

        # Left-drag moves it (there is no title bar to grab); right-click is the whole menu.
        for widget in (self.canvas, self.graph, self.console):
            widget.bind("<Button-3>", self.popup)
            widget.bind("<Button-1>", self.grab)
            widget.bind("<B1-Motion>", self.drag)
            widget.bind("<ButtonRelease-1>", self.drop)
        self.canvas.bind("<Double-Button-1>", lambda e: self.open_meld())

        # There used to be a second, tk.Menu-based copy of the right-click menu here. Nothing
        # opened it: right-click binds to popup() above, which draws its own Toplevel for the
        # reason given in that method's docstring. It was dead from the moment popup() replaced
        # it, and it had already drifted out of sync with the live menu's item order - so an edit
        # aimed at "the menu" had even odds of landing in the copy nobody sees. Deleted.
        return root

    def apply_alpha(self, value: float) -> None:
        try:
            self.root.attributes("-alpha", max(0.15, min(1.0, float(value))))
        except Exception:
            pass

    def set_alpha(self, value: float) -> None:
        self.alpha = value
        self.apply_alpha(value)
        self.cfg["alpha"] = value
        save_settings(self.cfg)

    def open_meld(self, *_):
        """Open the full Meld UI. The server opens the window, so it can attach this session's
        token; the bar has no way to put a token into a window it does not create."""
        self._post("/api/open-ui")

    def open_site(self, *_):
        import webbrowser
        try:
            webbrowser.open(f"https://{SITE}")
        except Exception:
            pass

    def open_update(self, *_):
        """Open the Meld UI, where the update panel lives.

        Deliberately not "start downloading 70 MB". The pill is a few pixels tall in a strip that
        people drag around by grabbing anywhere on it, so a mis-click has to be free; committing
        to a download from a target that small would be the wrong bargain. The UI asks first.
        """
        self._post("/api/open-ui")

    def toggle_graph(self, *_):
        self.show_graph = not self.show_graph
        if self.show_graph:
            # before=console keeps the order stable: bar, graph, console.
            self.graph.pack(fill="x", side="top", after=self.canvas)
        else:
            self.graph.pack_forget()
        self.resize()
        self.cfg["graph"] = self.show_graph
        save_settings(self.cfg)

    def resize(self):
        h = (self.BAR_H
             + (self.GRAPH_H if self.show_graph else 0)
             + (self.CONSOLE_H if self.show_console else 0))
        self.root.geometry(f"{self.WIDTH}x{h}")

    def paint_graph(self):
        """CPU and RAM over the last two minutes.

        A number tells you the instant; a line tells you the shape - whether a render is holding
        the machine flat out, ramping, or stalling between cells. That shape is the reason to
        look at a status bar at all while something long is running.
        """
        if not self.show_graph:
            return
        g = self.graph
        g.delete("all")
        w, h = self.WIDTH, self.GRAPH_H
        # Room to breathe: the plot is inset on every side so the traces never touch the frame
        # and the axis labels are not crammed against it.
        pad_l, pad_r, pad_t, pad_b = 30, 62, 12, 14
        plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
        right = w - pad_r

        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = pad_t + plot_h * (1 - frac)
            major = frac in (0.0, 0.5, 1.0)
            g.create_line(pad_l, y, right, y, fill="#221e17" if major else "#171410")
            if major:
                g.create_text(pad_l - 6, y, text=f"{int(frac * 100)}", anchor="e",
                              fill=MUT, font=("Consolas", 6))

        if len(self.history) < 2:
            g.create_text((pad_l + right) / 2, h / 2, text="sampling…", fill=MUT,
                          font=("Consolas", 7))
            return

        n = len(self.history)
        step = plot_w / max(1, self.history.maxlen - 1)
        # Right-aligned: the newest sample sits at the right edge and the trace grows leftwards,
        # so "now" never moves while the window fills up.
        x0 = right - (n - 1) * step

        labels = []
        for idx, colour, label in ((0, ACC, "CPU"), (1, "#3fa9a0", "RAM")):
            pts = []
            for i, sample in enumerate(self.history):
                v = sample[idx]
                if v is None:
                    continue
                pts += [x0 + i * step, pad_t + plot_h * (1 - min(100, max(0, v)) / 100)]
            if len(pts) < 4:
                continue
            g.create_line(*pts, fill=colour, width=1, smooth=False)
            cur = self.history[-1][idx]
            if cur is not None:
                labels.append([pad_t + plot_h * (1 - min(100, max(0, cur)) / 100),
                               colour, f"{label} {int(cur)}%"])

        # The current values park in the gutter on the trace's own line, so the two are read
        # together. When the traces cross, the labels would print on top of each other, so they
        # get pushed apart - the leader line still says which is which.
        labels.sort(key=lambda item: item[0])
        for i in range(1, len(labels)):
            gap = labels[i][0] - labels[i - 1][0]
            if gap < 10:
                labels[i][0] = labels[i - 1][0] + 10
        for y, colour, text in labels:
            y = max(pad_t, min(pad_t + plot_h, y))
            g.create_line(right, y, right + 5, y, fill=colour)
            g.create_text(right + 9, y, text=text, anchor="w", fill=colour,
                          font=("Consolas", 7))

        g.create_text(pad_l, h - 4, text=f"{n}s", anchor="w", fill="#3a352b",
                      font=("Consolas", 6))

    # ── interaction ──────────────────────────────────────────────────────────
    def popup(self, ev):
        """A menu we draw ourselves, instead of tk_popup.

        Tk's popup on Windows is wrapped in a system frame that stays bright whatever bg you
        give the widget - a white rectangle hanging off a black bar. A borderless Toplevel with
        our own hairline is the only way to control that edge, and it costs about as much code
        as configuring the one we cannot fix.
        """
        tk = self.tk
        self.dismiss_menu()

        win = tk.Toplevel(self.root)
        self._menu_win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=HAIR)                       # the 1px hairline, as the outer background
        inner = tk.Frame(win, bg=PANEL)
        inner.pack(padx=1, pady=1)                   # ... and the panel inset inside it

        def row(label, command=None, indent=False):
            if label is None:
                tk.Frame(inner, bg=HAIR, height=1).pack(fill="x", pady=3)
                return
            item = tk.Label(inner, text=("   " + label) if indent else label, anchor="w",
                            bg=PANEL, fg=MUT if indent else FG, font=("Segoe UI", 9),
                            padx=14, pady=4)
            item.pack(fill="x")
            item.bind("<Enter>", lambda _e: item.configure(bg=ACC, fg="#241b05"))
            item.bind("<Leave>", lambda _e: item.configure(bg=PANEL, fg=MUT if indent else FG))
            if command:
                item.bind("<Button-1>", lambda _e: (self.dismiss_menu(), command()))

        row("Open Meld", self.open_meld)
        row(None)
        # Order: the two view toggles first, then the appearance group, then the actions. The
        # graph leads because it is the one item people hit repeatedly.
        row("CPU / RAM graph", self.toggle_graph)
        # Submenus are flattened: at this size a cascade is more clicks and more chrome than the
        # three items it would hide.
        row("Console", None)
        row("Meld log", lambda: self.set_console("meld"), indent=True)
        row("Arnis output", lambda: self.set_console("arnis"), indent=True)
        row("Hidden", lambda: self.set_console(None), indent=True)
        row(None)
        row("Opacity", None)
        for label, value in (("Solid", 1.0), ("75%", 0.75), ("50%", 0.5)):
            row(label, lambda v=value: self.set_alpha(v), indent=True)
        row(None)
        # Only when there is one, and it opens the panel rather than starting a download - see
        # the note on the bottom-row pill.
        if self.update_state == "available" and self.update_latest:
            row(f"Update to {self.update_latest}…", self.open_update)
        row("Stop render", lambda: self._post("/api/stop"))
        row("Unlock position" if self.locked else "Lock position", self.toggle_lock)
        row("Hide", self.close)

        win.update_idletasks()
        # Keep it on screen: near the bottom-right of a display, a menu dropped at the cursor
        # would hang off the edge, and this bar lives in exactly that corner.
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = min(ev.x_root, win.winfo_screenwidth() - w - 4)
        y = ev.y_root
        if y + h > win.winfo_screenheight() - 4:
            y = max(0, ev.y_root - h)
        win.geometry(f"+{int(x)}+{int(y)}")
        win.bind("<Escape>", lambda _e: self.dismiss_menu())
        win.focus_force()
        win.bind("<FocusOut>", lambda _e: self.dismiss_menu())

    def dismiss_menu(self, *_):
        win, self._menu_win = getattr(self, "_menu_win", None), None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def grab(self, ev):
        self._drag = (ev.x_root - self.root.winfo_x(), ev.y_root - self.root.winfo_y())

    def drag(self, ev):
        if self._drag is None or self.locked:
            return
        self.root.geometry(f"+{ev.x_root - self._drag[0]}+{ev.y_root - self._drag[1]}")

    def drop(self, _ev):
        self._drag = None
        self.cfg.update(x=self.root.winfo_x(), y=self.root.winfo_y())
        save_settings(self.cfg)

    def toggle_lock(self):
        self.locked = not self.locked
        self.cfg["locked"] = self.locked
        save_settings(self.cfg)

    def set_console(self, source: str | None):
        self.show_console = source is not None
        if source:
            if source != self.console_source:
                self.cursor = 0                      # switching feeds restarts the tail
                self.console.configure(state="normal")
                self.console.delete("1.0", "end")
                self.console.configure(state="disabled")
            self.console_source = source
            self.console.pack(fill="both", expand=True, side="bottom")
        else:
            self.console.pack_forget()
        self.resize()
        self.cfg.update(console=self.show_console, console_source=self.console_source)
        save_settings(self.cfg)

    def close(self):
        # Remembered, so hiding it is a decision that survives a restart rather than something
        # you have to redo every launch. The tray's Show item brings it back and sets this again.
        self.cfg["visible"] = False
        save_settings(self.cfg)
        try:
            self.root.destroy()
        except Exception:
            pass

    # ── painting ─────────────────────────────────────────────────────────────
    def _text_w(self, text: str, font) -> int:
        """Pixel width of `text`, falling back to a per-character estimate.

        The fallback exists because a font object cannot be built before Tk has a root, and a
        paint that raised there would take the whole bar down over a label nobody asked for.
        """
        try:
            from tkinter import font as tkfont
            return int(tkfont.Font(font=font).measure(text))
        except Exception:
            return int(len(text) * 6)

    def paint(self, d: dict):
        c = self.canvas
        c.delete("all")
        w = self.WIDTH

        # The mark is the real app icon, and it is a BUTTON: clicking it opens Meld. That is the
        # obvious place to reach for, and it saves the right-click menu for everything else.
        if self._icon_img is not None:
            c.create_image(14, 18, image=self._icon_img, anchor="nw", tags="openbtn")
        else:
            # Flat-shaded fallback, same silhouette, for a build whose icon files are missing.
            ox, oy, s = 14, 18, 11
            c.create_polygon(ox + s, oy, ox + 2 * s, oy + s * .55, ox + s, oy + s * 1.1,
                             ox, oy + s * .55, fill="#f7dc95", outline="", tags="openbtn")
            c.create_polygon(ox, oy + s * .55, ox + s, oy + s * 1.1, ox + s, oy + s * 2,
                             ox, oy + s * 1.45, fill=ACC, outline="", tags="openbtn")
            c.create_polygon(ox + 2 * s, oy + s * .55, ox + 2 * s, oy + s * 1.45, ox + s,
                             oy + s * 2, ox + s, oy + s * 1.1, fill="#a9720f", outline="",
                             tags="openbtn")
        c.tag_bind("openbtn", "<Button-1>", lambda _e: self.open_meld())

        task = (d.get("task") or {})
        title = task.get("title") or "Idle"

        # Row 1: what it is doing on the left, what the machine is doing on the right, directly
        # above the worker blocks those numbers explain.
        c.create_text(50, 18, text=title, anchor="w", fill=FG, font=("Segoe UI Semibold", 10))
        st = d.get("stats") or {}
        load = []
        if st.get("cpu_pct") is not None:
            load.append(f"CPU {int(st['cpu_pct'])}%")
        if st.get("ram_pct") is not None:
            load.append(f"RAM {int(st['ram_pct'])}%")
        if st.get("disk_free_gb") is not None:
            load.append(f"{int(st['disk_free_gb'])}G free")
        hot = (st.get("cpu_pct") or 0) >= 85 or (st.get("ram_pct") or 0) >= 85
        c.create_text(w - 14, 18, text="   ".join(load), anchor="e",
                      fill=ACC2 if hot else MUT, font=("Consolas", 8))

        # Row 3: the run's own numbers. Cells done, how long it has been going, what is left.
        counts = []
        if d.get("total"):
            counts.append(f"{d.get('done', 0)}/{d['total']} cells")
            if d.get("failed"):
                counts.append(f"{d['failed']} failed")
            if d.get("eta_s") is not None and d.get("active"):
                m = int(d["eta_s"]) // 60
                counts.append(f"eta {m // 60}h {m % 60}m" if m >= 60 else f"eta {m}m")
        if counts:
            c.create_text(14, self.BAR_H - 20, text="   ".join(counts), anchor="w",
                          fill=MUT, font=("Consolas", 7))
        elif self.update_state == "available" and self.update_latest:
            # Only when idle, and only in the space the cell counts would otherwise hold. During
            # a render that line is telling the user how their six-hour job is going; a version
            # notice does not get to compete with it, and it will still be here afterwards.
            c.create_text(14, self.BAR_H - 20, text=f"▲ Update {self.update_latest}",
                          anchor="w", fill=ACC, font=("Consolas", 8), tags="upd")
            c.tag_bind("upd", "<Button-1>", lambda _e: self.open_update())
            c.tag_bind("upd", "<Enter>",
                       lambda _e: (c.itemconfigure("upd", fill=FG), c.configure(cursor="hand2")))
            c.tag_bind("upd", "<Leave>",
                       lambda _e: (c.itemconfigure("upd", fill=ACC), c.configure(cursor="")))

        # The site, quietly, on the bottom line under the worker blocks. It is the one surface
        # that sits on screen for hours, so it is where a wordmark actually earns its place -
        # and it is a link: clicking opens meldmc.com.
        c.create_text(w - 14, self.BAR_H - 20, text=SITE, anchor="e", fill=MUT,
                      font=("Consolas", 8), tags="site")
        c.tag_bind("site", "<Button-1>", lambda _e: self.open_site())
        c.tag_bind("site", "<Enter>",
                   lambda _e: (c.itemconfigure("site", fill=ACC2), c.configure(cursor="hand2")))
        c.tag_bind("site", "<Leave>",
                   lambda _e: (c.itemconfigure("site", fill=MUT), c.configure(cursor="")))

        # Row 2: the worker blocks on the right, the cell detail filling whatever is left. One
        # square per slot, coloured by stage - the shape of the pool, readable sideways, which
        # is the whole reason this bar exists.
        blocks = d.get("workers") or []
        bw, gap, per_row = 13, 4, 8
        rows = (len(blocks) + per_row - 1) // per_row or 1
        row_len = min(per_row, len(blocks)) or 1
        total_w = max(0, row_len * (bw + gap) - gap)
        bx = w - 14 - total_w
        top = 31 - (rows - 1) * 5          # two rows of eight straddle the same middle band
        unknown: list[str] = []
        for i, wk in enumerate(blocks):
            colour, label = stage_style(wk.get("stage"))
            if label and label not in unknown:
                unknown.append(label)
            col, row = i % per_row, i // per_row
            x0 = bx + col * (bw + gap)
            y0 = top + row * (bw + 5)
            c.create_rectangle(x0, y0, x0 + bw, y0 + bw, fill=colour, outline="")
            # A quiet under-bar for how far that cell has got, so a stuck worker shows as a block
            # that keeps its colour AND stops filling.
            pct_w = max(0, min(100, int(wk.get("pct") or 0)))
            if pct_w:
                c.create_rectangle(x0, y0 + bw + 1, x0 + bw * pct_w / 100, y0 + bw + 2,
                                   fill=colour, outline="")

        # Between the cell detail and the blocks: the segments that annotate the pool. Drawn
        # right-to-left from the cluster so they stay attached to what they describe, and
        # measured, so whatever is left over is what the detail gets - the same bargain the
        # detail already made with the blocks. Nothing here draws when the server sends neither
        # a governor block nor an unfamiliar stage, which is every build before this one.
        seg_font = ("Consolas", 7)
        band_y = top + (rows * (bw + 5) - 5) / 2
        segments = []
        gov = gov_segment(d)
        if gov:
            segments.append(gov)
        if unknown:
            # Two at most: past that the bar would be reporting on the server's vocabulary rather
            # than on the render, and the detail has a better claim on the space.
            segments.append((" ".join(unknown[:2]), STAGE_UNKNOWN))
        placed, edge = place_segments(segments, bx - 8, self.SEG_FLOOR_X,
                                      lambda t: self._text_w(t, seg_font))
        for text, colour, x in placed:
            c.create_text(x, band_y, text=text, anchor="e", fill=colour, font=seg_font)
        left_edge = edge if placed else bx

        # Truncated to the space actually left, measured rather than guessed at a character
        # count: "42,-17,2 · 43,-17,2" and "romania-north" are wildly different widths.
        detail = task.get("detail") or ""
        if detail:
            font = ("Consolas", 8)
            avail = left_edge - 12 - 50
            try:
                from tkinter import font as tkfont
                measure = tkfont.Font(font=font).measure
                while detail and measure(detail) > avail:
                    detail = detail[:-2]
                if detail != (task.get("detail") or ""):
                    detail = detail[:-1] + "…"
            except Exception:
                detail = detail[:40]
            c.create_text(50, 38, text=detail, anchor="w", fill=MUT, font=font)

        # Overall progress: a hairline the full width, so it reads from the corner of an eye.
        pct = task.get("pct")
        if pct is None:
            pct = d.get("percent") if d.get("total") else 0
        c.create_rectangle(0, self.BAR_H - 4, w, self.BAR_H - 1, fill="#141210", outline="")
        if pct:
            c.create_rectangle(0, self.BAR_H - 4, w * float(pct) / 100.0, self.BAR_H - 1,
                               fill=ACC2 if d.get("active") else "#4a4335", outline="")

    def paint_offline(self):
        c = self.canvas
        c.delete("all")
        c.create_text(14, self.BAR_H / 2, text="Meld is not running", anchor="w",
                      fill="#cf5a3e", font=("Segoe UI", 9))

    def pump_console(self):
        if not self.show_console:
            return
        try:
            d = self._get(f"/api/console?source={self.console_source}&since={self.cursor}")
        except Exception:
            return
        self.cursor = d.get("next", self.cursor)
        lines = d.get("lines") or []
        if not lines:
            # An idle server has genuinely logged nothing yet, and an empty black panel reads as
            # broken rather than quiet. Say which it is.
            if not self.console.get("1.0", "end-1c").strip():
                self.console.configure(state="normal")
                self.console.insert("end", f"(no {self.console_source} output yet)\n")
                self.console.configure(state="disabled")
            return
        if self.console.get("1.0", "end-1c").startswith("(no "):
            self.console.configure(state="normal")
            self.console.delete("1.0", "end")
            self.console.configure(state="disabled")
        self.console.configure(state="normal")
        for line in lines:
            self.console.insert("end", line + "\n")
        # Trim from the top so an overnight run cannot grow the widget without bound.
        excess = int(self.console.index("end-1c").split(".")[0]) - 400
        if excess > 0:
            self.console.delete("1.0", f"{excess}.0")
        self.console.see("end")
        self.console.configure(state="disabled")

    def tick(self):
        try:
            d = self._get("/api/mini")
            self._fails = 0
            u = d.get("update") or {}
            self.update_state = u.get("state") or ""
            self.update_latest = u.get("latest") or ""
            st = d.get("stats") or {}
            self.history.append((st.get("cpu_pct"), st.get("ram_pct")))
            self.paint(d)
        except Exception:
            self._fails += 1
            if self._fails > 2:
                self.paint_offline()
        self.paint_graph()
        self.pump_console()
        self.root.after(POLL_MS, self.tick)

    def run(self):
        root = self.build()
        self.cfg["visible"] = True
        save_settings(self.cfg)
        self.tick()
        root.mainloop()


def main(url: str, token: str = "") -> int:
    try:
        import tkinter  # noqa: F401
    except Exception as ex:                                   # noqa: BLE001
        print(f"[statusbar] tkinter is unavailable ({ex}); on Linux install python3-tk")
        return 1
    # One bar, ever. It has no taskbar button and no close box, so a second copy is invisible
    # except as slightly bolder text where the two overlap - which is how a stacked pair went
    # unnoticed during testing until the text looked wrong.
    #
    # (Counting processes is misleading here: a Windows venv's pythonw.exe is a trampoline that
    # runs the base interpreter as a CHILD, so every bar shows up as two processes. The number
    # of Tk windows is the honest count.)
    from .single_instance import SingleInstance

    lock = SingleInstance("statusbar.lock")
    if not lock.acquire():
        print("[statusbar] already running")
        return 0
    try:
        StatusBar(url, token).run()
    finally:
        lock.release()
    return 0
