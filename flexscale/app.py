"""Small desktop client for generating a rhythm video from a matching sheet."""

from __future__ import annotations

import math
import os
import queue
import sys
import threading
import uuid
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk

from . import renderer, rhythm_hud, sheet_recorder


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def application_root() -> Path:
    """Find external input/output folders in source and frozen layouts."""
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates = (Path.cwd(), executable_dir, executable_dir.parent)
    else:
        candidates = (Path(__file__).resolve().parent.parent, Path.cwd())
    for candidate in candidates:
        if (candidate / "input").is_dir() or (candidate / "guiding_sheets").is_dir():
            return candidate
    return candidates[0]


def bundled_asset(relative_path: str, root: Path) -> Path | None:
    """Prefer an editable external asset, then an asset bundled in the EXE."""
    external = root / relative_path
    if external.is_file():
        return external
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        bundled = Path(bundle_dir) / relative_path
        if bundled.is_file():
            return bundled
    return None


def find_video(input_dir: Path, entered_name: str) -> Path:
    """Resolve a filename or stem case-insensitively, only inside input_dir."""
    name = entered_name.strip().strip('"')
    if not name:
        raise ValueError("Enter the name of a video.")
    if Path(name).name != name or name in {".", ".."}:
        raise ValueError("Enter a filename only. The video must be inside the input folder.")
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input folder not found: {input_dir}")

    requested = Path(name)
    files = [path for path in input_dir.iterdir() if path.is_file()]
    if requested.suffix:
        matches = [path for path in files if path.name.casefold() == name.casefold()]
    else:
        matches = [
            path
            for path in files
            if path.stem.casefold() == name.casefold() and path.suffix.casefold() in VIDEO_EXTENSIONS
        ]
    if not matches:
        raise FileNotFoundError(f'Video "{name}" was not found in the input folder.')
    if len(matches) > 1:
        choices = ", ".join(sorted(path.name for path in matches))
        raise ValueError(f"More than one video has that name. Include the extension: {choices}")
    if matches[0].suffix.casefold() not in VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported video type: {matches[0].suffix}")
    return matches[0]


def find_sheet(sheet_dir: Path, video_stem: str) -> Path:
    """Find JSON first, then CSV, with the same stem as the video."""
    if not sheet_dir.is_dir():
        raise FileNotFoundError(f"Guiding-sheets folder not found: {sheet_dir}")
    files = [path for path in sheet_dir.iterdir() if path.is_file()]
    for suffix in (".json", ".csv"):
        for path in files:
            if path.stem.casefold() == video_stem.casefold() and path.suffix.casefold() == suffix:
                return path
    raise FileNotFoundError(
        f'No matching sheet was found. Expected "{video_stem}.json" or "{video_stem}.csv" '
        "in the guiding_sheets folder."
    )


def reserve_output(output_dir: Path, stem: str) -> tuple[Path, Path]:
    """Atomically reserve stem.mp4, stem_2.mp4, ... across app instances."""
    output_dir.mkdir(parents=True, exist_ok=True)
    version = 1
    while True:
        suffix = "" if version == 1 else f"_{version}"
        output_path = output_dir / f"{stem}{suffix}.mp4"
        lock_path = output_dir / f".{output_path.name}.lock"
        if output_path.exists():
            version += 1
            continue
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            version += 1
            continue
        os.close(descriptor)
        if output_path.exists():
            lock_path.unlink(missing_ok=True)
            version += 1
            continue
        return output_path, lock_path


