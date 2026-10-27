"""Export the sound cue sheet (FILM.collectCues) from the film page to build/cues.json."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from render import assemble, open_page  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
assemble()
with sync_playwright() as pw:
    browser, page, errors = open_page(pw)
    cues = page.evaluate("() => window.FILM.collectCues()")
    browser.close()
(ROOT / "build" / "cues.json").write_text(json.dumps(cues, indent=0), encoding="utf-8")
print(len(cues), "cues;", sorted({c["kind"] for c in cues}))
for e in errors[:10]:
    print("  ", e)
