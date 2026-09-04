# FlexScale Rhythm Video

Run `python main.py` or open `dist/FlexScale.exe`.

Enter a video filename (or just its name without the extension). The client
loads the video from `input`, finds the same-named `.json` or `.csv` file in
`guiding_sheets`, and writes the rendered video to `output`.

The **Create Sheet** tab uses the same video-name lookup. Click **Arm / Reset**,
press **F9** to open the video and establish time zero, tap **Down** on each
beat, press **F10** to stop, and click **Save Sheet**. The matching JSON and CSV
files are written to `guiding_sheets`. Existing sheets require confirmation
before they are replaced.

Outputs are never overwritten. For example, repeated renders of `video.mp4`
produce `video.mp4`, `video_2.mp4`, `video_3.mp4`, and so on.

On Windows, the render progress is also shown on the application's taskbar
button.

Every generated chart applies a 200 ms early-hit timing correction. The
**Generate Video** tab includes an optional offset adjustment in milliseconds:
`0` keeps that default timing, positive values delay the hearts, and negative
values make them start earlier. The center icon is white at rest and becomes
purple with a white outline on each beat.

## Custom icon

`assets/note.png` is the game's icon asset and is included in Git. Replace it
with any transparent PNG, such as a sword or star, while keeping the filename
`note.png`. New renders use the replacement automatically: the moving notes
use its original colors, while the center target is white at rest and purple on
each hit.

The EXE looks for `input`, `guiding_sheets`, and `output` beside itself, in its
working directory, or one directory above it. This means the built
`dist/FlexScale.exe` works directly with this project's folders.

To rebuild the self-contained Windows executable (including FFmpeg and
FFprobe), run:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```