class FlexScaleApp:
    def __init__(self, window: tk.Tk) -> None:
        self.window = window
        self.root_dir = application_root()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.close_after_worker = False
        self.sheet_events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.sheet_player = sheet_recorder.VideoPlayer(None)
        self.sheet_recorder = sheet_recorder.RhythmRecorder(
            {"down"}, self.sheet_events, self.sheet_player
        )
        self.sheet_hook = sheet_recorder.WindowsKeyboardHook(self.sheet_recorder)
        self.sheet_video_path: Path | None = None
        self.sheet_hook.start()

        window.title("FlexScale Rhythm Video")
        window.geometry("660x430")
        window.minsize(620, 410)
        window.resizable(True, True)
        window.protocol("WM_DELETE_WINDOW", self.request_close)

        outer = ttk.Frame(window, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="FlexScale Rhythm Video", font=("Segoe UI", 17, "bold")).pack(anchor="w")

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True, pady=(12, 0))
        generate_tab = ttk.Frame(notebook, padding=18)
        sheet_tab = ttk.Frame(notebook, padding=18)
        notebook.add(generate_tab, text="Generate Video")
        notebook.add(sheet_tab, text="Create Sheet")

        ttk.Label(
            generate_tab,
            text="Name of the video (needs to be inside input folder):",
        ).pack(anchor="w", pady=(2, 5))

        entry_row = ttk.Frame(generate_tab)
        entry_row.pack(fill="x")
        self.video_name = tk.StringVar()
        self.entry = ttk.Entry(entry_row, textvariable=self.video_name)
        self.entry.pack(side="left", fill="x", expand=True)
        self.ok_button = ttk.Button(entry_row, text="OK", width=10, command=self.start)
        self.ok_button.pack(side="left", padx=(10, 0))
        self.entry.bind("<Return>", lambda _event: self.start())

        offset_row = ttk.Frame(generate_tab)
        offset_row.pack(fill="x", pady=(10, 0))
        ttk.Label(offset_row, text="Offset adjustment (ms, optional):").pack(side="left")
        self.offset_ms = tk.StringVar(value="0")
        self.offset_entry = ttk.Entry(offset_row, textvariable=self.offset_ms, width=10)
        self.offset_entry.pack(side="left", padx=(8, 8))
        ttk.Label(
            offset_row,
            text="Instruction: positive = delay hearts; negative = start hearts earlier",
        ).pack(side="left")

        self.progress_value = tk.DoubleVar(value=0)
        self.progress = ttk.Progressbar(generate_tab, variable=self.progress_value, maximum=100)
        self.progress.pack(fill="x", pady=(20, 5))
        self.status = tk.StringVar(value="Ready")
        self.status_label = ttk.Label(generate_tab, textvariable=self.status)
        self.status_label.pack(anchor="w")

        button_row = ttk.Frame(generate_tab)
        button_row.pack(fill="x", pady=(18, 0))
        self.cancel_button = ttk.Button(button_row, text="Cancel", command=self.request_cancel, state="disabled")
        self.cancel_button.pack(side="left")
        self.close_button = ttk.Button(button_row, text="Close", command=self.request_close)
        self.close_button.pack(side="right")

        ttk.Label(
            sheet_tab,
            text="Name of the video (needs to be inside input folder):",
        ).pack(anchor="w", pady=(2, 5))
        sheet_entry_row = ttk.Frame(sheet_tab)
        sheet_entry_row.pack(fill="x")
        self.sheet_video_name = tk.StringVar()
        self.sheet_entry = ttk.Entry(sheet_entry_row, textvariable=self.sheet_video_name)
        self.sheet_entry.pack(side="left", fill="x", expand=True)
        self.sheet_bpm = tk.StringVar()
        ttk.Label(sheet_entry_row, text="BPM (optional):").pack(side="left", padx=(12, 5))
        ttk.Entry(sheet_entry_row, textvariable=self.sheet_bpm, width=8).pack(side="left")

        instructions = (
            "1. Click Arm / Reset.   2. Press F9 to open and sync the video.\n"
            "3. Press DOWN on every beat.   4. Press F10, then Save Sheet."
        )
        ttk.Label(sheet_tab, text=instructions, justify="left").pack(anchor="w", pady=(18, 12))
        self.sheet_status = tk.StringVar(value="Enter a video name, then click Arm / Reset")
        ttk.Label(sheet_tab, textvariable=self.sheet_status, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.sheet_count = tk.StringVar(value="Notes recorded: 0")
        ttk.Label(sheet_tab, textvariable=self.sheet_count).pack(anchor="w", pady=(5, 0))
        self.sheet_output = tk.StringVar(value="Output: guiding_sheets/<video-name>.json and .csv")
        ttk.Label(sheet_tab, textvariable=self.sheet_output).pack(anchor="w", pady=(5, 0))

        sheet_buttons = ttk.Frame(sheet_tab)
        sheet_buttons.pack(fill="x", pady=(20, 0))
        ttk.Button(sheet_buttons, text="Arm / Reset", command=self.arm_sheet).pack(side="left")
        ttk.Button(sheet_buttons, text="Stop", command=self.stop_sheet).pack(side="left", padx=(8, 0))
        ttk.Button(sheet_buttons, text="Save Sheet", command=self.save_sheet).pack(side="left", padx=(8, 0))
        ttk.Button(sheet_buttons, text="Close", command=self.request_close).pack(side="right")

        self.entry.focus_set()
        if sys.platform != "win32":
            key_map = {
                "Down": 0x28,
                "F9": sheet_recorder.SYNC_VK,
                "F10": sheet_recorder.STOP_VK,
            }
            window.bind(
                "<KeyPress>",
                lambda event: self.sheet_recorder.on_key(key_map.get(event.keysym, -1), True),
            )
            window.bind(
                "<KeyRelease>",
                lambda event: self.sheet_recorder.on_key(key_map.get(event.keysym, -1), False),
            )
        self.window.after(75, self.process_events)

    @property
    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.entry.configure(state=state)
        self.ok_button.configure(state=state)
        self.offset_entry.configure(state=state)
        self.cancel_button.configure(state="normal" if running else "disabled")

    def start(self) -> None:
        if self.running:
            return
        if self.sheet_recorder.active:
            messagebox.showerror(
                "Recorder active",
                "Stop and save the current sheet recording before generating a video.",
                parent=self.window,
            )
            return
        name = self.video_name.get()
        offset_text = self.offset_ms.get().strip()
        try:
            offset_adjustment_ms = float(offset_text) if offset_text else 0.0
            if not math.isfinite(offset_adjustment_ms):
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid offset",
                "Offset must be a number in milliseconds, or left empty.",
                parent=self.window,
            )
            return
        offset_seconds = renderer.DEFAULT_HIT_OFFSET_SECONDS + offset_adjustment_ms / 1000.0
        try:
            video_path = find_video(self.root_dir / "input", name)
            sheet_path = find_sheet(self.root_dir / "guiding_sheets", video_path.stem)
            output_path, lock_path = reserve_output(self.root_dir / "output", video_path.stem)
        except (OSError, ValueError) as error:
            messagebox.showerror("Cannot start", str(error), parent=self.window)
            return

        self.cancel_event.clear()
        self.close_after_worker = False
        self.progress_value.set(0)
        self.status.set(f"Preparing {video_path.name}...")
        self.set_running(True)
        self.worker = threading.Thread(
            target=self.render_worker,
            args=(video_path, sheet_path, output_path, lock_path, offset_seconds),
            daemon=True,
        )
        self.worker.start()

    def render_worker(
        self,
        video_path: Path,
        sheet_path: Path,
        output_path: Path,
        lock_path: Path,
        offset_seconds: float,
    ) -> None:
        temporary_path = output_path.parent / f".{output_path.stem}.{uuid.uuid4().hex}.part.mp4"
        try:
            self.events.put(("status", f"Rendering {output_path.name}..."))

            def report(value: float) -> None:
                self.events.put(("progress", 5.0 + value * 95.0))

            heart_path = bundled_asset("assets/note.png", self.root_dir)
            renderer.render_rhythm_video(
                video_path=video_path,
                sheet_path=sheet_path,
                output_path=temporary_path,
                work_dir=self.root_dir / ".work",
                heart_path=heart_path,
                offset_seconds=offset_seconds,
                progress_callback=report,
                cancel_event=self.cancel_event,
            )
            if self.cancel_event.is_set():
                raise rhythm_hud.RenderCancelled()
            os.replace(temporary_path, output_path)
            self.events.put(("complete", output_path))
        except rhythm_hud.RenderCancelled:
            self.events.put(("cancelled", None))
        except Exception as error:
            self.events.put(("error", str(error)))
        finally:
            temporary_path.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)

    def request_cancel(self) -> None:
        if not self.running or self.cancel_event.is_set():
            return
        confirmed = messagebox.askyesno(
            "Cancel rendering",
            "If you cancel the process it will discard the output.\n\nDo you want to continue?",
            parent=self.window,
        )
        if confirmed:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status.set("Cancelling and discarding the unfinished output...")

    def arm_sheet(self) -> None:
        if self.running:
            messagebox.showerror(
                "Render in progress",
                "Wait for the current render to finish or cancel it before recording a sheet.",
                parent=self.window,
            )
            return
        try:
            video_path = find_video(self.root_dir / "input", self.sheet_video_name.get())
            self.sheet_player.set_source(video_path)
            self.sheet_player.prepare()
        except (OSError, ValueError) as error:
            messagebox.showerror("Cannot arm recorder", str(error), parent=self.window)
            return
        self.sheet_video_path = video_path
        self.sheet_count.set("Notes recorded: 0")
        self.sheet_output.set(
            f"Output: guiding_sheets/{video_path.stem}.json and {video_path.stem}.csv"
        )
        self.sheet_recorder.arm()

    def stop_sheet(self) -> None:
        if self.sheet_recorder.active:
            self.sheet_recorder.stop()

    def save_sheet(self) -> None:
        if self.sheet_video_path is None or not self.sheet_recorder.active:
            messagebox.showerror(
                "Nothing to save",
                "Arm the recorder and record at least one Down-key beat first.",
                parent=self.window,
            )
            return
        self.sheet_recorder.stop(notify=False)
        events, duration = self.sheet_recorder.snapshot()
        if not events:
            messagebox.showerror(
                "Nothing to save",
                "No Down-key beats were recorded. Press F9, record the beats, and press F10.",
                parent=self.window,
            )
            return
        bpm_text = self.sheet_bpm.get().strip()
        try:
            bpm = float(bpm_text) if bpm_text else None
            if bpm is not None and bpm <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid BPM", "BPM must be a positive number or left empty.", parent=self.window)
            return

        output_base = self.root_dir / "guiding_sheets" / self.sheet_video_path.stem
        existing = [path for path in (output_base.with_suffix(".json"), output_base.with_suffix(".csv")) if path.exists()]
        if existing and not messagebox.askyesno(
            "Replace existing sheet?",
            f'A sheet for "{self.sheet_video_path.stem}" already exists. Replace it?',
            parent=self.window,
        ):
            return
        try:
            json_path, csv_path = sheet_recorder.write_sheet(
                output_base,
                events,
                duration,
                bpm,
                str(Path("input") / self.sheet_video_path.name),
            )
        except OSError as error:
            messagebox.showerror("Could not save sheet", str(error), parent=self.window)
            return
        self.sheet_recorder.deactivate()
        self.sheet_status.set(f"Saved {len(events)} notes for {self.sheet_video_path.name}")
        messagebox.showinfo(
            "Rhythm sheet saved",
            f"JSON: {json_path}\nCSV: {csv_path}",
            parent=self.window,
        )

    def request_close(self) -> None:
        if not self.running:
            if self.sheet_recorder.active:
                confirmed = messagebox.askyesno(
                    "Discard recording?",
                    "The unsaved sheet recording will be discarded. Do you want to close?",
                    parent=self.window,
                )
                if not confirmed:
                    return
            self.shutdown()
            return
        confirmed = messagebox.askyesno(
            "Cancel rendering",
            "If you cancel the process it will discard the output.\n\nDo you want to continue?",
            parent=self.window,
        )
        if confirmed:
            self.close_after_worker = True
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status.set("Cancelling and discarding the unfinished output...")

    def shutdown(self) -> None:
        self.sheet_recorder.deactivate()
        self.sheet_hook.close()
        self.window.destroy()

    def process_sheet_events(self) -> None:
        try:
            while True:
                kind, value = self.sheet_events.get_nowait()
                if kind == "status":
                    self.sheet_status.set(str(value))
                elif kind == "count":
                    self.sheet_count.set(f"Notes recorded: {value}")
                elif kind == "error":
                    self.sheet_status.set(str(value))
                    messagebox.showerror("Sheet recorder error", str(value), parent=self.window)
        except queue.Empty:
            pass

    def process_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    percent = float(value)
                    self.progress_value.set(percent)
                    self.status.set(f"Rendering... {percent:.0f}%")
                elif kind == "status":
                    self.status.set(str(value))
                elif kind == "complete":
                    self.progress_value.set(100)
                    self.status.set(f"Completed: {Path(value).name}")
                    self.set_running(False)
                    messagebox.showinfo(
                        "Render complete",
                        f"Rhythm video created:\n{value}",
                        parent=self.window,
                    )
                elif kind == "cancelled":
                    self.progress_value.set(0)
                    self.status.set("Cancelled. The unfinished output was discarded.")
                    self.set_running(False)
                    if self.close_after_worker:
                        self.shutdown()
                        return
                elif kind == "error":
                    self.status.set("Render failed")
                    self.set_running(False)
                    messagebox.showerror("Render failed", str(value), parent=self.window)
                    if self.close_after_worker:
                        self.shutdown()
                        return
        except queue.Empty:
            pass
        self.process_sheet_events()
        if self.window.winfo_exists():
            self.window.after(75, self.process_events)


def main() -> int:
    window = tk.Tk()
    FlexScaleApp(window)
    window.mainloop()
    return 0
