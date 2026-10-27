"""The macOS Vision backend of screen_ocr, with the Vision, Quartz and Foundation layers faked.

The live test (LUMEN_MAC_LIVE=1 on macOS) renders real text and reads it back through pyobjc.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from app import screen_ocr

PNG = b"\x89PNG\r\n\x1a\n-fake-page"
JPEG = b"\xff\xd8\xff-fake-jpeg"
UNREADABLE = b"not an image at all"


class Rect:
    """A Vision rectangle: normalized, bottom-left origin."""

    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        self.origin = SimpleNamespace(x=x, y=y)
        self.size = SimpleNamespace(width=w, height=h)


class Box:
    """A VNRectangleObservation returned for one word range."""

    def __init__(self, rect: Rect) -> None:
        self._rect = rect

    def boundingBox(self) -> Rect:
        return self._rect


class Candidate:
    def __init__(self, text: str, word_boxes=None) -> None:
        self._text = text
        self._word_boxes = word_boxes  # callable (location, length) -> Rect | None

    def string(self) -> str:
        return self._text

    def boundingBoxForRange_error_(self, given, _error):
        rect = self._word_boxes(*given) if self._word_boxes else None
        return (Box(rect) if rect is not None else None), None


class Observation:
    def __init__(self, text: str, box: Rect, word_boxes=None) -> None:
        self._candidate = Candidate(text, word_boxes)
        self._box = box

    def boundingBox(self) -> Rect:
        return self._box

    def topCandidates_(self, count: int):
        return [self._candidate]


class FakeImage:
    def __init__(self, width: int, height: int, observations=(), *, fail: bool = False) -> None:
        self.width, self.height = width, height
        self.observations = list(observations)
        self.fail = fail


class FakeVision:
    """Records what the production code asked of the framework, so tests can assert on it."""

    def __init__(self, images: dict[bytes, FakeImage]) -> None:
        self.images = images
        self.handler_options: list[object] = []
        self.levels: list[object] = []
        self.language_correction: list[object] = []
        self.performed: list[bytes] = []
        self.block: threading.Event | None = None

        outer = self

        class Request:
            def __init__(self) -> None:
                self._results: list[Observation] = []

            @classmethod
            def alloc(cls):
                return cls()

            def init(self):
                return self

            def setRecognitionLevel_(self, level):
                outer.levels.append(level)

            def setUsesLanguageCorrection_(self, value):
                outer.language_correction.append(value)

            def results(self):
                return self._results

        class Handler:
            @classmethod
            def alloc(cls):
                return cls.__new__(cls)

            def initWithCGImage_options_(self, image, options):
                self.image = image
                outer.handler_options.append(options)
                return self

            def performRequests_error_(self, requests, _error):
                if outer.block is not None and not outer.block.is_set():
                    outer.block.wait(5)
                if self.image.fail:
                    return False, None
                for request in requests:
                    request._results = list(self.image.observations)
                return True, None

        self.Request = Request
        self.Handler = Handler

    def modules(self):
        outer = self

        class VN:
            VNRecognizeTextRequest = outer.Request
            VNImageRequestHandler = outer.Handler
            VNRequestTextRecognitionLevelAccurate = 0

        class Quartz:
            @staticmethod
            def CGImageSourceCreateWithData(data, _options):
                return data

            @staticmethod
            def CGImageSourceCreateImageAtIndex(data, _index, _options):
                if data.startswith(b"\x89PNG") and data not in outer.images:
                    outer.performed.append(data)
                    return FakeImage(64, 64)  # the warm-up's blank image
                outer.performed.append(data)
                return outer.images.get(data)

            @staticmethod
            def CGImageGetWidth(image):
                return image.width

            @staticmethod
            def CGImageGetHeight(image):
                return image.height

        class Foundation:
            class NSData:
                @staticmethod
                def dataWithBytes_length_(data, length):
                    return data[:length]

            @staticmethod
            def NSMakeRange(location, length):
                return (location, length)

        class Objc:
            @staticmethod
            def autorelease_pool():
                from contextlib import nullcontext

                return nullcontext()

        return VN, Quartz, Foundation, Objc


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    screen_ocr._cache.clear()
    screen_ocr._words_cache.clear()
    yield
    screen_ocr._cache.clear()
    screen_ocr._words_cache.clear()


@pytest.fixture
def vision(monkeypatch):
    """Pretends to be macOS with Vision installed. Returns install(images), which maps each image's bytes
    to the FakeImage it decodes to, and returns the recording FakeVision."""

    def install(images: dict[bytes, FakeImage]) -> FakeVision:
        backend = FakeVision(images)
        monkeypatch.setattr(screen_ocr, "_is_macos", lambda: True)
        monkeypatch.setattr(screen_ocr, "_load_vision", lambda: backend.modules())
        return backend

    return install


def _line(text: str, box: tuple[float, float, float, float], word_boxes=None) -> Observation:
    x, y, w, h = box
    return Observation(text, Rect(x, y, w, h), word_boxes)


# A 1000 x 500 page: the first line sits near the top, the second near the bottom.
PAGE = FakeImage(
    1000,
    500,
    [
        _line("customs release", (0.5, 0.80, 0.4, 0.1)),  # top band, right: read second
        _line("INV-1881 form", (0.05, 0.80, 0.4, 0.1)),  # top band, left: read first
        _line("ZKDX-161085-2", (0.05, 0.10, 0.5, 0.1)),  # bottom of the page
    ],
)


def test_box_flips_normalized_bottom_left_to_top_left_pixels():
    x, y, w, h = screen_ocr._pixel_box(Rect(0.1, 0.8, 0.2, 0.1), 1000, 500)
    assert (x, y, w, h) == pytest.approx((100.0, 50.0, 200.0, 50.0))


def test_word_boxes_come_from_vision_ranges_counted_in_utf16_units(vision):
    # "😀" is two UTF-16 units, so the second word's range starts at 3, not 2.
    def word_boxes(location, length):
        if (location, length) == (3, 3):  # the word "INV" after the emoji
            return Rect(0.5, 0.5, 0.25, 0.1)
        return None

    page = FakeImage(1000, 500, [_line("😀 INV", (0.0, 0.0, 1.0, 1.0), word_boxes)])
    vision({PNG: page})
    words = screen_ocr.read_text_lines([PNG])[0][0]["words"]
    assert words[1] == {"t": "INV", "x": 500.0, "y": 200.0, "w": 250.0, "h": 50.0}


def test_result_shape_matches_the_windows_reading(vision):
    backend = vision({PNG: PAGE})
    lines = screen_ocr.read_text_lines([PNG])
    assert isinstance(lines, list) and len(lines) == 1
    for line in lines[0]:
        assert set(line) == {"text", "words"}
        assert line["words"], "a line without words is dropped, as on Windows"
        for word in line["words"]:
            assert set(word) == {"t", "x", "y", "w", "h"}
            assert all(isinstance(word[key], float) for key in "xywh")
            assert isinstance(word["t"], str)
    assert [line["text"] for line in lines[0]] == ["INV-1881 form", "customs release", "ZKDX-161085-2"]
    assert screen_ocr.read_lines([PNG]) == [["INV-1881 form", "customs release", "ZKDX-161085-2"]]
    assert backend.levels == [0] and backend.language_correction == [False]
    assert backend.handler_options == [None]


def test_reading_order_is_top_to_bottom_then_left_to_right(vision):
    vision({PNG: PAGE})
    texts = [line["text"] for line in screen_ocr.read_text_lines([PNG])[0]]
    assert texts == ["INV-1881 form", "customs release", "ZKDX-161085-2"]


def test_word_boxes_map_into_the_text_lines_with_their_pixel_positions(vision):
    vision({PNG: PAGE})
    words = screen_ocr.read_text_lines([PNG])[0][2]["words"]
    assert words[0]["t"] == "ZKDX-161085-2"
    assert words[0]["x"] == pytest.approx(50.0)
    assert words[0]["y"] == pytest.approx(400.0)  # (1 - 0.10 - 0.1) * 500
    assert words[0]["w"] == pytest.approx(500.0)
    assert words[0]["h"] == pytest.approx(50.0)


def test_a_line_box_shared_by_every_word_is_split_by_characters(vision):
    whole_line = lambda location, length: Rect(0.0, 0.9, 1.0, 0.1)  # noqa: E731 - the line's own box
    page = FakeImage(1000, 500, [_line("ab cdef", (0.0, 0.9, 1.0, 0.1), whole_line)])
    vision({PNG: page})
    first, second = screen_ocr.read_text_lines([PNG])[0][0]["words"]
    # "ab cdef" is 7 characters across a 1000 px line: "ab" is 2/7 of it, "cdef" the last 4/7 from 3/7.
    assert first["x"] == pytest.approx(0.0) and first["w"] == pytest.approx(1000 * 2 / 7)
    assert second["x"] == pytest.approx(1000 * 3 / 7) and second["w"] == pytest.approx(1000 * 4 / 7)


def test_empty_page_reads_as_no_lines_and_finds_nothing(vision):
    vision({PNG: FakeImage(10, 10, [])})
    assert screen_ocr.read_lines([PNG]) == [[]]
    assert screen_ocr.read_text_lines([PNG]) == [[]]
    assert screen_ocr.find_text("INV-1881", [PNG]) == {"found": False}


def test_failed_recognition_and_unreadable_image_both_read_as_no_lines(vision):
    vision({PNG: FakeImage(10, 10, [_line("INV-1881", (0.0, 0.0, 1.0, 1.0))], fail=True), JPEG: None})
    assert screen_ocr.read_lines([PNG, JPEG]) == [[], []]
    assert screen_ocr.read_text_lines([PNG, JPEG]) == [[], []]


def test_find_text_proves_a_value_read_from_vision_output(vision):
    vision({PNG: PAGE})
    assert screen_ocr.find_text("INV-1881", [PNG]) == {"found": True, "image_index": 0, "line": "INV-1881 form"}
    assert screen_ocr.find_text("INV-188", [PNG]) == {"found": False}


def test_a_timed_out_read_reads_as_no_text_and_is_not_cached(vision):
    backend = vision({PNG: PAGE})
    backend.block = threading.Event()
    assert screen_ocr.read_lines([PNG], timeout=0.05) == [[]]
    backend.block.set()
    assert screen_ocr.read_lines([PNG], timeout=5) == [["INV-1881 form", "customs release", "ZKDX-161085-2"]]


def test_warm_up_runs_one_blank_recognition_and_caches_nothing(vision):
    backend = vision({PNG: PAGE})
    seconds = screen_ocr.warm_up()
    assert isinstance(seconds, float) and seconds >= 0
    assert len(backend.performed) == 1 and backend.performed[0].startswith(b"\x89PNG")
    assert not screen_ocr._cache and not screen_ocr._words_cache


def test_warm_up_is_none_off_macos(monkeypatch):
    monkeypatch.setattr(screen_ocr, "_is_macos", lambda: False)
    assert screen_ocr.warm_up() is None


def test_without_pyobjc_vision_is_unavailable_and_reads_no_text(monkeypatch):
    monkeypatch.setattr(screen_ocr, "_is_macos", lambda: True)
    monkeypatch.setattr(screen_ocr, "_load_vision", lambda: None)
    assert screen_ocr.available() is False
    assert screen_ocr.read_lines([PNG]) == [[]]
    assert screen_ocr.warm_up() is None


def test_windows_path_still_runs_powershell_and_never_touches_vision(monkeypatch):
    monkeypatch.setattr(screen_ocr, "_is_macos", lambda: False)
    monkeypatch.setattr(screen_ocr, "_vision_invoke", lambda *a, **k: pytest.fail("Vision on Windows"))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='[["INV-1881"]]', stderr="")

    monkeypatch.setattr(screen_ocr.subprocess, "run", fake_run)
    assert screen_ocr._invoke_ocr(["a.png"], timeout=1) == [["INV-1881"]]
    assert calls and calls[0][0] == "powershell"


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.environ.get("LUMEN_MAC_LIVE"),
    reason="live Vision read: macOS only, set LUMEN_MAC_LIVE=1",
)
def test_live_vision_reads_rendered_text(tmp_path):
    AppKit = pytest.importorskip("AppKit")
    import Foundation

    width, height = 900, 200
    image = AppKit.NSImage.alloc().initWithSize_((width, height))
    image.lockFocus()
    AppKit.NSColor.whiteColor().set()
    AppKit.NSRectFill(((0, 0), (width, height)))
    attributes = {
        AppKit.NSFontAttributeName: AppKit.NSFont.monospacedSystemFontOfSize_weight_(40, 0.0),
        AppKit.NSForegroundColorAttributeName: AppKit.NSColor.blackColor(),
    }
    Foundation.NSString.stringWithString_("INV-1881 customs release").drawAtPoint_withAttributes_(
        (20, 120), attributes
    )
    image.unlockFocus()
    representation = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    png = bytes(representation.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None))

    assert screen_ocr.available()
    screen_ocr.warm_up()
    assert screen_ocr.find_text("INV-1881", [png])["found"] is True
    lines = screen_ocr.read_text_lines([png])[0]
    assert screen_ocr.locate_phrase("customs release", [line["words"] for line in lines], (0, 0))["found"] is True
