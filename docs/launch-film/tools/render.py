"""Render the film: stills for review, or the full video.

  python tools/render.py stills 3.2 9.5 13.4          # PNGs in build/stills/
  python tools/render.py video --fps 60 --workers 6   # out/video_noaudio.mp4

Frames are captured from headless Chromium after a frame-exact seek; each worker pipes its
contiguous chunk of frames straight into its own x264 encoder, and the chunks are concatenated.
"""

import argparse
import base64
import json
import math
import multiprocessing as mp
import pathlib
import subprocess
import sys
import time

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FF = imageio_ffmpeg.get_ffmpeg_exe()


def assemble() -> pathlib.Path:
    body = (SRC / "film.html").read_text(encoding="utf-8")
    page = "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n" \
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n</head><body>\n" + body + "\n</body></html>\n"
    out = SRC / "render.html"
    out.write_text(page, encoding="utf-8")
    return out


def open_page(pw):
    browser = pw.chromium.launch(args=["--force-color-profile=srgb", "--disable-lcd-text", "--font-render-hinting=none",
                                       "--hide-scrollbars", "--disable-gpu-vsync"])
    ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    ctx.add_init_script("window.__RENDER__ = true;")
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto((SRC / "render.html").as_uri())
    page.wait_for_function("window.__ready !== undefined", timeout=30000)
    page.evaluate("() => window.__ready")
    page.wait_for_timeout(300)
    return browser, page, errors


def stills(times: list[float], tag: str = "") -> None:
    assemble()
    out = ROOT / "build" / "stills"
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser, page, errors = open_page(pw)
        for t in times:
            page.evaluate("t => window.__seek(t)", t)
            page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
            path = out / f"{tag}t{t:07.2f}.png"
            page.screenshot(path=str(path))
            print(path)
        for e in errors[:20]:
            print("  ", e)
        browser.close()


def _worker(args):
    idx, f0, f1, fps, chunk = args
    with sync_playwright() as pw:
        browser, page, errors = open_page(pw)
        cdp = page.context.new_cdp_session(page)
        enc = subprocess.Popen(
            [FF, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps), "-c:v", "png", "-i", "-",
             "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
             "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-tune", "film", "-g", str(fps * 2),
             "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
             "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
             str(chunk)],
            stdin=subprocess.PIPE,
        )
        t_start = time.time()
        for f in range(f0, f1):
            t = f / fps
            page.evaluate("t => window.__seek(t)", t)
            shot = cdp.send("Page.captureScreenshot", {"format": "png", "optimizeForSpeed": True, "captureBeyondViewport": False})
            enc.stdin.write(base64.b64decode(shot["data"]))
            if (f - f0) % (fps * 10) == 0:
                done = f - f0
                rate = done / max(1e-6, time.time() - t_start)
                print(f"[w{idx}] frame {f} ({done}/{f1 - f0}) {rate:.1f} fps", flush=True)
        enc.stdin.close()
        enc.wait()
        if errors:
            print(f"[w{idx}] console:", errors[:5], flush=True)
        browser.close()
    return str(chunk)


def video(fps: int, workers: int, t0: float, t1: float | None, name: str) -> None:
    assemble()
    tl = json.loads((ROOT / "build" / "timeline.json").read_text(encoding="utf-8"))
    dur = tl["duration"] if t1 is None else t1
    f_start = int(math.floor(t0 * fps))
    f_end = int(math.ceil(dur * fps))
    n = f_end - f_start
    per = math.ceil(n / workers)
    outdir = ROOT / "out"
    outdir.mkdir(exist_ok=True)
    jobs = []
    for i in range(workers):
        a = f_start + i * per
        b = min(f_end, a + per)
        if a >= b:
            break
        jobs.append((i, a, b, fps, outdir / f"chunk_{name}_{i:02d}.mp4"))
    t_begin = time.time()
    with mp.get_context("spawn").Pool(len(jobs)) as pool:
        chunks = pool.map(_worker, jobs)
    lst = outdir / f"chunks_{name}.txt"
    lst.write_text("".join(f"file '{pathlib.Path(c).as_posix()}'\n" for c in chunks), encoding="utf-8")
    final = outdir / f"{name}.mp4"
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(final)], check=True)
    for c in chunks:
        pathlib.Path(c).unlink(missing_ok=True)
    lst.unlink(missing_ok=True)
    print(f"wrote {final} ({n} frames in {time.time() - t_begin:.0f}s)")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("stills")
    a.add_argument("times", nargs="+", type=float)
    a.add_argument("--tag", default="")
    b = sub.add_parser("video")
    b.add_argument("--fps", type=int, default=30)
    b.add_argument("--workers", type=int, default=6)
    b.add_argument("--from", dest="t0", type=float, default=0.0)
    b.add_argument("--to", dest="t1", type=float, default=None)
    b.add_argument("--name", default="video_noaudio")
    args = ap.parse_args()
    if args.cmd == "stills":
        stills(args.times, args.tag)
    else:
        video(args.fps, args.workers, args.t0, args.t1, args.name)


if __name__ == "__main__":
    sys.exit(main())
