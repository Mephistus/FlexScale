"""Create a baked rhythm-game HUD subtitle track and render it over a video.

The script intentionally uses only the Python standard library.  It extracts a
small mono audio stream with ffmpeg, finds percussion/onset peaks, turns those
peaks into a deterministic DDR-style note chart, writes ASS vector/text events,
and asks ffmpeg/libass to composite the HUD while copying the original audio.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import threading
from array import array
from pathlib import Path
from typing import Callable


LANE_COLORS = ["#31d7ff", "#ffcf4a", "#ff65c7", "#75f58e"]
REFERENCE_WIDTH = 624
REFERENCE_HEIGHT = 352
TARGET_CENTER_X = 64
LANE_GLYPHS = ["◀", "▼", "▲", "▶"]


ProgressCallback = Callable[[float], None]


class RenderCancelled(Exception):
    """Raised when the user cancels an active FFmpeg render."""


def media_tool(name: str) -> str:
    """Locate FFmpeg tools on PATH or bundled beside a frozen application."""
    executable = f"{name}.exe" if sys.platform == "win32" else name
    candidates: list[Path] = []
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.extend((Path(bundle_dir) / executable, Path(bundle_dir) / "bin" / executable))
    if getattr(sys, "frozen", False):
        candidates.extend((Path(sys.executable).parent / executable, Path(sys.executable).parent / "bin" / executable))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    located = shutil.which(name)
    if located:
        return located
    raise FileNotFoundError(
        f"{executable} was not found. Install FFmpeg or place {executable} beside the application."
    )


def hidden_process_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def filter_path(path: Path) -> str:
    """Escape an absolute path for use as a value inside an FFmpeg filter."""
    value = path.resolve().as_posix().replace("'", r"\'").replace(":", r"\:")
    return f"'{value}'"


def run_capture(args: list[str]) -> bytes:
    command = [media_tool(args[0]), *args[1:]]
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=hidden_process_flags(),
    )
    return result.stdout


def probe_video(input_path: Path) -> tuple[float, int, int]:
    raw = run_capture(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=width,height",
            "-of",
            "json",
            str(input_path),
        ]
    )
    payload = json.loads(raw.decode())
    streams = payload.get("streams", [])
    if not streams:
        raise ValueError(f"No video stream found in {input_path}")
    stream = streams[0]
    return float(payload["format"]["duration"]), int(stream["width"]), int(stream["height"])


def probe_duration(input_path: Path) -> float:
    return probe_video(input_path)[0]


def extract_mono_audio(input_path: Path, audio_filter: str | None = None) -> tuple[array, int]:
    sample_rate = 11_025
    command = ["ffmpeg", "-v", "error", "-i", str(input_path), "-vn", "-ac", "1", "-ar", str(sample_rate)]
    if audio_filter:
        command.extend(["-af", audio_filter])
    command.extend(["-f", "s16le", "-"])
    raw = run_capture(command)
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples, sample_rate


def moving_average(values: list[float], radius: int) -> list[float]:
    if radius <= 0:
        return values[:]
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
    result: list[float] = []
    for index in range(len(values)):
        left = max(0, index - radius)
        right = min(len(values), index + radius + 1)
        result.append((prefix[right] - prefix[left]) / (right - left))
    return result


def audio_onsets(samples: array, sample_rate: int) -> list[float]:
    """Return approximate onset times using a lightweight energy detector."""
    block = max(1, int(sample_rate * 0.02))
    energy: list[float] = []
    for start in range(0, len(samples), block):
        part = samples[start : start + block]
        if not part:
            break
        # Mean absolute energy is less sensitive to individual loud vocal peaks
        # than a single-sample peak and is sufficient for a visual beat chart.
        energy.append(sum(abs(value) for value in part) / len(part))

    if len(energy) < 12:
        return []

    short = moving_average(energy, 1)
    baseline = moving_average(energy, 18)
    flux = [max(0.0, short[i] - baseline[i]) for i in range(len(short))]
    nonzero = [value for value in flux if value > 0]
    if not nonzero:
        return []
    threshold = statistics.mean(nonzero) + 0.45 * statistics.pstdev(nonzero)

    peaks: list[tuple[int, float]] = []
    for index in range(2, len(flux) - 2):
        value = flux[index]
        if value < threshold:
            continue
        if value >= flux[index - 1] and value >= flux[index + 1]:
            peaks.append((index, value))

    # Keep the strongest onset in a short neighborhood.  This avoids drawing
    # several notes for one drum hit while preserving fast later sections.
    selected: list[tuple[int, float]] = []
    for peak in peaks:
        if not selected or peak[0] - selected[-1][0] >= 6:
            selected.append(peak)
        elif peak[1] > selected[-1][1]:
            selected[-1] = peak
    return [index * 0.02 for index, _ in selected]


def merge_onsets(onset_lists: list[list[float]], minimum_gap: float = 0.12) -> list[float]:
    """Merge full-mix, vocal, kick, and high-frequency onset detections."""
    candidates = sorted(value for onset_list in onset_lists for value in onset_list)
    merged: list[float] = []
    for value in candidates:
        if not merged or value - merged[-1] >= minimum_gap:
            merged.append(value)
    return merged


def align_onsets_to_reference_grid(
    onsets: list[float],
    duration: float,
    bpm: float,
    phase_seconds: float = 0.0,
) -> list[float]:
    """Snap detected vocal/beat events to a caller-supplied beat grid."""
    if not onsets:
        return []
    beat = 60.0 / bpm
    phase = phase_seconds
    aligned: list[float] = []
    slot = 0
    while True:
        beat_time = phase + slot * (beat / 2.0)
        if beat_time >= duration - 0.25:
            break
        nearest = min(onsets, key=lambda value: abs(value - beat_time))
        if abs(nearest - beat_time) <= 0.095:
            aligned.append(round(beat_time, 3))
        slot += 1

    # In the last ten seconds, include quarter-beat positions only when the
    # vocal/electronic detector found a matching event there.
    finale_start = duration - 10.0
    slot = 0
    quarter_aligned: list[float] = []
    while True:
        beat_time = phase + slot * (beat / 4.0)
        if beat_time >= duration - 0.25:
            break
        if beat_time < finale_start:
            slot += 1
            continue
        nearest = min(onsets, key=lambda value: abs(value - beat_time))
        if abs(nearest - beat_time) <= 0.065:
            quarter_aligned.append(round(beat_time, 3))
        slot += 1

    return sorted(set(aligned + quarter_aligned))


def fallback_onsets(duration: float, bpm: float = 120.0) -> list[float]:
    interval = 60.0 / bpm
    return [index * interval for index in range(1, int(duration / interval))]


def hex_ass_color(hex_rgb: str) -> str:
    value = hex_rgb.lstrip("#")
    red, green, blue = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return f"&H{blue:02X}{green:02X}{red:02X}&"


def ass_alpha(alpha: int) -> str:
    return f"&H{max(0, min(255, alpha)):02X}&"


def ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remainder = seconds % 60
    return f"{hours}:{minutes:02d}:{remainder:05.2f}"


def rect(x: int, y: int, width: int, height: int, color: str, alpha: int = 0) -> str:
    return (
        f"{{\\p1\\pos({x},{y})\\1c{hex_ass_color(color)}\\1a{ass_alpha(alpha)}}}"
        f"m 0 0 l {width} 0 l {width} {height} l 0 {height}{{\\p0}}"
    )


def line(x: int, y: int, width: int, height: int, color: str, alpha: int = 0) -> str:
    return rect(x, y, width, height, color, alpha)


def make_chart(onsets: list[float], duration: float) -> list[tuple[float, int]]:
    """Create increasingly dense deterministic notes from detected onsets."""
    if not onsets:
        onsets = fallback_onsets(duration)

    usable = [value for value in onsets if 0.5 < value < duration - 0.35]
    if not usable:
        usable = fallback_onsets(duration)

    rng = random.Random(20260904)
    chart: list[tuple[float, int]] = []
    last_time = -10.0
    for index, hit in enumerate(usable):
        progress = hit / duration
        # Sparse intro, normal middle, then nearly every detected onset.
        # Begin with a meaningful pattern immediately, then continue ramping
        # toward the dense final sections.
        keep_probability = 0.50 + 0.50 * (progress**1.25)
        min_gap = 0.40 - 0.28 * progress
        if index == 0:
            lane = 0
            chart.append((hit, lane))
            last_time = hit
            continue
        if hit - last_time < min_gap:
            continue
        if rng.random() > keep_probability and progress < 0.84:
            continue
        lane = (index + int(progress * 11)) % 4
        chart.append((hit, lane))
        last_time = hit

        # Later sections add simultaneous and alternating notes, making the
        # visual chart substantially harder without requiring input detection.
        if progress > 0.62 and rng.random() < (progress - 0.42):
            second_lane = (lane + (1 if index % 2 == 0 else 3)) % 4
            chart.append((hit, second_lane))
        if progress > 0.84 and rng.random() < 0.62:
            third_lane = (lane + 2) % 4
            chart.append((hit, third_lane))
        if progress > 0.94 and rng.random() < 0.48:
            fourth_lane = (lane + 1) % 4
            chart.append((hit, fourth_lane))

    chart.sort()

    # Do not invent midpoint notes: every heart must correspond to an actual
    # detected audio onset so the visual timing stays locked to the music.
    return chart


def ass_header(width: int, height: int, scale: float) -> list[str]:
    hud_font = max(8, round(12 * scale))
    small_font = max(7, round(9 * scale))
    note_font = max(14, round(22 * scale))
    flash_font = max(9, round(11 * scale))
    return [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: HUD,Arial,{hud_font},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1",
        f"Style: Small,Arial,{small_font},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1",
        f"Style: Note,Arial,{note_font},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,5,0,0,0,1",
        f"Style: Flash,Arial,{flash_font},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,0,5,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]


def build_ass(
    duration: float,
    chart: list[tuple[float, int]],
    width: int,
    height: int,
    note_speed: float = 1.0,
) -> str:
    if not math.isfinite(note_speed) or note_speed <= 0:
        raise ValueError("note_speed must be a positive number")
    scale = min(width / REFERENCE_WIDTH, height / REFERENCE_HEIGHT)
    px = lambda value: max(1, round(value * scale))
    events = ass_header(width, height, scale)
    # One Taiko-style track. The target is fixed at the far left and every
    # note travels horizontally from the right across the full frame.
    track_y = height - px(52)
    # Leave enough room for the 66 px hit outline around the target.
    target_x = min(px(TARGET_CENTER_X), width - px(34))
    track_top = track_y - px(28)
    target_gap = px(25)
    events.append(f"Dialogue: 0,{ts(0)},{ts(duration)},HUD,,0,0,0,,{rect(0, track_top, width, px(56), '#06111c', 112)}")
    events.append(f"Dialogue: 1,{ts(0)},{ts(duration)},HUD,,0,0,0,,{line(target_x + target_gap, track_y - px(1), width - target_x - target_gap, px(2), '#ffffff', 78)}")
    events.append(f"Dialogue: 1,{ts(0)},{ts(duration)},HUD,,0,0,0,,{line(target_x + target_gap, track_y + px(1), width - target_x - target_gap, px(1), '#ff65c7', 110)}")

    # Small, unobtrusive status readout above the single track.
    header = (
        f"{{\\pos({px(10)},{track_top - px(18)})\\fs{px(11)}\\c{hex_ass_color('#ffffff')}\\bord{px(1)}\\3c{hex_ass_color('#000000')}}}"
        "RHYTHM TRACK"
        f"{{\\pos({px(103)},{track_top - px(18)})\\fs{px(10)}\\c{hex_ass_color('#31d7ff')}\\bord{px(1)}}}"
        "FOLLOW THE RHYTHM"
    )
    events.append(f"Dialogue: 3,{ts(0)},{ts(duration)},HUD,,0,0,0,,{header}")
    events.append(f"Dialogue: 3,{ts(0)},{ts(duration)},Small,,0,0,0,,{{\\pos({width - px(159)},{track_top - px(17)})\\c{hex_ass_color('#ff65c7')}\\bord{px(1)}}}♥ ♥ ♥")

    # One large bongo/receptor.  The target itself never moves.
    events.append(
        f"Dialogue: 2,{ts(0)},{ts(duration)},HUD,,0,0,0,,{{\\pos({target_x},{track_y})\\an5\\fs{px(31)}\\c{hex_ass_color('#ff65c7')}\\3c{hex_ass_color('#ffffff')}\\bord{px(2)}\\shad0}}♡"
    )
    events.append(
        f"Dialogue: 2,{ts(0)},{ts(duration)},HUD,,0,0,0,,{{\\pos({target_x},{track_y})\\an5\\fs{px(17)}\\c{hex_ass_color('#ffffff')}\\bord{px(1)}\\shad0}}♥"
    )

    # Deduplicate simultaneous four-lane events from the chart: Taiko has one
    # note stream, so a timestamp produces one visual note only.
    chart_hits = sorted({hit for hit, _ in chart})
    for hit in chart_hits:
        progress = hit / duration
        # Longer travel times make the notes easier to visually follow while
        # the chart itself still becomes denser as the video progresses.
        travel = (3.20 - 1.00 * progress) / note_speed
        start = max(0.0, hit - travel)
        end = min(duration, hit + 0.10)
        # If the normal travel begins before the video, the note has less
        # than the nominal travel time available. Adjust the move duration so
        # the first visible note still reaches the target at its hit time.
        visible_duration = hit - start
        note = (
            f"{{\\move({width + px(22)},{track_y},{target_x},{track_y},0,{int(visible_duration * 1000)})"
            f"\\c{hex_ass_color('#ffcf4a')}\\3c{hex_ass_color('#ffffff')}\\bord{px(2)}\\shad{px(1)}\\blur0.4}}♥"
        )
        events.append(f"Dialogue: 4,{ts(start)},{ts(end)},Note,,0,0,0,,{note}")

        # The flash marks the exact intended drum hit.
        flash = f"{{\\pos({target_x},{track_y})\\an5\\fs{px(35)}\\c{hex_ass_color('#ffffff')}\\3c{hex_ass_color('#ff65c7')}\\bord{px(2)}\\blur{px(1)}}}♥"
        events.append(f"Dialogue: 3,{ts(hit)},{ts(min(duration, hit + 0.12))},Flash,,0,0,0,,{flash}")

    # Scripted readout is only a visual progression indicator; it does not
    # claim that the viewer was detected or scored.
    for index, hit in enumerate(chart_hits):
        next_hit = chart_hits[index + 1] if index + 1 < len(chart_hits) else duration
        level = min(10, 1 + int(10 * hit / duration))
        readout = (
            f"{{\\pos({px(10)},{track_top + px(34)})\\fs{px(10)}\\c{hex_ass_color('#ffffff')}\\bord{px(1)}}}"
            f"LEVEL {level:02d}"
        )
        events.append(f"Dialogue: 3,{ts(hit)},{ts(next_hit)},HUD,,0,0,0,,{readout}")

    return "\n".join(events) + "\n"


def render(input_path: Path, ass_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            media_tool("ffmpeg"),
            "-y",
            "-v",
            "error",
            "-i",
            str(input_path),
            "-vf",
            f"subtitles=filename={filter_path(ass_path)}",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        check=True,
        creationflags=hidden_process_flags(),
    )


def parse_ffmpeg_time(value: str) -> float | None:
    """Convert FFmpeg's HH:MM:SS.microseconds progress value to seconds."""
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return None


