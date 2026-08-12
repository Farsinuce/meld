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
}
STAGE_ORDER = ("fetch", "prepare", "build", "save", "merge")

POLL_MS = 1000
CONSOLE_LINES = 8


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
    BAR_H = 44
    CONSOLE_H = 130

    def __init__(self, url: str, token: str = "") -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.cfg = load_settings()
        self.show_console = bool(self.cfg.get("console", False))
        self.console_source = self.cfg.get("console_source", "meld")
        self.cursor = 0
        self.locked = bool(self.cfg.get("locked", False))
        self._drag = None
        self._fails = 0

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
        try:
            root.attributes("-alpha", float(self.cfg.get("alpha", 0.96)))
        except Exception:
            pass

        w, h = self.WIDTH, self.BAR_H + (self.CONSOLE_H if self.show_console else 0)
        x = int(self.cfg.get("x", root.winfo_screenwidth() - w - 24))
        y = int(self.cfg.get("y", root.winfo_screenheight() - h - 80))
        root.geometry(f"{w}x{h}+{x}+{y}")

        self.canvas = tk.Canvas(root, width=w, height=self.BAR_H, bg=BG,
                                highlightthickness=1, highlightbackground=HAIR)
        self.canvas.pack(fill="x", side="top")

        self.console = tk.Text(root, height=8, bg="#08070500" if False else "#0d0c09",
                               fg="#c9c0aa", insertbackground=FG, relief="flat",
                               font=("Consolas", 8), wrap="none", padx=6, pady=4,
                               highlightthickness=1, highlightbackground=HAIR)
        self.console.configure(state="disabled")
        if self.show_console:
            self.console.pack(fill="both", expand=True, side="bottom")

        # Left-drag moves it (there is no title bar to grab); right-click is the whole menu.
        for widget in (self.canvas, self.console):
            widget.bind("<Button-3>", self.popup)
            widget.bind("<Button-1>", self.grab)
            widget.bind("<B1-Motion>", self.drag)
            widget.bind("<ButtonRelease-1>", self.drop)
        self.canvas.bind("<Double-Button-1>", lambda e: self._post("/api/open-ui"))

        self.menu = tk.Menu(root, tearoff=0, bg=PANEL, fg=FG,
                            activebackground=ACC, activeforeground="#241b05",
                            borderwidth=0, font=("Segoe UI", 9))
        self.menu.add_command(label="Open Meld", command=lambda: self._post("/api/open-ui"))
        self.menu.add_separator()
        self.consmenu = tk.Menu(self.menu, tearoff=0, bg=PANEL, fg=FG,
                                activebackground=ACC, activeforeground="#241b05")
        self.consmenu.add_command(label="Meld log", command=lambda: self.set_console("meld"))
        self.consmenu.add_command(label="Arnis output", command=lambda: self.set_console("arnis"))
        self.consmenu.add_command(label="Hide", command=lambda: self.set_console(None))
        self.menu.add_cascade(label="Console", menu=self.consmenu)
        self.menu.add_command(label="Stop render", command=lambda: self._post("/api/stop"))
        self.menu.add_separator()
        self.menu.add_command(label="Lock position", command=self.toggle_lock)
        # "Hide", not "Close": it is not going anywhere, and the tray icon brings it straight
        # back. Nothing here can stop a render.
        self.menu.add_command(label="Hide", command=self.close)
        return root

    # ── interaction ──────────────────────────────────────────────────────────
    def popup(self, ev):
        self.menu.entryconfigure(
            self.menu.index("Lock position"),
            label="Unlock position" if self.locked else "Lock position")
        try:
            self.menu.tk_popup(ev.x_root, ev.y_root)
        finally:
            self.menu.grab_release()

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
        h = self.BAR_H + (self.CONSOLE_H if self.show_console else 0)
        self.root.geometry(f"{self.WIDTH}x{h}")
        self.cfg.update(console=self.show_console, console_source=self.console_source)
        save_settings(self.cfg)

    def close(self):
        save_settings(self.cfg)
        try:
            self.root.destroy()
        except Exception:
            pass

    # ── painting ─────────────────────────────────────────────────────────────
    def paint(self, d: dict):
        c = self.canvas
        c.delete("all")
        w = self.WIDTH

        # The mark: three faces of the Meld block, flat-shaded. Same shape as the app icon.
        ox, oy, s = 10, 10, 11
        c.create_polygon(ox + s, oy, ox + 2 * s, oy + s * .55, ox + s, oy + s * 1.1,
                         ox, oy + s * .55, fill="#f7dc95", outline="")
        c.create_polygon(ox, oy + s * .55, ox + s, oy + s * 1.1, ox + s, oy + s * 2,
                         ox, oy + s * 1.45, fill=ACC, outline="")
        c.create_polygon(ox + 2 * s, oy + s * .55, ox + 2 * s, oy + s * 1.45, ox + s, oy + s * 2,
                         ox + s, oy + s * 1.1, fill="#a9720f", outline="")

        task = (d.get("task") or {})
        title = task.get("title") or "Idle"

        # Row 1: what it is doing, and the numbers, at opposite ends.
        c.create_text(42, 12, text=title, anchor="w", fill=FG, font=("Segoe UI Semibold", 10))
        right = []
        if d.get("total"):
            right.append(f"{d.get('done', 0)}/{d['total']}")
            if d.get("eta_s") is not None and d.get("active"):
                m = int(d["eta_s"]) // 60
                right.append(f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m")
        if d.get("failed"):
            right.append(f"{d['failed']} failed")
        c.create_text(w - 12, 12, text="  ".join(right), anchor="e", fill=MUT,
                      font=("Consolas", 8))

        # Row 2: the worker blocks on the right, the cell detail filling whatever is left. One
        # square per slot, coloured by stage - the shape of the pool, readable sideways, which
        # is the whole reason this bar exists.
        blocks = d.get("workers") or []
        bw, gap = 9, 3
        total_w = max(0, len(blocks) * (bw + gap) - gap)
        bx = w - 12 - total_w
        top = 24
        for i, wk in enumerate(blocks):
            colour = STAGE_COLOURS.get(wk.get("stage") or "idle", STAGE_COLOURS["idle"])
            x0 = bx + i * (bw + gap)
            c.create_rectangle(x0, top, x0 + bw, top + bw, fill=colour, outline="")
            # A quiet under-bar for how far that cell has got, so a stuck worker shows as a block
            # that keeps its colour AND stops filling.
            pct_w = max(0, min(100, int(wk.get("pct") or 0)))
            if pct_w:
                c.create_rectangle(x0, top + bw + 2, x0 + bw * pct_w / 100, top + bw + 3,
                                   fill=colour, outline="")

        # Truncated to the space actually left, measured rather than guessed at a character
        # count: "42,-17,2 · 43,-17,2" and "romania-north" are wildly different widths.
        detail = task.get("detail") or ""
        if detail:
            font = ("Consolas", 8)
            avail = bx - 10 - 42
            try:
                from tkinter import font as tkfont
                measure = tkfont.Font(font=font).measure
                while detail and measure(detail) > avail:
                    detail = detail[:-2]
                if detail != (task.get("detail") or ""):
                    detail = detail[:-1] + "…"
            except Exception:
                detail = detail[:40]
            c.create_text(42, 29, text=detail, anchor="w", fill=MUT, font=font)

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
            return
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
            self.paint(d)
        except Exception:
            self._fails += 1
            if self._fails > 2:
                self.paint_offline()
        self.pump_console()
        self.root.after(POLL_MS, self.tick)

    def run(self):
        root = self.build()
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
