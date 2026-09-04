"""Play a reference video and record a keyboard rhythm sheet.

Windows uses a low-level global keyboard hook. Press F9 to start the explicitly
provided video and set time zero, then press the arrow key(s) in time with it.
Press F10 to stop and save a JSON + CSV sheet.

Examples:
    python -m flexscale.sheet_recorder --video input/example.mp4 --bpm 120 --keys down
    python -m flexscale.sheet_recorder --video path/to/another_video.mp4 --sheet-dir path/to/sheets --keys down
"""

from __future__ import annotations

import argparse
import csv
import ctypes
from ctypes import wintypes
import json
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from . import rhythm_hud

VK_TO_KEY = {
    0x25: "left",
    0x26: "up",
    0x27: "right",
    0x28: "down",
}
SYNC_VK = 0x78  # F9
STOP_VK = 0x79  # F10
SPACE_VK = 0x20


class VideoPlayer:
    """Open the actual reference video when F9 establishes time zero."""

    def __init__(self, source: str | None) -> None:
        self.source = Path(source).resolve() if source else None
        self.process: subprocess.Popen[bytes] | None = None

    def set_source(self, source: str | Path) -> None:
        self.stop()
        self.source = Path(source).resolve()

    def prepare(self) -> None:
        if not self.source:
            return
        if not self.source.exists():
            raise FileNotFoundError(f"Video file not found: {self.source}")

    def play(self) -> None:
        if self.source:
            self.stop()
            try:
                self.process = subprocess.Popen(
                    [
                        rhythm_hud.media_tool("ffplay"),
                        "-autoexit",
                        "-fs",
                        "-probesize",
                        "32",
                        "-analyzeduration",
                        "0",
                        "-loglevel",
                        "warning",
                        str(self.source),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=rhythm_hud.hidden_process_flags(),
                )
            except OSError as error:
                raise RuntimeError("ffplay was not found or could not start") from error

    def stop(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None


class RhythmRecorder:
    def __init__(self, allowed_keys: set[str], ui_queue: queue.Queue[tuple[str, object]], video_player: VideoPlayer) -> None:
        self.allowed_keys = allowed_keys
        self.ui_queue = ui_queue
        self.video_player = video_player
        self.lock = threading.Lock()
        self.active = False
        self.recording = False
        self.zero_time: float | None = None
        self.duration_seconds = 0.0
        self.events: list[dict[str, object]] = []
        self.pressed: set[int] = set()

    def arm(self) -> None:
        with self.lock:
            self.active = True
            self.recording = True
            self.zero_time = None
            self.duration_seconds = 0.0
            self.events = []
            self.pressed.clear()
        self.ui_queue.put(("status", "ARMED - press F9 exactly when the video starts"))

    def sync(self) -> None:
        with self.lock:
            if not self.recording:
                self.recording = True
            self.zero_time = time.perf_counter()
            self.duration_seconds = 0.0
            self.events = []
        try:
            self.video_player.play()
        except RuntimeError as error:
            self.stop()
            self.ui_queue.put(("error", str(error)))
            return
        self.ui_queue.put(("status", "SYNCED - video playing, recording Down-key beats now"))
        self.ui_queue.put(("count", 0))

    def stop(self, notify: bool = True) -> None:
        with self.lock:
            was_recording = self.recording
            if self.zero_time is not None and self.recording:
                self.duration_seconds = max(0.0, time.perf_counter() - self.zero_time)
            self.recording = False
        self.video_player.stop()
        if was_recording and notify:
            self.ui_queue.put(("status", "STOPPED - press Save Sheet to write the reference files"))

    def deactivate(self) -> None:
        self.stop(notify=False)
        with self.lock:
            self.active = False
            self.pressed.clear()

    def on_key(self, vk_code: int, is_down: bool) -> bool:
        with self.lock:
            active = self.active
            recording = self.recording
        if not active:
            return False
        captures_key = VK_TO_KEY.get(vk_code) in self.allowed_keys
        captures_special = vk_code in (SYNC_VK, STOP_VK)
        if is_down:
            if vk_code in self.pressed:
                return captures_special or (captures_key and recording)
            self.pressed.add(vk_code)
        else:
            self.pressed.discard(vk_code)
            return captures_special or (captures_key and recording)

        if vk_code == SYNC_VK:
            self.sync()
            return True
        if vk_code == STOP_VK:
            self.stop()
            return True
        if vk_code == SPACE_VK:
            return False

        key = VK_TO_KEY.get(vk_code)
        if not key or key not in self.allowed_keys:
            return False
        with self.lock:
            if not self.recording or self.zero_time is None:
                return False
            timestamp = max(0.0, time.perf_counter() - self.zero_time)
            self.events.append({"time_seconds": round(timestamp, 4), "key": key})
            count = len(self.events)
        self.ui_queue.put(("count", count))
        return True

    def snapshot(self) -> tuple[list[dict[str, object]], float]:
        with self.lock:
            events = list(self.events)
            if self.zero_time is None:
                duration = 0.0
            elif not self.recording:
                duration = self.duration_seconds
            else:
                duration = max(0.0, time.perf_counter() - self.zero_time)
        return events, round(duration, 4)


class WindowsKeyboardHook(threading.Thread):
    """Global arrow/F-key listener using only Windows ctypes APIs."""

    def __init__(self, recorder: RhythmRecorder) -> None:
        super().__init__(daemon=True)
        self.recorder = recorder
        self.thread_id = 0
        self.hook = None
        self.stop_requested = threading.Event()

    def run(self) -> None:
        if sys.platform != "win32":
            return

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.thread_id = kernel32.GetCurrentThreadId()

        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p),
            ]

        callback_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        # Declare pointer-sized signatures explicitly. Without these ctypes
        # can truncate the callback/module handles on 64-bit Windows.
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, ctypes.c_void_p, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_long
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL

        @callback_type
        def callback(n_code: int, w_param: int, l_param: int) -> int:
            if n_code >= 0:
                data = ctypes.cast(l_param, ctypes.POINTER(KeyboardInput)).contents
                is_down = w_param in (0x0100, 0x0104)  # WM_KEYDOWN / WM_SYSKEYDOWN
                is_up = w_param in (0x0101, 0x0105)  # WM_KEYUP / WM_SYSKEYUP
                if is_down or is_up:
                    consumed = self.recorder.on_key(int(data.vkCode), is_down)
                    if consumed:
                        return 1
            return user32.CallNextHookEx(self.hook, n_code, w_param, l_param)

        module_handle = kernel32.GetModuleHandleW(None)
        self.hook = user32.SetWindowsHookExW(13, callback, module_handle, 0)  # WH_KEYBOARD_LL
        if not self.hook:
            error_code = ctypes.get_last_error()
            self.recorder.ui_queue.put(("error", f"Could not install the global keyboard hook (Windows error {error_code})."))
            return

        msg = wintypes.MSG()
        while not self.stop_requested.is_set() and user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(self.hook)

    def close(self) -> None:
        self.stop_requested.set()
        if self.thread_id and sys.platform == "win32":
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT


