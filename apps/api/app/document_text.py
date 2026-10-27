"""Read-only text extraction from a downloaded PDF or DOCX, run in a child process.

The file is hostile input. The child process has a wall-clock timeout and is killed on expiry; the file,
the page count, the zip's declared and real decompressed sizes, and the extracted text are all capped; any
parser exception becomes a clean refusal. Nothing is executed or fetched: a PDF is read as text only (no
JavaScript, no launch actions, no embedded files), a DOCX is a zip whose word/document.xml is parsed with
the standard library after a DOCTYPE/ENTITY refusal, and macros (vbaProject.bin) are never opened.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_DOCX_XML_BYTES = 8 * 1024 * 1024
MAX_DOCX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_DOCX_RATIO = 1000
MAX_DOCX_ENTRIES = 1000
EXTRACT_TIMEOUT_SECONDS = 15.0
_RAW_TEXT_CAP = 200_000
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentRefused(Exception):
    pass


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        raise DocumentRefused("encrypted")
    parts: list[str] = []
    total = 0
    for index, page in enumerate(reader.pages):
        if index >= MAX_PDF_PAGES or total >= _RAW_TEXT_CAP:
            break
        text = page.extract_text() or ""
        parts.append(text)
        total += len(text)
    return "\n\n".join(parts)


def _docx_text(path: Path) -> str:
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_DOCX_ENTRIES:
            raise DocumentRefused("too_many_entries")
        if sum(item.file_size for item in infos) > MAX_DOCX_TOTAL_BYTES:
            raise DocumentRefused("too_large_uncompressed")
        try:
            info = archive.getinfo("word/document.xml")
        except KeyError as exc:
            raise DocumentRefused("not_a_docx") from exc
        if info.file_size > MAX_DOCX_XML_BYTES or info.file_size > max(1, info.compress_size) * MAX_DOCX_RATIO:
            raise DocumentRefused("too_large_uncompressed")
        # Read with a hard bound that does not trust the declared size.
        with archive.open(info) as stream:
            raw = stream.read(MAX_DOCX_XML_BYTES + 1)
    if len(raw) > MAX_DOCX_XML_BYTES:
        raise DocumentRefused("too_large_uncompressed")
    # Only plain UTF-8/ASCII XML: a UTF-16/32 file (BOM or declaration) would be decoded by the parser
    # while the DOCTYPE check below only sees ASCII.
    body = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
    if b"\x00" in raw or not body.startswith(b"<"):
        raise DocumentRefused("encoding_not_allowed")
    declared = re.match(rb"<\?xml[^>]*?encoding\s*=\s*[\"']([^\"']+)[\"']", body)
    if declared and declared.group(1).strip().lower() not in (b"utf-8", b"utf8"):
        raise DocumentRefused("encoding_not_allowed")
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", raw, re.IGNORECASE):
        raise DocumentRefused("dtd_not_allowed")
    root = ET.fromstring(raw)
    lines: list[str] = []
    size = 0
    for paragraph in root.iter(f"{_W}p"):
        pieces: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_W}t":
                pieces.append(node.text or "")
            elif node.tag == f"{_W}tab":
                pieces.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                pieces.append("\n")
        line = "".join(pieces)
        lines.append(line)
        size += len(line)
        if size >= _RAW_TEXT_CAP:
            break
    return "\n".join(lines)


def _extract_here(path: Path, extension: str) -> str:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DocumentRefused("too_large")
    if extension == "pdf":
        return _pdf_text(path)
    if extension == "docx":
        return _docx_text(path)
    raise DocumentRefused("type")


def extract_text(path: str, extension: str, limit: int) -> dict[str, Any] | None:
    """{text, truncated} for a PDF/DOCX at `path`, or None when it cannot be read within the caps."""
    try:
        done = subprocess.run(
            [sys.executable, "-m", "app.document_text", str(path), str(extension)],
            capture_output=True,
            timeout=EXTRACT_TIMEOUT_SECONDS,
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.DEVNULL,
        )
        if done.returncode != 0:
            return None
        text = str(json.loads(done.stdout.decode("utf-8", errors="replace")).get("text") or "")
    except Exception:  # noqa: BLE001 - timeout, crash, bad output: all one clean refusal
        return None
    text = text.replace("\x00", "").strip()
    if not text:
        return None
    return {"text": text[: int(limit)], "truncated": len(text) > int(limit)}


if __name__ == "__main__":  # child process
    try:
        out = _extract_here(Path(sys.argv[1]), sys.argv[2])
        sys.stdout.write(json.dumps({"text": out[:_RAW_TEXT_CAP]}))
    except BaseException:  # noqa: BLE001
        sys.exit(2)
