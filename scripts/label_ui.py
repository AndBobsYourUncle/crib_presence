#!/usr/bin/env python3
"""Interactive timeline labeler for crib frames.

Scrub through a directory of timestamped frames, mark IN/OUT transitions,
and export a ranges.csv compatible with label_by_time.py.

Keys:
    Left / Right        prev / next frame
    Shift+Left/Right    ±30 frames
    Cmd/Ctrl+Left/Right ±300 frames
    Home / End          first / last frame
    i                   mark baby IN (occupied starts at this frame)
    o                   mark baby OUT (empty starts at this frame)
    u                   undo most recent marker
    s                   save ranges CSV

Requirements:
    pip install pillow
"""
import argparse
import csv
import json
import re
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

FRAME_RE = re.compile(r"(\d{8}-\d{6})_([a-z]+)_c([0-9.]+)\.jpg$")

COLOR_OCCUPIED = "#2e7d32"
COLOR_EMPTY = "#c62828"
COLOR_UNKNOWN = "#555"
COLOR_CURSOR = "#ffeb3b"
COLOR_MARKER = "#ffffff"


@dataclass
class Frame:
    path: Path
    timestamp: datetime
    yolo_label: str
    yolo_conf: float


@dataclass
class Marker:
    timestamp: datetime
    kind: str  # "in" or "out"


def load_frames(frames_dir: Path) -> list[Frame]:
    frames: list[Frame] = []
    for p in sorted(frames_dir.glob("*.jpg")):
        m = FRAME_RE.match(p.name)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
        frames.append(Frame(p, ts, m.group(2), float(m.group(3))))
    return frames


def state_between(markers: list[Marker]) -> list[tuple[datetime, datetime, str]]:
    """Derive (start, end, label) spans from a sorted marker list."""
    spans = []
    sorted_markers = sorted(markers, key=lambda m: m.timestamp)
    for i, m in enumerate(sorted_markers):
        end = (
            sorted_markers[i + 1].timestamp
            if i + 1 < len(sorted_markers)
            else None
        )
        label = "occupied" if m.kind == "in" else "empty"
        spans.append((m.timestamp, end, label))
    return spans