def run_ffmpeg_with_progress(
    command: list[str],
    duration: float,
    progress_callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Run FFmpeg while reporting 0..1 progress and honoring cancellation."""
    full_command = [media_tool("ffmpeg"), "-progress", "pipe:1", "-nostats", *command[1:]]
    process = subprocess.Popen(
        full_command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=hidden_process_flags(),
    )
    recent_output: list[str] = []
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line:
                recent_output.append(line)
                recent_output = recent_output[-30:]
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise RenderCancelled()
            if line.startswith("out_time="):
                seconds = parse_ffmpeg_time(line.partition("=")[2])
                if seconds is not None and duration > 0 and progress_callback is not None:
                    progress_callback(min(1.0, max(0.0, seconds / duration)))
        return_code = process.wait()
        if cancel_event is not None and cancel_event.is_set():
            raise RenderCancelled()
        if return_code != 0:
            detail = "\n".join(recent_output) or f"FFmpeg exited with code {return_code}."
            raise RuntimeError(detail)
        if progress_callback is not None:
            progress_callback(1.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path, nargs="?", help="Defaults to output/<video-name>_rhythm_hud.mp4")
    parser.add_argument("--ass", type=Path, help="Defaults to .work/<video-name>_rhythm_hud.ass")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="Default rendered-video directory")
    parser.add_argument("--work-dir", type=Path, default=Path(".work"), help="Default generated-file directory")
    parser.add_argument("--bpm", type=float, help="Optional BPM used to align detected events to a beat grid")
    parser.add_argument("--phase-ms", type=float, default=0.0, help="Beat-grid phase when --bpm is supplied")
    args = parser.parse_args()

    stem = args.input.stem
    output_path = args.output or args.output_dir / f"{stem}_rhythm_hud.mp4"
    ass_path = args.ass or args.work_dir / f"{stem}_rhythm_hud.ass"
    duration, width, height = probe_video(args.input)
    # Detect events in separate musical regions so hearts can follow both
    # vocal attacks and electronic kick/hat transients.
    band_filters = [
        None,
        "highpass=f=180,lowpass=f=4200",  # vocal/midrange activity
        "lowpass=f=220",  # kick and bass pulse
        "highpass=f=3000",  # hats and bright electronic percussion
    ]
    onsets = []
    for audio_filter in band_filters:
        samples, sample_rate = extract_mono_audio(args.input, audio_filter)
        onsets.append(audio_onsets(samples, sample_rate))
    onsets = merge_onsets(onsets)
    if args.bpm:
        aligned_onsets = align_onsets_to_reference_grid(onsets, duration, args.bpm, args.phase_ms / 1000.0)
        if len(aligned_onsets) >= 12:
            onsets = aligned_onsets
    print(f"aligned_grid_events={len(onsets)}")
    chart = make_chart(onsets, duration)
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(build_ass(duration, chart, width, height), encoding="utf-8")
    print(f"duration={duration:.2f}s detected_onsets={len(onsets)} chart_notes={len(chart)}")
    print(f"video_size={width}x{height} ass={ass_path}")
    render(args.input, ass_path, output_path)
    print(f"output={output_path} bytes={output_path.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
