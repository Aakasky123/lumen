"""Text eyes for screenshots the run already captured: Windows' built-in OCR (Windows.Media.Ocr), or
Apple's Vision text recognition on macOS (VNRecognizeTextRequest, through pyobjc).

Native windows built with toolkits that expose no UI Automation text (Tk, custom canvases) show values
only as pixels. This reads the lines of text in captured screenshots so the runtime can prove that an
exact value was on screen (a proof, not an interpretation: the value must appear verbatim in a line).
Masked regions (password and model-marked controls) are painted over before capture, so they never
read back. Results are cached by image content; any failure reads as "no text" so callers fall back to
their existing checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

# Every desktop view a run keeps (each window's latest plus 64 earlier ones) stays read once.
_CACHE_LIMIT = 160
_PNG_MAGIC = b"\x89PNG"
_cache: OrderedDict[str, list[str]] = OrderedDict()
_cache_lock = threading.Lock()

# One PowerShell process reads every image passed to it and prints one JSON array of line lists.
_OCR_SCRIPT = r"""
param([string]$ListPath, [switch]$Words)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait() | Out-Null
    $task.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$results = @()
foreach ($path in (Get-Content -LiteralPath $ListPath)) {
    $lines = @()
    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        if ($Words) {
            # Each line as its text and its words with their boxes in image pixels.
            foreach ($line in $result.Lines) {
                $boxes = @()
                foreach ($word in $line.Words) {
                    $r = $word.BoundingRect
                    $boxes += ,([ordered]@{ t = $word.Text; x = $r.X; y = $r.Y; w = $r.Width; h = $r.Height })
                }
                $lines += ,([ordered]@{ text = $line.Text; words = @($boxes) })
            }
        } else {
            $lines = @($result.Lines | ForEach-Object { $_.Text })
        }
        $stream.Dispose()
    } catch {
        $lines = @()
    }
    # An object per image in word mode: nested arrays of one element would arrive unwrapped.
    if ($Words) { $results += ,([ordered]@{ lines = @($lines) }) } else { $results += ,$lines }
}
ConvertTo-Json -InputObject @($results) -Depth 6 -Compress
"""


def available() -> bool:
    if _is_macos():
        return _load_vision() is not None
    return platform.system() == "Windows"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _invoke_ocr(paths: list[str], *, timeout: float, words: bool = False) -> Any:
    if _is_macos():
        return _vision_invoke(paths, timeout=timeout, words=words)
    with tempfile.TemporaryDirectory(prefix="lumen-ocr-") as folder:
        script = Path(folder) / "ocr.ps1"
        script.write_text(_OCR_SCRIPT, encoding="utf-8")
        listing = Path(folder) / "images.txt"
        listing.write_text("\n".join(paths), encoding="utf-8")
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-ListPath", str(listing), *(["-Words"] if words else []),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    return json.loads(completed.stdout.strip().lstrip("﻿") or "[]", strict=False)


# macOS: Apple's Vision recognizes the same image files in-process. pyobjc is imported lazily, so
# nothing loads on Windows or in a process that never reads a screenshot.
@lru_cache(maxsize=1)
def _load_vision() -> tuple[Any, Any, Any, Any] | None:
    """(Vision, Quartz, Foundation, objc) when pyobjc's Vision bindings import; None otherwise."""
    try:
        import Foundation
        import objc
        import Quartz
        import Vision
    except Exception:  # noqa: BLE001 - no pyobjc means no OCR, which callers already handle
        return None
    return Vision, Quartz, Foundation, objc


