"""Render a recorded rhythm sheet as the FlexScale heart HUD.

The selected video's basename determines default sheet, work, and output
filenames. Paths, timing corrections, grid phase, and heart artwork remain
overridable for reuse with unrelated videos.
"""

from __future__ import annotations

import argparse
import csv
import json
import threading
from pathlib import Path
from typing import Callable

from . import rhythm_hud as make_rhythm_hud


DEFAULT_HIT_OFFSET_SECONDS = -0.200
# The source clips are commonly 30 fps.  Compositing the moving sprite at a
# higher constant rate gives the overlay position more frequent updates,
# without changing the source video's timing or the chart's hit timestamps.
HEART_RENDER_FPS = 60


def load_events(sheet_path: Path) -> tuple[list[tuple[float, int]], float | None, float | None]:
    if sheet_path.suffix.lower() == ".json":
        payload = json.loads(sheet_path.read_text(encoding="utf-8"))
        events = payload.get("events", [])
        bpm = payload.get("bpm")
        recorded_duration = payload.get("duration_seconds")
    else:
        events = []
        bpm = None
        recorded_duration = None
        with sheet_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                events.append(row)

    chart: list[tuple[float, int]] = []
    for event in events:
        try:
            timestamp = float(event["time_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp >= 0:
            chart.append((timestamp, 0))
    chart.sort()
    return chart, bpm, float(recorded_duration) if recorded_duration is not None else None


def quantize_events(chart: list[tuple[float, int]], bpm: float, phase_seconds: float) -> list[tuple[float, int]]:
    """Snap each event independently to the nearest 16th-note grid position."""
    subdivision = (60.0 / bpm) / 4.0
    quantized = []
    for hit, lane in chart:
        grid_index = round((hit - phase_seconds) / subdivision)
        quantized.append((round(phase_seconds + grid_index * subdivision, 4), lane))
    return quantized


def sprite_ass(duration: float, chart: list[tuple[float, int]], width: int, height: int) -> str:
    """Build the HUD without the glyph hearts or the old spark animation."""
    base = make_rhythm_hud.build_ass(duration, [], width, height)
    lines = [
        line
        for line in base.splitlines()
        if not line.startswith("Dialogue: 2,") and ",Small," not in line
    ]
    return "\n".join(lines) + "\n"


def render_with_heart_sprite(
    video_path: Path,
    ass_path: Path,
    output_path: Path,
    heart_path: Path,
    chart: list[tuple[float, int]],
    duration: float,
    width: int,
    height: int,
    filter_script: Path,
    progress_callback: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Render the HUD and animate the supplied raster heart as every note."""
    scale = min(width / make_rhythm_hud.REFERENCE_WIDTH, height / make_rhythm_hud.REFERENCE_HEIGHT)
    px = lambda value: max(1, round(value * scale))
    track_y = height - px(52)
    target_x = width // 2
    hits = sorted({hit for hit, _ in chart})
    note_labels = [f"n{index}" for index in range(len(hits))]
    outline_label = "v1"
    hit_label = "v2"
    final_label = f"v{len(hits) + 2}"
    graph = [
        f"[0:v]fps={HEART_RENDER_FPS},subtitles=filename={make_rhythm_hud.filter_path(ass_path)}[base];",
        "[1:v]format=rgba,split=4[note_source][target_source][outline_source][hit_source];",
        f"[note_source]scale={px(30)}:-1,split={max(1, len(hits))}" + "".join(f"[{label}]" for label in note_labels) + ";",
        f"[target_source]lutrgb=r=255:g=255:b=255,scale={px(42)}:-1[target];",
        f"[outline_source]lutrgb=r=255:g=255:b=255,scale={px(66)}:-1[hit_outline];",
        f"[hit_source]lutrgb=r=164:g=72:b=214,scale={px(56)}:-1[hit];",
        f"[base][target]overlay=x='{target_x}-w/2':y='{track_y}-h/2':shortest=1:eof_action=pass[v0];",
    ]

    hit_windows = "+".join(
        f"(gte(t\\,{hit:.4f})*lte(t\\,{min(duration, hit + 0.12):.4f}))"
        for hit in hits
    )
    graph.append(
        f"[v0][hit_outline]overlay=x='{target_x}-w/2':y='{track_y}-h/2':shortest=1:eof_action=pass:"
        f"enable='{hit_windows}'[{outline_label}];"
    )
    graph.append(
        f"[{outline_label}][hit]overlay=x='{target_x}-w/2':y='{track_y}-h/2':shortest=1:eof_action=pass:"
        f"enable='{hit_windows}'[{hit_label}];"
    )

    previous = hit_label
    for index, hit in enumerate(hits):
        progress = hit / duration
        travel = 3.20 - 1.00 * progress
        start = max(0.0, hit - travel)
        label = f"v{index + 3}"
        x_expression = f"{width}+{px(22)}-w/2-({width}+{px(22)}-{target_x})*(t-{start:.4f})/{travel:.4f}"
        graph.append(
            f"[{previous}][{note_labels[index]}]overlay="
            f"x='{x_expression}':y='{track_y}-h/2':shortest=1:eof_action=pass:"
            f"enable='gte(t\\,{start:.4f})*lte(t\\,{hit:.4f})'[{label}];"
        )
        previous = label


    filter_script.parent.mkdir(parents=True, exist_ok=True)
    filter_script.write_text("\n".join(graph), encoding="utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    make_rhythm_hud.run_ffmpeg_with_progress(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(video_path),
            "-loop", "1", "-framerate", str(HEART_RENDER_FPS), "-i", str(heart_path),
            "-filter_complex_script", str(filter_script),
            "-map", f"[{final_label}]", "-map", "0:a:0?", "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "copy", str(output_path),
        ],
        duration,
        progress_callback,
        cancel_event,
    )


def render_rhythm_video(
    video_path: Path,
    sheet_path: Path,
    output_path: Path,
    work_dir: Path,
    heart_path: Path | None = None,
    progress_callback: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
    offset_seconds: float = DEFAULT_HIT_OFFSET_SECONDS,
) -> None:
    """Render one matched video/sheet pair to an explicitly selected output."""
    if cancel_event is not None and cancel_event.is_set():
        raise make_rhythm_hud.RenderCancelled()
    duration, width, height = make_rhythm_hud.probe_video(video_path)
    chart, bpm, _ = load_events(sheet_path)
    if bpm:
        chart = quantize_events(chart, bpm, 0.0)
    chart = [
        (hit + offset_seconds, lane)
        for hit, lane in chart
        if 0.0 <= hit + offset_seconds < duration - 0.25
    ]
    if not chart:
        raise ValueError(f"The guiding sheet contains no usable events: {sheet_path.name}")

    work_dir.mkdir(parents=True, exist_ok=True)
    ass_path = work_dir / f"{video_path.stem}_rhythm_hud.ass"
    filter_script = work_dir / f"{video_path.stem}_heart_sprite_filter.txt"
    ass_path.write_text(
        sprite_ass(duration, chart, width, height)
        if heart_path
        else make_rhythm_hud.build_ass(duration, chart, width, height),
        encoding="utf-8",
    )
    if cancel_event is not None and cancel_event.is_set():
        raise make_rhythm_hud.RenderCancelled()
    if heart_path:
        render_with_heart_sprite(
            video_path,
            ass_path,
            output_path,
            heart_path,
            chart,
            duration,
            width,
            height,
            filter_script,
            progress_callback,
            cancel_event,
        )
    else:
        command = [
            "ffmpeg", "-y", "-v", "error", "-i", str(video_path),
            "-vf", f"subtitles=filename={make_rhythm_hud.filter_path(ass_path)}",
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
            "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", str(output_path),
        ]
        make_rhythm_hud.run_ffmpeg_with_progress(
            command, duration, progress_callback, cancel_event
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True, help="Video to receive the HUD")
    parser.add_argument("--sheet", type=Path, help="Defaults to guiding_sheets/<video-name>.json")
    parser.add_argument("--output", type=Path, help="Defaults to output/<video-name>_rhythm_hud.mp4")
    parser.add_argument("--ass", type=Path, help="Defaults to .work/<video-name>_rhythm_hud.ass")
    parser.add_argument("--sheet-dir", type=Path, default=Path("guiding_sheets"), help="Default guiding-sheet directory")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="Default rendered-video directory")
    parser.add_argument("--work-dir", type=Path, default=Path(".work"), help="Directory for generated filter files")
    parser.add_argument(
        "--offset-ms",
        type=float,
        default=DEFAULT_HIT_OFFSET_SECONDS * 1000.0,
        help="Shift hit times in milliseconds; defaults to -200 so hearts hit 200 ms earlier",
    )
    parser.add_argument("--phase-ms", type=float, default=0.0, help="Beat-grid phase in milliseconds")
    parser.add_argument("--no-quantize", action="store_true", help="Keep raw recorded timestamps instead of snapping to the BPM grid")
    parser.add_argument(
        "--icon-image",
        "--heart-image",
        dest="heart_image",
        type=Path,
        help="Use a PNG icon sprite for the target and moving notes",
    )
    args = parser.parse_args()

    stem = args.video.stem
    sheet_path = args.sheet or args.sheet_dir / f"{stem}.json"
    output_path = args.output or args.output_dir / f"{stem}_rhythm_hud.mp4"
    ass_path = args.ass or args.work_dir / f"{stem}_rhythm_hud.ass"
    filter_script = args.work_dir / f"{stem}_heart_sprite_filter.txt"
    if not args.video.is_file():
        parser.error(f"video not found: {args.video}")
    if not sheet_path.is_file():
        csv_fallback = sheet_path.with_suffix(".csv")
        if args.sheet is None and csv_fallback.is_file():
            sheet_path = csv_fallback
        else:
            parser.error(f"guiding sheet not found: {sheet_path}")
    if args.heart_image and not args.heart_image.is_file():
        parser.error(f"heart image not found: {args.heart_image}")

    duration, width, height = make_rhythm_hud.probe_video(args.video)
    chart, bpm, recorded_duration = load_events(sheet_path)
    if bpm and not args.no_quantize:
        chart = quantize_events(chart, bpm, args.phase_ms / 1000.0)
    offset_seconds = args.offset_ms / 1000.0
    chart = [(hit + offset_seconds, lane) for hit, lane in chart if 0.0 <= hit + offset_seconds < duration - 0.25]
    if not chart:
        parser.error(f"guiding sheet contains no usable events: {sheet_path}")
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(
        sprite_ass(duration, chart, width, height)
        if args.heart_image
        else make_rhythm_hud.build_ass(duration, chart, width, height),
        encoding="utf-8",
    )
    print(f"video_duration={duration:.3f}s sheet_duration={recorded_duration}")
    print(f"video_size={width}x{height} sheet={sheet_path}")
    print(f"sheet_bpm={bpm} rendered_events={len(chart)}")
    if args.heart_image:
        render_with_heart_sprite(
            args.video,
            ass_path,
            output_path,
            args.heart_image,
            chart,
            duration,
            width,
            height,
            filter_script,
        )
    else:
        make_rhythm_hud.render(args.video, ass_path, output_path)
    print(f"output={output_path} bytes={output_path.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
