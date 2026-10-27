"""Mux the rendered picture with the soundtrack, cut share, captioned and phone versions, write SRT captions.

  python tools/finalize.py <destination folder>
"""

import json
import pathlib
import shutil
import subprocess
import sys

import imageio_ffmpeg

ROOT = pathlib.Path(__file__).resolve().parent.parent
FF = imageio_ffmpeg.get_ffmpeg_exe()


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(path: pathlib.Path) -> None:
    tl = json.loads((ROOT / "build" / "timeline.json").read_text(encoding="utf-8"))
    lines = sorted(tl["lines"].values(), key=lambda l: l["start"])
    out = []
    for i, l in enumerate(lines, 1):
        start, end = l["start"] - 0.05, l["end"] + 0.3
        if i < len(lines):
            end = min(end, lines[i]["start"] - 0.06)
        out.append(f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{l['text']}\n")
    path.write_text("\n".join(out), encoding="utf-8")


def ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def write_ass(path: pathlib.Path) -> None:
    """Burn-in captions: a semi-transparent box, bottom center, system UI font."""
    tl = json.loads((ROOT / "build" / "timeline.json").read_text(encoding="utf-8"))
    lines = sorted(tl["lines"].values(), key=lambda l: l["start"])
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Segoe UI Semibold,39,&H00FFFFFF,&H00FFFFFF,&H48000000,&H48000000,0,0,0,0,100,100,0,0,3,14,0,2,280,280,54,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    for i, l in enumerate(lines):
        start, end = l["start"] - 0.05, l["end"] + 0.3
        if i + 1 < len(lines):
            end = min(end, lines[i + 1]["start"] - 0.06)
        ev.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,{l['text']}")
    path.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


def mobile_cut(master: pathlib.Path, out: pathlib.Path) -> None:
    """720p cut under 30 MiB, the attachment limit for phone viewers: two-pass at a fixed bitrate."""
    common = [FF, "-y", "-loglevel", "error", "-i", str(master), "-vf", "fps=30,scale=1280:720:flags=lanczos",
              "-c:v", "libx264", "-preset", "slow", "-b:v", "720k", "-maxrate", "1400k", "-bufsize", "2800k",
              "-passlogfile", str(ROOT / "out" / "mobile2pass")]
    subprocess.run(common + ["-pass", "1", "-an", "-f", "null", "-"], check=True)
    subprocess.run(common + ["-pass", "2", "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
                             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)],
                   check=True)


def main() -> None:
    dest = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out"
    dest.mkdir(parents=True, exist_ok=True)
    video = ROOT / "out" / "video_noaudio.mp4"
    audio = ROOT / "out" / "soundtrack.m4a"
    master = dest / "Lumen-Launch-Film-1080p60.mp4"
    subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "copy", "-shortest", "-movflags", "+faststart",
                    "-metadata", "title=Lumen: launch film", "-metadata", "comment=The system as of October 1, 2026", str(master)], check=True)
    share = dest / "Lumen-Launch-Film-1080p30-share.mp4"
    subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(master), "-vf", "fps=30", "-c:v", "libx264", "-preset", "slow", "-crf", "23",
                    "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(share)], check=True)
    write_srt(dest / "Lumen-Launch-Film.en.srt")
    ass = ROOT / "out" / "captions.ass"
    write_ass(ass)
    captioned = dest / "Lumen-Launch-Film-1080p30-captioned.mp4"
    subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(master), "-vf", "fps=30,subtitles=captions.ass",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "23", "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(captioned)],
                   check=True, cwd=str(ROOT / "out"))
    mobile_cut(master, dest / "Lumen-Launch-Film-720p-mobile.mp4")
    shutil.copy(ROOT / "src" / "soundtrack.mp3", dest / "Lumen-Launch-Film-soundtrack.mp3")
    for f in sorted(dest.iterdir()):
        print(f"{f.name:42s} {f.stat().st_size / 1e6:8.1f} MB")


if __name__ == "__main__":
    main()
