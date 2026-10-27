"""Contact sheets: one small frame per step across a time range, labeled, for flow review."""
import io
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from render import assemble, SRC  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
t0, t1, step = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
tag = sys.argv[4] if len(sys.argv) > 4 else "sheet"
assemble()
times = []
t = t0
while t <= t1 + 1e-6:
    times.append(round(t, 3)); t += step
TW, TH, COLS = 384, 216, 5
with sync_playwright() as pw:
    b = pw.chromium.launch(args=["--force-color-profile=srgb"])
    ctx = b.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=0.2)
    ctx.add_init_script("window.__RENDER__ = true;")
    p = ctx.new_page()
    p.goto((SRC / "render.html").as_uri())
    p.wait_for_function("window.__ready !== undefined"); p.evaluate("() => window.__ready"); p.wait_for_timeout(300)
    thumbs = []
    for t in times:
        p.evaluate("t => window.__seek(t)", t)
        img = Image.open(io.BytesIO(p.screenshot())).convert("RGB").resize((TW, TH))
        d = ImageDraw.Draw(img); d.rectangle([0, 0, 64, 18], fill=(0, 0, 0)); d.text((4, 3), f"{t:6.2f}", fill=(255, 220, 0))
        thumbs.append(img)
    b.close()
rows = (len(thumbs) + COLS - 1) // COLS
sheet = Image.new("RGB", (COLS * TW, rows * TH), (40, 40, 40))
for i, im in enumerate(thumbs):
    sheet.paste(im, ((i % COLS) * TW, (i // COLS) * TH))
out = ROOT / "build" / f"{tag}.png"
sheet.save(out); print(out, len(thumbs))
