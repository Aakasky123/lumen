"""Synthesize every narration line with word timings (edge-tts, 96 kbps), then decode to 48 kHz WAV.

Lines are cached by a hash of (voice, rate, text): re-running only re-synthesizes changed lines.
"""

import asyncio
import hashlib
import inspect
import json
import pathlib
import subprocess
import sys
import types

import edge_tts.communicate as _cm
import imageio_ffmpeg

ROOT = pathlib.Path(__file__).resolve().parent.parent
VO = ROOT / "build" / "vo"
FF = imageio_ffmpeg.get_ffmpeg_exe()
RATE = "+0%"

# The stock client asks for 48 kbps; the service also serves 96 kbps for the same voice.
_src = inspect.getsource(_cm).replace(
    '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"',
    '"outputFormat":"audio-24khz-96kbitrate-mono-mp3"',
)
_mod = types.ModuleType("edge_tts.cm96")
_mod.__dict__["__package__"] = "edge_tts"
exec(compile(_src, "cm96", "exec"), _mod.__dict__)


async def synth(text: str, voice: str, mp3: pathlib.Path) -> list[dict]:
    comm = _mod.Communicate(text, voice, rate=RATE, boundary="WordBoundary")
    words: list[dict] = []
    with mp3.open("wb") as handle:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                handle.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append({
                    "t": round(chunk["offset"] / 1e7, 3),
                    "d": round(chunk["duration"] / 1e7, 3),
                    "w": chunk["text"],
                })
    return words


def decode(mp3: pathlib.Path, wav: pathlib.Path) -> float:
    subprocess.run(
        [FF, "-y", "-loglevel", "error", "-i", str(mp3), "-ac", "1", "-ar", "48000", "-c:a", "pcm_f32le", str(wav)],
        check=True,
    )
    probe = subprocess.run([FF, "-i", str(wav)], capture_output=True, text=True)
    line = next(l for l in probe.stderr.splitlines() if "Duration" in l)
    h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


async def main() -> None:
    script = json.loads((ROOT / "build" / "script.json").read_text(encoding="utf-8"))
    voice = script["voice"]
    VO.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[1:])
    for scene in script["scenes"]:
        for line_id, text, _gap in scene["lines"]:
            if only and line_id not in only:
                continue
            key = hashlib.sha256(f"{voice}|{RATE}|{text}".encode()).hexdigest()[:16]
            meta_path = VO / f"{line_id}.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("key") == key and (VO / f"{line_id}.wav").exists():
                    continue
            mp3 = VO / f"{line_id}.mp3"
            for attempt in range(4):
                try:
                    words = await synth(text, voice, mp3)
                    break
                except Exception as exc:  # network blips: retry
                    print(f"  retry {line_id}: {exc}")
                    await asyncio.sleep(2 + attempt * 2)
            else:
                raise SystemExit(f"failed to synthesize {line_id}")
            duration = decode(mp3, VO / f"{line_id}.wav")
            meta = {"id": line_id, "text": text, "key": key, "duration": duration, "words": words}
            meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
            print(f"{line_id}: {duration:5.2f}s  {len(words):2d} words  {text[:60]}")


if __name__ == "__main__":
    asyncio.run(main())