def _in_worker(work: Callable[[], Any], timeout: float) -> Any:
    """Runs work on a daemon thread and waits at most timeout seconds. A run that overruns raises
    TimeoutError (its thread finishes in the background, its result discarded)."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = work()
        except BaseException as error:  # noqa: BLE001 - re-raised on the caller's thread below
            outcome["error"] = error

    worker = threading.Thread(target=run, name="lumen-vision-ocr", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"Vision OCR did not finish within {timeout} seconds")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _vision_invoke(paths: list[str], *, timeout: float, words: bool = False) -> Any:
    """The same JSON-shaped output PowerShell gives on Windows, computed from the image files."""
    modules = _load_vision()
    if modules is None:
        raise RuntimeError("Vision OCR is not available (pyobjc-framework-Vision is not installed)")

    def read_all() -> list[Any]:
        results = []
        for path in paths:
            with open(path, "rb") as handle:
                results.append(_vision_read(modules, handle.read(), words=words))
        return results

    return _in_worker(read_all, timeout)


def _vision_read(modules: tuple[Any, Any, Any, Any], data: bytes, *, words: bool) -> Any:
    """One image's lines: a list of line texts, or {"lines": [{"text", "words"}]} in word mode. Boxes
    are in image pixels with a top-left origin; an unreadable image reads as no lines."""
    Vision, Quartz, Foundation, objc = modules
    with objc.autorelease_pool():
        source = Quartz.CGImageSourceCreateWithData(Foundation.NSData.dataWithBytes_length_(data, len(data)), None)
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None) if source is not None else None
        lines: list[dict[str, Any]] = []
        if image is not None:
            width, height = int(Quartz.CGImageGetWidth(image)), int(Quartz.CGImageGetHeight(image))
            request = Vision.VNRecognizeTextRequest.alloc().init()
            # Accurate, and no language correction: a code or an invoice number must read as printed.
            request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
            request.setUsesLanguageCorrection_(False)
            if hasattr(request, "setAutomaticallyDetectsLanguage_"):
                request.setAutomaticallyDetectsLanguage_(True)
            # options=None: an empty dict is not the same as no options to this binding.
            handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
            performed, _error = handler.performRequests_error_([request], None)
            if performed:
                lines = _vision_lines(list(request.results() or []), width, height, Foundation)
    if words:
        return {"lines": lines}
    return [str(line["text"]) for line in lines]


def _vision_lines(observations: list[Any], width: int, height: int, Foundation: Any) -> list[dict[str, Any]]:
    """Each observation as {"text", "words": [{"t", "x", "y", "w", "h"}]} in reading order. A line
    with no word is dropped, as on Windows."""
    found: list[tuple[tuple[float, float, float, float], dict[str, Any]]] = []
    for observation in observations:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        text = str(candidate.string() or "")
        tokens = list(re.finditer(r"\S+", text))
        if not tokens:
            continue
        line_box = _pixel_box(observation.boundingBox(), width, height)
        words = []
        for token in tokens:
            location = _utf16_length(text[: token.start()])
            length = _utf16_length(token.group())
            word_observation, _error = candidate.boundingBoxForRange_error_(
                Foundation.NSMakeRange(location, length), None
            )
            box = _pixel_box(word_observation.boundingBox(), width, height) if word_observation is not None else None
            # Vision sometimes returns the whole line's box for every word; split it by characters.
            if box is None or (len(tokens) > 1 and _same_box(box, line_box)):
                box = _split_box(line_box, len(text), token.start(), token.end())
            x, y, w, h = box
            words.append({"t": token.group(), "x": x, "y": y, "w": w, "h": h})
        found.append((line_box, {"text": text, "words": words}))
    return [line for _, line in _reading_order(found)]


def _pixel_box(rect: Any, width: int, height: int) -> tuple[float, float, float, float]:
    """A Vision rectangle (normalized, bottom-left origin) as (x, y, w, h) in top-left-origin pixels."""
    x, y = float(rect.origin.x), float(rect.origin.y)
    w, h = float(rect.size.width), float(rect.size.height)
    return (x * width, (1.0 - y - h) * height, w * width, h * height)


def _split_box(
    line_box: tuple[float, float, float, float], length: int, start: int, end: int
) -> tuple[float, float, float, float]:
    """The part of a line's box that the characters [start, end) occupy, by character count."""
    x, y, w, h = line_box
    span = max(1, length)
    return (x + w * start / span, y, w * (end - start) / span, h)


def _same_box(first: tuple[float, ...], second: tuple[float, ...]) -> bool:
    return all(abs(a - b) < 0.5 for a, b in zip(first, second))


def _utf16_length(text: str) -> int:
    """Length in UTF-16 code units, which is how Foundation (and so Vision's ranges) counts a string."""
    return len(text.encode("utf-16-le")) // 2


