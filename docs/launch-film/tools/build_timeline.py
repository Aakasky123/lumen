"""Lay the narration out in time and snap every scene cut to the music's beat grid.

Output: build/timeline.json (read by the film, the music generator, and the mixer) and
src/timeline.js (the same data as a script the film page loads).
"""

import json
import math
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
BPM = 96.0
BEAT = 60.0 / BPM
PRE = 0.05   # audio kept before the first word
POST = 0.22  # audio kept after the last word (decay)


def ceil_beat(t: float) -> float:
    return math.ceil(round(t / BEAT, 6)) * BEAT


def main() -> None:
    script = json.loads((ROOT / "build" / "script.json").read_text(encoding="utf-8"))
    vo = ROOT / "build" / "vo"
    t = 0.0
    scenes, lines = [], {}
    for scene in script["scenes"]:
        start = ceil_beat(t)
        cursor = start + scene["lead"]
        for line_id, text, gap in scene["lines"]:
            meta = json.loads((vo / f"{line_id}.json").read_text(encoding="utf-8"))
            words = meta["words"]
            first = words[0]["t"]
            speech_end = words[-1]["t"] + words[-1]["d"]
            clip_in = max(0.0, first - PRE)
            clip_out = min(meta["duration"], speech_end + POST)
            clip_start = cursor - first  # the clip's own t=0 lands here
            lines[line_id] = {
                "id": line_id,
                "scene": scene["id"],
                "text": text,
                "start": round(cursor, 3),
                "end": round(clip_start + speech_end, 3),
                "clip_start": round(clip_start, 3),
                "clip_in": round(clip_in, 3),
                "clip_out": round(clip_out, 3),
                "words": [{"t": round(clip_start + w["t"], 3), "d": w["d"], "w": w["w"]} for w in words],
            }
            cursor = clip_start + speech_end + gap
        end = cursor + scene["tail"]
        scenes.append({
            "id": scene["id"],
            "chapter": scene["chapter"],
            "start": round(start, 3),
            "end": round(end, 3),
            "lines": [lid for lid, _, _ in scene["lines"]],
        })
        t = end
    duration = ceil_beat(t)
    scenes[-1]["end"] = round(duration, 3)
    timeline = {"bpm": BPM, "beat": BEAT, "duration": round(duration, 3), "scenes": scenes, "lines": lines}
    (ROOT / "build" / "timeline.json").write_text(json.dumps(timeline, indent=1), encoding="utf-8")
    (ROOT / "src" / "timeline.js").write_text("window.TIMELINE = " + json.dumps(timeline) + ";\n", encoding="utf-8")
    for s in scenes:
        print(f"{s['id']:9s} {s['start']:7.2f} -> {s['end']:7.2f}  ({s['end'] - s['start']:5.2f}s)  {s['chapter']}")
    print(f"duration {duration:.2f}s = {int(duration // 60)}:{duration % 60:05.2f}")


if __name__ == "__main__":
    main()
