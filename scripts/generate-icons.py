"""Generate the Lumen brand icons for the desktop app, the Chrome extension, and the Android app.

The mark is four upright slabs of light, the inner pair set slightly lower. It is
drawn once at high resolution and downsampled, since PIL's primitives are not antialiased.
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
DESKTOP_BUILD = ROOT / "apps" / "desktop" / "build"
EXTENSION_ICONS = ROOT / "apps" / "extension" / "icons"
MOBILE_ASSETS = ROOT / "apps" / "mobile" / "assets"

MASTER = 1024
TILE = "#0d1f1a"
TILE_EDGE = "#1c3a31"
SLAB = "#5ee6bd"
SLAB_SHADE = "#3fbf99"


def draw_slabs(draw: ImageDraw.ImageDraw, scale: float = 1.0, center: float = MASTER / 2, mono: str | None = None) -> None:
    """The four slabs, laid out on the 1024 master grid, scaled about the canvas centre."""

    def at(value: float) -> float:
        return center + (value - MASTER / 2) * scale

    left, right, top, bottom, gap = 212, 812, 200, 824, 40
    width = (right - left - gap * 3) / 4
    for index in range(4):
        x0 = left + index * (width + gap)
        # The inner pair sits slightly lower.
        offset = 36 if index in (1, 2) else 0
        box = (at(x0), at(top + offset), at(x0 + width), at(bottom - (36 - offset)))
        fill = mono or (SLAB if index in (0, 3) else SLAB_SHADE)
        draw.rounded_rectangle(box, radius=26 * scale, fill=fill)


def draw_master() -> Image.Image:
    image = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, MASTER - 1, MASTER - 1), radius=228, fill=TILE_EDGE)
    draw.rounded_rectangle((28, 28, MASTER - 29, MASTER - 29), radius=204, fill=TILE)
    draw_slabs(draw)
    return image


def draw_slabs_only(scale: float, mono: str | None = None) -> Image.Image:
    image = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    draw_slabs(ImageDraw.Draw(image), scale=scale, mono=mono)
    return image


def write_mobile_assets(master: Image.Image) -> None:
    MOBILE_ASSETS.mkdir(parents=True, exist_ok=True)
    # Full-bleed square tile: Android masks launcher icons itself.
    square = Image.new("RGBA", (MASTER, MASTER), TILE)
    draw_slabs(ImageDraw.Draw(square), scale=0.8)
    square.save(MOBILE_ASSETS / "icon.png")
    # Adaptive icon layers: the slabs sit inside the 66% safe zone over a solid tile colour.
    draw_slabs_only(0.56).save(MOBILE_ASSETS / "android-icon-foreground.png")
    Image.new("RGBA", (MASTER, MASTER), TILE).save(MOBILE_ASSETS / "android-icon-background.png")
    draw_slabs_only(0.56, mono="#ffffff").save(MOBILE_ASSETS / "android-icon-monochrome.png")
    # Status-bar notification glyph: white on transparent.
    draw_slabs_only(0.9, mono="#ffffff").resize((96, 96), Image.LANCZOS).save(MOBILE_ASSETS / "notification-icon.png")
    draw_slabs_only(0.9).resize((512, 512), Image.LANCZOS).save(MOBILE_ASSETS / "splash-icon.png")
    master.resize((48, 48), Image.LANCZOS).save(MOBILE_ASSETS / "favicon.png")


def main() -> None:
    master = draw_master()
    DESKTOP_BUILD.mkdir(parents=True, exist_ok=True)
    EXTENSION_ICONS.mkdir(parents=True, exist_ok=True)

    master.resize((512, 512), Image.LANCZOS).save(DESKTOP_BUILD / "icon.png")
    master.save(
        DESKTOP_BUILD / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    for size in (16, 32, 48, 128):
        master.resize((size, size), Image.LANCZOS).save(EXTENSION_ICONS / f"icon{size}.png")
    write_mobile_assets(master)


if __name__ == "__main__":
    main()