def write_sheet(base_path: Path, events: list[dict[str, object]], duration: float, bpm: float | None, music: str | None) -> tuple[Path, Path]:
    base_path.parent.mkdir(parents=True, exist_ok=True)
    json_path = base_path.with_suffix(".json")
    csv_path = base_path.with_suffix(".csv")
    sheet = {
        "format": "flexscale-rhythm-sheet",
        "version": 1,
        "source_video": music,
        "bpm": bpm,
        "timing_reference": "F9 keypress is time zero",
        "duration_seconds": duration,
        "events": events,
    }
    json_path.write_text(json.dumps(sheet, indent=2) + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time_seconds", "key"])
        for event in events:
            writer.writerow([event["time_seconds"], event["key"]])
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional sheet basename; its filename must match the video filename",
    )
    parser.add_argument("--sheet-dir", type=Path, default=Path("guiding_sheets"), help="Default sheet directory")
    parser.add_argument("--bpm", type=float, default=None, help="Optional BPM written into the sheet metadata")
    parser.add_argument("--video", "--music", dest="video", required=True, help="Video file to open when F9 is pressed")
    parser.add_argument("--keys", nargs="+", choices=sorted(VK_TO_KEY.values()), default=list(VK_TO_KEY.values()))
    args = parser.parse_args()

    video_stem = Path(args.video).stem
    output_base = args.output or args.sheet_dir / video_stem
    if output_base.stem != video_stem:
        parser.error(
            f"sheet filename must match the video filename: expected '{video_stem}', got '{output_base.stem}'"
        )

    ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
    video_source = args.video
    video_player = VideoPlayer(video_source)
    if video_source:
        try:
            video_player.prepare()
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            print(f"Could not prepare video: {error}", file=sys.stderr)
            return 1
    recorder = RhythmRecorder(set(args.keys), ui_queue, video_player)
    hook = WindowsKeyboardHook(recorder)
    hook.start()

    root = tk.Tk()
    root.title("FlexScale Rhythm Sheet Recorder")
    root.geometry("520x310")
    root.resizable(False, False)

    status_var = tk.StringVar(value="Press Arm, then press F9 on the first beat")
    count_var = tk.StringVar(value="Notes recorded: 0")
    output_var = tk.StringVar(value=f"Output: {output_base.with_suffix('.json')}")

    tk.Label(root, text="FlexScale Rhythm Sheet Recorder", font=("Segoe UI", 16, "bold")).pack(pady=(18, 8))
    video_label = f"Video: {Path(video_source).name}\nPress F9 to open it and set time zero."
    tk.Label(root, text=video_label + "\nUse F10 to stop; arrow presses are captured globally on Windows.", justify="center").pack()
    tk.Label(root, textvariable=status_var, fg="#0b6680", font=("Segoe UI", 11, "bold")).pack(pady=(14, 4))
    tk.Label(root, textvariable=count_var).pack()
    tk.Label(root, textvariable=output_var, fg="#555").pack(pady=(4, 12))

    buttons = tk.Frame(root)
    buttons.pack()

    def arm() -> None:
        recorder.arm()

    def stop() -> None:
        recorder.stop()

    def save() -> None:
        events, duration = recorder.snapshot()
        json_path, csv_path = write_sheet(output_base, events, duration, args.bpm, video_source)
        status_var.set(f"Saved {len(events)} notes")
        messagebox.showinfo("Rhythm sheet saved", f"JSON: {json_path}\nCSV: {csv_path}")

    tk.Button(buttons, text="Arm / Reset", width=14, command=arm).grid(row=0, column=0, padx=5)
    tk.Button(buttons, text="Stop", width=14, command=stop).grid(row=0, column=1, padx=5)
    tk.Button(buttons, text="Save Sheet", width=14, command=save).grid(row=0, column=2, padx=5)

    def process_queue() -> None:
        try:
            while True:
                kind, value = ui_queue.get_nowait()
                if kind == "status":
                    status_var.set(str(value))
                elif kind == "count":
                    count_var.set(f"Notes recorded: {value}")
                elif kind == "error":
                    status_var.set(str(value))
                    messagebox.showerror("Keyboard hook error", str(value))
        except queue.Empty:
            pass
        root.after(50, process_queue)

    def on_close() -> None:
        recorder.stop()
        hook.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    if sys.platform != "win32":
        root.bind("<KeyPress>", lambda event: recorder.on_key({"Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28, "F9": SYNC_VK, "F10": STOP_VK}.get(event.keysym, -1), True))
        root.bind("<KeyRelease>", lambda event: recorder.on_key({"Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28, "F9": SYNC_VK, "F10": STOP_VK}.get(event.keysym, -1), False))
    root.after(50, process_queue)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