class LabelerApp:
    def __init__(self, root: tk.Tk, frames_dir: Path, markers_path: Path | None):
        self.root = root
        self.frames_dir = frames_dir
        self.markers_path = markers_path
        self.frames = load_frames(frames_dir)
        if not self.frames:
            messagebox.showerror("No frames", f"No .jpg frames in {frames_dir}")
            root.destroy()
            return

        self.idx = 0
        self.markers: list[Marker] = []
        self._photo = None  # keep reference so GC doesn't eat it

        if markers_path and markers_path.exists():
            self._load_markers(markers_path)

        self._build_ui()
        self.root.after(50, self._render)  # let geometry settle first

    # ---------- UI ----------

    def _build_ui(self) -> None:
        self.root.title(f"Crib Labeler — {self.frames_dir}")
        self.root.geometry("1000x820")
        self.root.configure(bg="#1a1a1a")

        info = tk.Frame(self.root, bg="#1a1a1a")
        info.pack(fill=tk.X, padx=10, pady=(10, 4))
        self.info_var = tk.StringVar()
        tk.Label(
            info, textvariable=self.info_var,
            font=("Menlo", 12), fg="#eee", bg="#1a1a1a", anchor="w",
        ).pack(fill=tk.X)

        body = tk.Frame(self.root, bg="#1a1a1a")
        body.pack(fill=tk.BOTH, expand=True, padx=10)

        self.canvas = tk.Canvas(body, bg="black", highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda e: self._render())

        side = tk.Frame(body, bg="#1a1a1a", width=260)
        side.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))
        side.pack_propagate(False)
        tk.Label(
            side, text="Markers", font=("Menlo", 12, "bold"),
            fg="#eee", bg="#1a1a1a",
        ).pack(anchor="w")
        self.marker_list = tk.Listbox(
            side, font=("Menlo", 11), bg="#111", fg="#eee",
            selectbackground="#333", highlightthickness=0, borderwidth=0,
        )
        self.marker_list.pack(fill=tk.BOTH, expand=True, pady=4)
        self.marker_list.bind("<<ListboxSelect>>", self._on_marker_pick)

        tk.Button(side, text="Clear all markers", command=self._clear_all).pack(
            fill=tk.X, pady=(4, 0),
        )

        self.timeline = tk.Canvas(
            self.root, height=72, bg="#222", highlightthickness=0,
        )
        self.timeline.pack(fill=tk.X, padx=10, pady=6)
        self.timeline.bind("<Button-1>", self._on_timeline_click)
        self.timeline.bind("<B1-Motion>", self._on_timeline_click)
        self.timeline.bind("<Configure>", lambda e: self._draw_timeline())

        controls = tk.Frame(self.root, bg="#1a1a1a")
        controls.pack(fill=tk.X, padx=10, pady=(0, 10))
        tk.Button(
            controls, text="◀ Prev", command=lambda: self._nav(-1),
        ).pack(side=tk.LEFT)
        tk.Button(
            controls, text="Next ▶", command=lambda: self._nav(1),
        ).pack(side=tk.LEFT, padx=(4, 16))
        tk.Button(
            controls, text="⬇ Mark IN (i)",
            highlightbackground="#2e7d32",
            command=self._mark_in,
        ).pack(side=tk.LEFT)
        tk.Button(
            controls, text="⬆ Mark OUT (o)",
            highlightbackground="#c62828",
            command=self._mark_out,
        ).pack(side=tk.LEFT, padx=4)
        tk.Button(
            controls, text="Undo (u)", command=self._undo,
        ).pack(side=tk.LEFT, padx=(16, 0))
        tk.Button(
            controls, text="Save CSV (s)", command=self._save,
        ).pack(side=tk.RIGHT)

        # Keybindings
        for key, fn in [
            ("<Right>", lambda e: self._nav(1)),
            ("<Left>", lambda e: self._nav(-1)),
            ("<Shift-Right>", lambda e: self._nav(30)),
            ("<Shift-Left>", lambda e: self._nav(-30)),
            ("<Command-Right>", lambda e: self._nav(300)),
            ("<Command-Left>", lambda e: self._nav(-300)),
            ("<Control-Right>", lambda e: self._nav(300)),
            ("<Control-Left>", lambda e: self._nav(-300)),
            ("<Home>", lambda e: self._nav_to(0)),
            ("<End>", lambda e: self._nav_to(len(self.frames) - 1)),
            ("i", lambda e: self._mark_in()),
            ("o", lambda e: self._mark_out()),
            ("u", lambda e: self._undo()),
            ("s", lambda e: self._save()),
        ]:
            self.root.bind(key, fn)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- navigation ----------

    def _nav(self, delta: int) -> None:
        self._nav_to(self.idx + delta)

    def _nav_to(self, i: int) -> None:
        self.idx = max(0, min(len(self.frames) - 1, i))
        self._render()

    def _on_timeline_click(self, event: tk.Event) -> None:
        w = self.timeline.winfo_width()
        if w <= 0 or not self.frames:
            return
        frac = max(0.0, min(1.0, event.x / w))
        start = self.frames[0].timestamp
        end = self.frames[-1].timestamp
        target = start + (end - start) * frac
        # nearest frame by timestamp
        best = min(
            range(len(self.frames)),
            key=lambda i: abs((self.frames[i].timestamp - target).total_seconds()),
        )
        self._nav_to(best)

    def _on_marker_pick(self, _event) -> None:
        sel = self.marker_list.curselection()
        if not sel:
            return
        sorted_markers = sorted(self.markers, key=lambda m: m.timestamp)
        target = sorted_markers[sel[0]].timestamp
        best = min(
            range(len(self.frames)),
            key=lambda i: abs((self.frames[i].timestamp - target).total_seconds()),
        )
        self._nav_to(best)

    # ---------- marker ops ----------

    def _mark_in(self) -> None:
        self._add_marker("in")

    def _mark_out(self) -> None:
        self._add_marker("out")

    def _add_marker(self, kind: str) -> None:
        ts = self.frames[self.idx].timestamp
        # replace any marker at the same timestamp
        self.markers = [m for m in self.markers if m.timestamp != ts]
        self.markers.append(Marker(ts, kind))
        self._render_markers()
        self._draw_timeline()

    def _undo(self) -> None:
        if not self.markers:
            return
        self.markers.pop()
        self._render_markers()
        self._draw_timeline()

    def _clear_all(self) -> None:
        if not self.markers:
            return
        if not messagebox.askyesno("Clear all markers?", "Remove every marker?"):
            return
        self.markers.clear()
        self._render_markers()
        self._draw_timeline()

    # ---------- render ----------

    def _render(self) -> None:
        if not self.frames:
            return
        f = self.frames[self.idx]
        self.info_var.set(
            f"[{self.idx + 1}/{len(self.frames)}]   "
            f"{f.timestamp:%Y-%m-%d %H:%M:%S}   "
            f"YOLO: {f.yolo_label} c={f.yolo_conf:.2f}   "
            f"{f.path.name}"
        )
        try:
            img = Image.open(f.path)
        except Exception as e:
            self.canvas.delete("all")
            self.canvas.create_text(
                10, 10, text=f"failed to load: {e}",
                anchor="nw", fill="#f88", font=("Menlo", 12),
            )
            return
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        img.thumbnail((cw, ch))
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, image=self._photo, anchor=tk.CENTER)
        self._draw_timeline()
        self._render_markers()

    def _draw_timeline(self) -> None:
        self.timeline.delete("all")
        w = max(self.timeline.winfo_width(), 1)
        h = int(self.timeline.winfo_height()) or 72
        if not self.frames:
            return
        start = self.frames[0].timestamp
        end = self.frames[-1].timestamp
        total = (end - start).total_seconds() or 1

        # Background: unknown everywhere
        self.timeline.create_rectangle(0, 0, w, h, fill=COLOR_UNKNOWN, outline="")

        # Colored state spans from markers
        for s, e, label in state_between(self.markers):
            x0 = ((s - start).total_seconds() / total) * w
            x1 = (
                ((e - start).total_seconds() / total) * w
                if e is not None else w
            )
            color = COLOR_OCCUPIED if label == "occupied" else COLOR_EMPTY
            self.timeline.create_rectangle(x0, 0, x1, h, fill=color, outline="")

        # Marker lines
        for m in self.markers:
            x = ((m.timestamp - start).total_seconds() / total) * w
            self.timeline.create_line(x, 0, x, h, fill=COLOR_MARKER, width=2)
            self.timeline.create_text(
                x + 3, 8, text=m.kind.upper(), anchor="nw",
                fill=COLOR_MARKER, font=("Menlo", 9, "bold"),
            )

        # Cursor
        cur_x = ((self.frames[self.idx].timestamp - start).total_seconds() / total) * w
        self.timeline.create_line(cur_x, 0, cur_x, h, fill=COLOR_CURSOR, width=3)

    def _render_markers(self) -> None:
        self.marker_list.delete(0, tk.END)
        for m in sorted(self.markers, key=lambda x: x.timestamp):
            self.marker_list.insert(
                tk.END, f"{m.kind.upper():3s}  {m.timestamp:%Y-%m-%d %H:%M:%S}",
            )

    # ---------- persistence ----------

    def _load_markers(self, path: Path) -> None:
        with open(path) as f:
            data = json.load(f)
        self.markers = [
            Marker(datetime.fromisoformat(e["timestamp"]), e["kind"])
            for e in data
        ]

    def _markers_to_json(self, path: Path) -> None:
        data = [
            {"timestamp": m.timestamp.isoformat(), "kind": m.kind}
            for m in sorted(self.markers, key=lambda x: x.timestamp)
        ]
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def _save(self) -> None:
        if not self.markers:
            messagebox.showwarning("Nothing to save", "No markers placed yet.")
            return
        path = filedialog.asksaveasfilename(
            title="Save ranges CSV",
            defaultextension=".csv",
            initialfile="ranges.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        csv_path = Path(path)
        sidecar = csv_path.with_suffix(".markers.json")
        sorted_markers = sorted(self.markers, key=lambda m: m.timestamp)
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["start", "end", "label"])
            for i, m in enumerate(sorted_markers):
                end_ts = (
                    sorted_markers[i + 1].timestamp
                    if i + 1 < len(sorted_markers)
                    else self.frames[-1].timestamp
                )
                label = "occupied" if m.kind == "in" else "empty"
                w.writerow([
                    m.timestamp.strftime("%Y%m%d-%H%M%S"),
                    end_ts.strftime("%Y%m%d-%H%M%S"),
                    label,
                ])
        self._markers_to_json(sidecar)
        messagebox.showinfo(
            "Saved",
            f"Ranges:  {csv_path}\nMarkers: {sidecar}",
        )

    def _on_close(self) -> None:
        if self.markers:
            backup = Path.home() / ".baby_presence_markers_backup.json"
            try:
                self._markers_to_json(backup)
            except Exception:
                pass
        self.root.destroy()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("frames_dir", type=Path, help="directory of .jpg frames")
    p.add_argument(
        "--markers", type=Path,
        help="markers.json to resume from "
             "(default: ./ranges.markers.json if present)",
    )
    args = p.parse_args()

    markers_path = args.markers
    if markers_path is None:
        default = Path("ranges.markers.json")
        if default.exists():
            markers_path = default
            print(f"loading markers from {default}")

    root = tk.Tk()
    LabelerApp(root, args.frames_dir, markers_path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
