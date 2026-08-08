"""Floating always-on-top window listing all registered Claude Code sessions.

One row per session; clicking a row pins it as the PTT target, clicking
"Auto" returns to focus tracking. Runs entirely in its own thread (tkinter
mainloop), reading the registry via polling - no cross-thread tk calls.
"""
import logging
import threading

log = logging.getLogger("claude_code_ptt")

BG = "#101418"
FG = "#d7e0ea"
ACCENT = "#2c7df0"
ROW_BG = "#1a2129"


class Overlay:
    def __init__(self, registry, recorder):
        self._registry = registry
        self._recorder = recorder
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
        screen_w = root.winfo_screenwidth()
        root.geometry(f"+{screen_w - 240}+40")

        title = tk.Label(root, text="● PTT", bg=BG, fg=ACCENT,
                         font=("Segoe UI", 10, "bold"), anchor="w")
        title.pack(fill="x", padx=8, pady=(6, 2))
        rows_frame = tk.Frame(root, bg=BG)
        rows_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        # window drag support (frameless window has no title bar)
        drag = {"x": 0, "y": 0}

        def on_press(event):
            drag["x"], drag["y"] = event.x, event.y

        def on_drag(event):
            root.geometry(f"+{event.x_root - drag['x']}"
                          f"+{event.y_root - drag['y']}")

        title.bind("<Button-1>", on_press)
        title.bind("<B1-Motion>", on_drag)

        def rebuild():
            for child in rows_frame.winfo_children():
                child.destroy()
            sessions = self._registry.list()
            selected = self._registry.selected_pid

            def add_row(text, pid, active):
                row = tk.Label(
                    rows_frame, text=text, anchor="w", padx=8, pady=3,
                    bg=ACCENT if active else ROW_BG,
                    fg="white" if active else FG, font=("Segoe UI", 9))
                row.pack(fill="x", pady=1)
                row.bind("<Button-1>",
                         lambda _e, p=pid: self._registry.select(p))

            add_row("Auto (letzter Fokus)", 0, selected == 0)
            for info in sorted(sessions, key=lambda s: s["label"].lower()):
                add_row(f"{info['label']}  ({info['pid']})", info["pid"],
                        info["pid"] == selected)
            if not sessions:
                tk.Label(rows_frame, text="keine Sessions", bg=BG,
                         fg="#5a6672", font=("Segoe UI", 8)).pack(pady=2)

        def tick():
            rebuild()
            title.configure(
                fg="#ff5252" if self._recorder.recording else ACCENT,
                text="● REC" if self._recorder.recording else "● PTT")
            root.after(1000, tick)

        tick()
        root.mainloop()