def _reading_order(
    found: list[tuple[tuple[float, float, float, float], dict[str, Any]]]
) -> list[tuple[tuple[float, float, float, float], dict[str, Any]]]:
    """Top to bottom by line, and left to right within a line (lines whose tops are within half a line
    height share a band)."""
    ordered = sorted(found, key=lambda entry: entry[0][1])
    bands: list[list[tuple[tuple[float, float, float, float], dict[str, Any]]]] = []
    for entry in ordered:
        if bands and abs(entry[0][1] - bands[-1][0][0][1]) <= 0.5 * max(bands[-1][0][0][3], 1.0):
            bands[-1].append(entry)
        else:
            bands.append([entry])
    return [entry for band in bands for entry in sorted(band, key=lambda item: item[0][0])]


def _blank_png(width: int = 64, height: int = 64) -> bytes:
    """A small white PNG, for the warm-up (no file is needed)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"\xff" * (width * 3) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def warm_up(*, timeout: float = 120.0) -> float | None:
    """Runs one Vision recognition on a blank image, so the first real read does not pay Vision's
    one-time cost (about 20 s on the measured Mac; warm reads take about 0.1 s). Call it once at
    startup on a background thread. Returns the seconds it took, or None when Vision is not in use
    or the warm-up failed. It caches nothing."""
    modules = _load_vision() if _is_macos() else None
    if modules is None:
        return None
    started = time.monotonic()
    try:
        _in_worker(lambda: _vision_read(modules, _blank_png(), words=False), timeout)
    except Exception:  # noqa: BLE001 - a failed warm-up only means the first real read is slow
        return None
    return time.monotonic() - started


def _run_ocr(paths: list[str], *, timeout: float) -> list[list[str]]:
    parsed = _invoke_ocr(paths, timeout=timeout)
    if len(paths) == 1 and parsed and all(isinstance(item, str) for item in parsed):
        parsed = [parsed]  # PowerShell flattens a single nested array
    rows: list[list[str]] = []
    for item in parsed if isinstance(parsed, list) else []:
        if isinstance(item, list):
            rows.append([str(line) for line in item if isinstance(line, str)])
        elif isinstance(item, str):
            rows.append([item])
        else:
            rows.append([])
    return rows


def read_lines(images: list[bytes], *, timeout: float = 20.0) -> list[list[str]]:
    """Lines of text in each image (encoded JPEG/PNG bytes), in order; [] for an unreadable image."""
    if not images or not available():
        return [[] for _ in images]
    keys = [hashlib.sha256(image).hexdigest() for image in images]
    results: dict[str, list[str]] = {}
    missing: list[tuple[str, bytes]] = []
    with _cache_lock:
        for key, image in zip(keys, images):
            if key in _cache:
                _cache.move_to_end(key)
                results[key] = _cache[key]
            elif key not in {pending for pending, _ in missing}:
                missing.append((key, image))
    if missing:
        try:
            with tempfile.TemporaryDirectory(prefix="lumen-ocr-images-") as folder:
                paths = []
                for index, (_, image) in enumerate(missing):
                    suffix = "png" if image[:4] == _PNG_MAGIC else "jpg"
                    path = os.path.join(folder, f"image-{index}.{suffix}")
                    with open(path, "wb") as handle:
                        handle.write(image)
                    paths.append(path)
                rows = _run_ocr(paths, timeout=timeout)
        except Exception:  # noqa: BLE001 - no text is the safe answer; a failed read is not cached
            rows = None
        with _cache_lock:
            for index, (key, _) in enumerate(missing):
                lines = rows[index] if rows is not None and index < len(rows) else []
                results[key] = lines
                if rows is not None:
                    _cache[key] = lines
                    _cache.move_to_end(key)
            while len(_cache) > _CACHE_LIMIT:
                _cache.popitem(last=False)
    return [results.get(key, []) for key in keys]


def _squash(value: str) -> str:
    return " ".join(str(value or "").split())


# Windows OCR reads the slashed zero of monospace fonts (Consolas, the Windows default: Notepad,
# terminals, many apps' codes) as the letter Ø. Inside a token that holds a digit it is that zero; a
# word with no digit keeps its letter, and the letter O never becomes a zero (a misread stays caught).
_SLASHED_ZERO = re.compile(r"[\u00d8\u00f8]")
_DIGIT_TOKEN = re.compile(r"\S*\d\S*")


def _read_slashed_zeros(text: str) -> str:
    return _DIGIT_TOKEN.sub(lambda token: _SLASHED_ZERO.sub("0", token.group(0)), text)


# What else OCR has returned for a slashed zero (run 1663 read ZKDX-161085-2 as "161e85"). Never
# the letter O: a zero misread as O is exactly the mistake a proof must catch.
_ZERO_MISREADS = "0\u00d8\u00f8e\u03b8\u0398"
# What OCR has returned for a capital I in Segoe UI, a plain bar (v5_reconcile run 1717 read
# INV-1881 as "'NV-1881"). Never accepted alone either: a second reader must confirm.
_CAPITAL_I_MISREADS = "I'|l1!\u2019\u2018`"


def find_text(
    value: str, images: list[bytes], *, timeout: float = 20.0, zero_misreads: bool = False
) -> dict[str, object]:
    """Whether the exact value (whitespace collapsed, same letter case) appears as a whole token run
    in one OCR line of any image: no letter or digit may touch it on either side, so "2026" never
    matches inside "12026" and a code never matches inside a longer one. Returns
    {"found": True, "image_index": i, "line": text} or {"found": False}.

    zero_misreads: where the value has a 0, the line may show a glyph OCR returns for a slashed zero
    instead, and where it has a capital I, a glyph OCR returns for that plain bar (every other
    character still exact). Such a read is not proof alone; the result says "zero_misread" so the
    caller has the pixels confirmed another way."""
    if len(_squash(value)) < 3 or not images:
        return {"found": False}
    return find_in_lines(value, read_lines(images, timeout=timeout), zero_misreads=zero_misreads)


def find_in_lines(value: str, lines_per_image: list[list[str]], *, zero_misreads: bool = False) -> dict[str, object]:
    """find_text's matching over lines already read (by OCR, or transcribed by a model). Only the read
    lines' slashed zeros are folded: a value carrying Ø is not the zero a screen showed."""
    wanted = _squash(value)
    if len(wanted) < 3:
        return {"found": False}
    def piece(character: str) -> str:
        if zero_misreads and character == "0":
            return f"[{_ZERO_MISREADS}]"
        if zero_misreads and character == "I":
            return f"[{re.escape(_CAPITAL_I_MISREADS)}]"
        return re.escape(character)

    body = "".join(piece(character) for character in wanted)
    pattern = re.compile(r"(?<![0-9A-Za-z])" + body + r"(?![0-9A-Za-z])")
    for index, lines in enumerate(lines_per_image):
        for line in lines or []:
            read = _read_slashed_zeros(_squash(line))
            match = pattern.search(read)
            if match:
                return {
                    "found": True,
                    "image_index": index,
                    "line": read[:300],
                    **({"zero_misread": True} if match.group(0) != wanted else {}),
                }
    return {"found": False}


