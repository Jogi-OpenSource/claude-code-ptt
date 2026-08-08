"""Floating always-on-top window listing all registered Claude Code sessions.

One row per session; clicking a row pins it as the PTT target, clicking
"Auto" returns to focus tracking. The target row mirrors the PTT state:
  recording      red dot + red frame on the row the text will land in
  transcribing   the row text "breathes" orange (soft pulse, no blinking)
  landed         the whole row flashes orange for one second, then normal

Runs entirely in its own thread (tkinter mainloop), polling daemon state -
no cross-thread tk calls.
"""
import logging
import math
import threading
import time

log = logging.getLogger("claude_code_ptt")

BG = "#101418"
FG = "#d7e0ea"
ACCENT = "#2c7df0"
ROW_BG = "#1a2129"
RED = "#ff5252"
DARK_RED = "#5a1f1f"
ORANGE = "#ff9d3c"
TICK_MS = 100

# Temporary style picker: five candidate recording looks rendered as demo
# rows that all animate during a real recording. Jogi picks one, the winner
# becomes the single recording style and this block gets removed.
DEMO_STYLES = [
    "Stil 1: Hintergrund atmet rot",
    "Stil 2: alles statisch rot",
    "Stil 3: Rahmen atmet rot",
    "Stil 4: dunkelroter Grund",
    "Stil 5: nur Punkt atmet",
]


def _mix(color_a: str, color_b: str, amount: float) -> str:
    """Blend two #rrggbb colors; amount 0 = a, 1 = b."""
    a = [int(color_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(color_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}"
                         for x, y in zip(a, b))


class Overlay:
    def __init__(self, daemon):
        self._daemon = daemon
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception:                  # noqa: BLE001
            log.exception("tkinter unavailable, overlay disabled")
            return

        root = tk.Tk()
        root.title("PTT")
        root.attributes("-topmost", True)
        root.overrideredirect(True)
        root.configure(bg=BG)
        root.geometry(f"+{root.winfo_screenwidth() - 250}+40")

        title = tk.Label(root, text="● PTT", bg=BG, fg=ACCENT,
                         font=("Segoe UI", 10, "bold"), anchor="w")
        title.pack(fill="x", padx=8, pady=(6, 2))
        rows_frame = tk.Frame(root, bg=BG)
        rows_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        drag = {"x": 0, "y": 0}
        title.bind("<Button-1>",
                   lambda e: drag.update(x=e.x, y=e.y))
        title.bind("<B1-Motion>",
                   lambda e: root.geometry(f"+{e.x_root - drag['x']}"
                                           f"+{e.y_root - drag['y']}"))

        rows: dict[int, tuple] = {}        # pid -> (frame, dot, text)
        structure: list | None = None      # None forces the initial build

        def build_rows(sessions):
            for child in rows_frame.winfo_children():
                child.destroy()
            rows.clear()

            def add_row(label_text, pid):
                frame = tk.Frame(rows_frame, bg=ROW_BG,
                                 highlightthickness=2,
                                 highlightbackground=ROW_BG)
                frame.pack(fill="x", pady=1)
                dot = tk.Label(frame, text="●", bg=ROW_BG, fg=ROW_BG,
                               font=("Segoe UI", 9), padx=4)
                dot.pack(side="left")
                text = tk.Label(frame, text=label_text, anchor="w",
                                bg=ROW_BG, fg=FG, pady=3,
                                font=("Segoe UI", 9))
                text.pack(side="left", fill="x", expand=True)
                if pid >= 0:               # demo rows are display-only
                    for widget in (frame, dot, text):
                        widget.bind("<Button-1>",
                                    lambda _e, p=pid:
                                    self._daemon.registry.select(p))
                rows[pid] = (frame, dot, text)

            add_row("Auto (letzter Fokus)", 0)
            for info in sorted(sessions, key=lambda s: s["label"].lower()):
                add_row(f"{info['label']}  ({info['pid']})", info["pid"])
            if not sessions:
                tk.Label(rows_frame, text="keine Sessions", bg=BG,
                         fg="#5a6672", font=("Segoe UI", 8)).pack(pady=2)
            tk.Label(rows_frame, text="Aufnahme-Stile (Demo)", bg=BG,
                     fg="#5a6672", font=("Segoe UI", 8)).pack(pady=(6, 1))
            for style_no, style_label in enumerate(DEMO_STYLES, start=1):
                add_row(style_label, -style_no)

        def paint():
            try:
                _paint_once()
            except Exception:              # noqa: BLE001
                log.exception("overlay paint failed")
            root.after(TICK_MS, paint)     # the loop must survive any error

        def _paint_once():
            sessions = self._daemon.registry.list()
            selected = self._daemon.registry.selected_pid
            state = self._daemon.ui_state()

            nonlocal structure
            new_structure = sorted(s["pid"] for s in sessions)
            if new_structure != structure:
                structure = new_structure
                build_rows(sessions)

            highlight = state["highlight_pid"]
            recording = state["phase"] == "recording"
            pulse = (math.sin(time.monotonic() * 2 * math.pi / 1.2) + 1) / 2
            for pid, (frame, dot, text) in rows.items():
                is_selected = pid == selected
                is_target = pid == highlight
                base_bg = ACCENT if is_selected else ROW_BG
                base_fg = "white" if is_selected else FG
                bg, fg, dot_fg, border = base_bg, base_fg, base_bg, base_bg

                if pid < 0:                # demo rows mirror the styles live
                    style = -pid
                    if recording:
                        if style == 1:
                            bg = _mix(ROW_BG, RED, pulse)
                            fg, dot_fg, border = "white", "white", bg
                        elif style == 2:
                            fg, dot_fg, border = RED, RED, RED
                        elif style == 3:
                            border = _mix(ROW_BG, RED, pulse)
                        elif style == 4:
                            bg = DARK_RED
                            fg, border = "white", DARK_RED
                            dot_fg = _mix("#ffffff", RED, pulse)
                        elif style == 5:
                            dot_fg = _mix(ROW_BG, RED, pulse)
                elif is_target and recording:
                    dot_fg, border, fg = RED, RED, RED
                elif is_target and state["phase"] == "transcribing":
                    fg = _mix("#8a5a2a", ORANGE, pulse)
                elif is_target and state["phase"] == "flash":
                    bg, fg, dot_fg, border = ORANGE, "black", ORANGE, ORANGE

                frame.configure(bg=bg, highlightbackground=border)
                dot.configure(bg=bg, fg=dot_fg)
                text.configure(bg=bg, fg=fg)

            title.configure(
                fg=RED if state["phase"] == "recording" else ACCENT,
                text="● REC" if state["phase"] == "recording" else "● PTT")

        paint()
        root.mainloop()