_words_cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()


def _word_row(raw: Any) -> list[dict[str, Any]]:
    row: list[dict[str, Any]] = []
    for word in [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []:
        if not isinstance(word, dict) or not str(word.get("t") or ""):
            continue
        try:
            row.append(
                {
                    "t": str(word["t"]),
                    "x": float(word.get("x") or 0),
                    "y": float(word.get("y") or 0),
                    "w": float(word.get("w") or 0),
                    "h": float(word.get("h") or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    return row


def _text_lines(raw: Any) -> list[dict[str, Any]]:
    """One image's word-mode reading as [{"text": line, "words": [...]}]; a line with no word is dropped."""
    lines = raw.get("lines") if isinstance(raw, dict) else None
    rows: list[dict[str, Any]] = []
    for line in [lines] if isinstance(lines, dict) else lines if isinstance(lines, list) else []:
        if not isinstance(line, dict):
            continue
        words = _word_row(line.get("words"))
        if words:
            rows.append({"text": str(line.get("text") or " ".join(word["t"] for word in words)), "words": words})
    return rows


def read_text_lines(images: list[bytes], *, timeout: float = 20.0) -> list[list[dict[str, Any]]]:
    """The lines of each image (encoded PNG/JPEG bytes) with their words' boxes in image pixels:
    [[{"text": line, "words": [{"t", "x", "y", "w", "h"}, ...]}, ...], ...]; [] for an unreadable image.
    One OCR process reads every image; readings are cached by image content, and their line texts
    also answer read_lines for the same image."""
    if not images or not available():
        return [[] for _ in images]
    keys = [hashlib.sha256(image).hexdigest() for image in images]
    results: dict[str, list[dict[str, Any]]] = {}
    missing: list[tuple[str, bytes]] = []
    with _cache_lock:
        for key, image in zip(keys, images):
            if key in _words_cache:
                _words_cache.move_to_end(key)
                results[key] = _words_cache[key]
            elif key not in {pending for pending, _ in missing}:
                missing.append((key, image))
    if missing:
        rows: list[list[dict[str, Any]]] | None
        try:
            with tempfile.TemporaryDirectory(prefix="lumen-ocr-words-") as folder:
                paths = []
                for index, (_, image) in enumerate(missing):
                    path = os.path.join(folder, f"image-{index}." + ("png" if image[:4] == _PNG_MAGIC else "jpg"))
                    with open(path, "wb") as handle:
                        handle.write(image)
                    paths.append(path)
                parsed = _invoke_ocr(paths, timeout=timeout, words=True)
            # Each image is an object, so only the outer array of one image may arrive unwrapped.
            parsed = [parsed] if isinstance(parsed, dict) else parsed if isinstance(parsed, list) else []
            rows = [_text_lines(item) for item in parsed]
        except Exception:  # noqa: BLE001 - no text is the safe answer; a failed read is not cached
            rows = None
        with _cache_lock:
            for index, (key, _) in enumerate(missing):
                lines = rows[index] if rows is not None and index < len(rows) else []
                results[key] = lines
                if rows is not None:
                    _words_cache[key] = lines
                    _words_cache.move_to_end(key)
                    _cache[key] = [line["text"] for line in lines]
                    _cache.move_to_end(key)
            for cache in (_words_cache, _cache):
                while len(cache) > _CACHE_LIMIT:
                    cache.popitem(last=False)
    return [results.get(key, []) for key in keys]


def read_words(image: bytes, *, timeout: float = 20.0) -> list[list[dict[str, Any]]]:
    """The lines of one image (encoded PNG/JPEG bytes), each as its words with their boxes in image
    pixels: [[{"t": text, "x", "y", "w", "h"}, ...], ...]; [] when the image cannot be read."""
    if not image:
        return []
    return [line["words"] for line in (read_text_lines([image], timeout=timeout) or [[]])[0]]


def locate_phrase(text: str, lines: list[list[dict[str, Any]]], near: tuple[float, float]) -> dict[str, Any]:
    """Where to press and release to drag-select exactly `text` among OCR'd words: the occurrence
    nearest `near`, which may wrap onto the next line. Words are compared ignoring letter case; the
    phrase's first word may follow punctuation inside the OCR word and its last word may precede it
    ("(customs" / "form,"). The press lands just before the first character and the release just
    after the last, so a text control's nearest character gap is the phrase's own edge; inside a word
    the edge is placed by the word's average character width.

    With no exact reading, a word one letter off counts ("approximate": True).

    Returns {"found": True, "from": (x, y), "to": (x, y), "box": (left, top, right, bottom),
    "occurrences": n, "distance": d} or {"found": False, "occurrences": 0}."""
    tokens = str(text or "").split()
    if not tokens:
        return {"found": False, "occurrences": 0}
    flat = [word for line in lines for word in line]
    wanted = [token.casefold() for token in tokens]
    matches: list[dict[str, Any]] = []
    for start in range(len(flat) - len(wanted) + 1):
        span = flat[start : start + len(wanted)]
        read = [str(word["t"]).casefold() for word in span]
        if len(wanted) == 1:
            offset = read[0].find(wanted[0])
            if offset < 0:
                continue
            first_offset, last_end = offset, offset + len(wanted[0])
        else:
            if not read[0].endswith(wanted[0]) or not read[-1].startswith(wanted[-1]):
                continue
            if read[1:-1] != wanted[1:-1]:
                continue
            first_offset, last_end = len(read[0]) - len(wanted[0]), len(wanted[-1])
        # Neither edge may cut through a letter or digit: "form" is not inside "formal".
        first_text, last_text = str(span[0]["t"]), str(span[-1]["t"])
        if first_offset > 0 and first_text[first_offset - 1].isalnum():
            continue
        if last_end < len(last_text) and last_text[last_end].isalnum():
            continue
        matches.append({"span": span, "first_offset": first_offset, "last_end": last_end})
    approximate = False
    if not matches:
        # OCR misreads a letter at a highlight's edge ("release" half-selected read "releas"): with no
        # exact reading, a word of 5+ letters one edit away still places the drag (marked approximate;
        # the brain checks the highlight). Punctuation around a word is set aside first.
        for start in range(len(flat) - len(wanted) + 1):
            span = flat[start : start + len(wanted)]
            cores = [_word_core(str(word["t"])) for word in span]
            if not all(_close_word(core.casefold(), token) for (_, core, _), token in zip(cores, wanted)):
                continue
            first_lead, _, _ = cores[0]
            last_lead, last_core, _ = cores[-1]
            matches.append({"span": span, "first_offset": first_lead, "last_end": last_lead + len(last_core)})
        approximate = bool(matches)
    if not matches:
        return {"found": False, "occurrences": 0}

    def edge(word: dict[str, Any], chars: int, *, leading: bool) -> float:
        width = max(1.0, float(word["w"]))
        length = max(1, len(str(word["t"])))
        char_width = width / length
        if leading and chars == 0:
            return float(word["x"]) - max(1.0, 0.15 * char_width)
        if not leading and chars >= length:
            return float(word["x"]) + width + max(1.0, 0.15 * char_width)
        return float(word["x"]) + chars * char_width

    best: dict[str, Any] | None = None
    for match in matches:
        span = match["span"]
        left = min(float(word["x"]) for word in span)
        top = min(float(word["y"]) for word in span)
        right = max(float(word["x"]) + float(word["w"]) for word in span)
        bottom = max(float(word["y"]) + float(word["h"]) for word in span)
        nearest_x = min(max(near[0], left), right)
        nearest_y = min(max(near[1], top), bottom)
        distance = ((near[0] - nearest_x) ** 2 + (near[1] - nearest_y) ** 2) ** 0.5
        if best is None or distance < best["distance"]:
            first, last = span[0], span[-1]
            best = {
                "found": True,
                "from": (
                    round(edge(first, match["first_offset"], leading=True)),
                    round(float(first["y"]) + float(first["h"]) / 2),
                ),
                "to": (
                    round(edge(last, match["last_end"], leading=False)),
                    round(float(last["y"]) + float(last["h"]) / 2),
                ),
                "box": (round(left), round(top), round(right), round(bottom)),
                "distance": round(distance, 1),
            }
    assert best is not None
    best["occurrences"] = len(matches)
    if approximate:
        best["approximate"] = True
    return best


def _word_core(word: str) -> tuple[int, str, int]:
    """A read word split as (leading punctuation length, the word, trailing punctuation length)."""
    start, end = 0, len(word)
    while start < end and not word[start].isalnum():
        start += 1
    while end > start and not word[end - 1].isalnum():
        end -= 1
    return start, word[start:end], len(word) - end


def _close_word(read: str, wanted: str) -> bool:
    """Equal, or one insertion, deletion, or substitution apart when the wanted word has 5+ letters."""
    if read == wanted:
        return True
    if len(wanted) < 5 or abs(len(read) - len(wanted)) > 1:
        return False
    if len(read) == len(wanted):
        return sum(a != b for a, b in zip(read, wanted)) == 1
    shorter, longer = (read, wanted) if len(read) < len(wanted) else (wanted, read)
    return any(longer[:index] + longer[index + 1 :] == shorter for index in range(len(longer)))
