"""Headless helpers for the Mac bench's harder tasks: an RTF reader that knows which characters are bold, a
tiny PDF writer for fine print, a loopback page server, and read-only probes of system facts and settings.

None of these drives an app or the screen. The graders use them to read an end state the agent left on disk
(or a loopback server saw), and validate_golden_path.py uses them to prove the graders without the agent.
"""

from __future__ import annotations

import hashlib
import http.server
import platform
import re
import subprocess
import sys
import threading
import urllib.parse
from typing import Any

# --- RTF: the text and which characters are bold ---------------------------------------------------

_SKIP_DESTINATIONS = {
    "fonttbl", "colortbl", "expandedcolortbl", "stylesheet", "info", "listtable", "listoverridetable", "pict",
    "header", "footer", "headerl", "headerr", "footerl", "footerr", "generator", "xmlnstbl", "themedata",
    "colorschememapping", "latentstyles", "datastore", "rsidtbl", "mmathPr", "fldinst", "object", "nonshppict",
}
_CONTROL_WORD = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?")
_FONT_ENTRY = re.compile(r"\\f(\d+)(?:\\[a-zA-Z]+-?\d* ?)*\s*([^;{}\\]+);")
_BOLD_FACE = re.compile(r"(bold|black|heavy|semibold|demibold|extrabold|ultrabold)", re.I)


def _font_table(rtf: str) -> dict[int, str]:
    start = rtf.find("{\\fonttbl")
    if start < 0:
        return {}
    depth, end = 0, start
    for end in range(start, len(rtf)):
        if rtf[end] == "{":
            depth += 1
        elif rtf[end] == "}":
            depth -= 1
            if depth == 0:
                break
    return {int(num): name.strip() for num, name in _FONT_ENTRY.findall(rtf[start:end])}


def rtf_chars(rtf: str) -> list[tuple[str, bool]]:
    """Every visible character of an RTF document with whether it renders bold: a ``\\b`` run or a bold face
    from the font table (Cocoa writes both; a face alone, e.g. ``Helvetica-Bold``, still counts)."""
    fonts = _font_table(rtf)
    out: list[tuple[str, bool]] = []
    stack: list[tuple[bool, int, bool, int]] = []
    bold, font, skip, uc = False, -1, False, 1
    pending = 0  # fallback characters still to drop after a \uN
    i, n = 0, len(rtf)

    def emit(ch: str) -> None:
        nonlocal pending
        if pending:
            pending -= 1
            return
        if not skip:
            out.append((ch, bold or bool(_BOLD_FACE.search(fonts.get(font, "")))))

    while i < n:
        c = rtf[i]
        if c == "{":
            stack.append((bold, font, skip, uc))
            i += 1
        elif c == "}":
            if stack:
                bold, font, skip, uc = stack.pop()
            i += 1
        elif c == "\\":
            nxt = rtf[i + 1] if i + 1 < n else ""
            if nxt in ("\\", "{", "}"):
                emit(nxt)
                i += 2
            elif nxt in ("\n", "\r"):  # Cocoa's paragraph break: a backslash at the end of the line
                emit("\n")
                i += 2
            elif nxt == "'":
                try:
                    emit(bytes([int(rtf[i + 2:i + 4], 16)]).decode("cp1252"))
                except (ValueError, UnicodeDecodeError):
                    emit("?")
                i += 4
            elif nxt == "*":
                skip = True
                i += 2
            elif nxt == "~":
                emit("\u00a0")
                i += 2
            elif nxt in ("-", "_", "|", ":"):
                i += 2
            else:
                m = _CONTROL_WORD.match(rtf, i)
                if not m:
                    i += 2
                    continue
                word, arg = m.group(1), m.group(2)
                i = m.end()
                if word in _SKIP_DESTINATIONS:
                    skip = True
                elif word == "b":
                    bold = arg is None or arg != "0"
                elif word == "plain":
                    bold = False
                elif word == "f" and arg is not None:
                    font = int(arg)
                elif word in ("par", "line"):
                    emit("\n")
                elif word == "tab":
                    emit("\t")
                elif word == "uc" and arg is not None:
                    uc = int(arg)
                elif word == "u" and arg is not None:
                    code = int(arg)
                    emit(chr(code + 65536 if code < 0 else code))
                    pending = uc
        elif c in ("\r", "\n"):
            i += 1
        else:
            emit(c)
            i += 1
    return out


def rtf_plain_text(rtf: str) -> str:
    return "".join(ch for ch, _ in rtf_chars(rtf))


def textutil_rtf(source: str, fmt: str) -> str | None:
    """Convert html or txt to RTF with macOS's own Cocoa writer (``textutil``, no window): the same
    NSAttributedString RTF that TextEdit saves. None off macOS or on failure."""
    if sys.platform != "darwin":
        return None
    try:
        done = subprocess.run(
            ["/usr/bin/textutil", "-stdin", "-stdout", "-format", fmt, "-convert", "rtf"],
            input=source.encode("utf-8"), capture_output=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = done.stdout.decode("utf-8", errors="replace")
    return text if done.returncode == 0 and text.startswith("{\\rtf") else None


def html_paragraphs(lines: list[str], bold_first: bool = False) -> str:
    def esc(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    body = []
    for index, line in enumerate(lines):
        inner = f"<b>{esc(line)}</b>" if bold_first and index == 0 else esc(line)
        body.append(f"<p style='margin:0'>{inner}</p>")
    return (
        "<html><head><meta charset='utf-8'></head><body style='font-family: Helvetica; font-size: 13px'>"
        + "".join(body) + "</body></html>"
    )


def simple_rtf(lines: list[str], bold_first: bool = False) -> str:
    """A hand-written RTF (used when textutil is unavailable)."""
    def esc(text: str) -> str:
        out = []
        for ch in text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}"):
            out.append(ch if ord(ch) < 128 else f"\\u{ord(ch) if ord(ch) < 32768 else ord(ch) - 65536}?")
        return "".join(out)

    body = []
    for index, line in enumerate(lines):
        body.append(("\\b " + esc(line) + "\\b0" if bold_first and index == 0 else esc(line)) + "\\par\n")
    return "{\\rtf1\\ansi{\\fonttbl\\f0\\fswiss Helvetica;}\\f0\\fs26\n" + "".join(body) + "}"


def make_rtf(lines: list[str], bold_first: bool = False) -> str:
    return textutil_rtf(html_paragraphs(lines, bold_first), "html") or simple_rtf(lines, bold_first)


# --- PDF: one page of Helvetica text at given sizes and greys --------------------------------------

def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(items: list[tuple[float, float, float, float, str]], *, width: int = 612, height: int = 792) -> bytes:
    """A valid one-page PDF. ``items`` are ``(x, y, size_pt, grey 0..1, text)`` (latin-1 text), y from the bottom."""
    ops = []
    for x, y, size, grey, text in items:
        ops.append(f"BT /F1 {size:g} Tf {grey:g} g {x:g} {y:g} Td ({_pdf_escape(text)}) Tj ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << /Font << /F1 4 0 R >> >> "
        "/Contents 5 0 R >>".encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    return bytes(out)


def pdf_text(path: str) -> str | None:
    """The text PDFKit extracts from a PDF (no window). None off macOS or without pyobjc."""
    if sys.platform != "darwin":
        return None
    try:
        from Foundation import NSURL
        from Quartz import PDFDocument
    except ImportError:
        return None
    doc = PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(path))
    return None if doc is None else str(doc.string() or "")


# --- a loopback page that records who loaded it ----------------------------------------------------

class PageServer:
    """Serves one HTML page at ``/page`` on 127.0.0.1 and records each GET's User-Agent."""

    def __init__(self, html: str) -> None:
        self.html = html
        self.visits: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                if urllib.parse.urlparse(self.path).path != "/page":
                    body, status = b"not found", 404
                else:
                    outer.visits.append({"ua": self.headers.get("User-Agent", "")})
                    body, status = outer.html.encode("utf-8"), 200
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    @property
    def url(self) -> str:
        return self.base + "/page"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# --- read-only system facts and settings sentinels -------------------------------------------------

def _run(args: list[str]) -> str | None:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def macos_version() -> str:
    return (_run(["/usr/bin/sw_vers", "-productVersion"]) if sys.platform == "darwin" else None) or platform.mac_ver()[0] or ""


def chip_name() -> str:
    return (_run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]) if sys.platform == "darwin" else None) or ""


# Settings a careless agent in System Settings could flip. Read with `defaults read` / `scutil --get` only; the
# values are kept as hashes so the bench never stores the machine's name.
SETTINGS_SENTINELS: dict[str, list[str]] = {
    "computer_name": ["/usr/sbin/scutil", "--get", "ComputerName"],
    "local_host_name": ["/usr/sbin/scutil", "--get", "LocalHostName"],
    "appearance": ["/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle"],
    "accent_color": ["/usr/bin/defaults", "read", "-g", "AppleAccentColor"],
    "natural_scrolling": ["/usr/bin/defaults", "read", "-g", "com.apple.swipescrolldirection"],
    "dock_autohide": ["/usr/bin/defaults", "read", "com.apple.dock", "autohide"],
    "dock_tile_size": ["/usr/bin/defaults", "read", "com.apple.dock", "tilesize"],
}


def settings_snapshot() -> dict[str, str]:
    if sys.platform != "darwin":
        return {}
    return {name: hashlib.sha256(str(_run(args)).encode()).hexdigest()[:16] for name, args in SETTINGS_SENTINELS.items()}


def close_confirms_changes() -> bool | None:
    """System Settings > Desktop & Dock > "Ask to keep changes when closing documents"; None when unset or off macOS."""
    if sys.platform != "darwin":
        return None
    value = _run(["/usr/bin/defaults", "read", "-g", "NSCloseAlwaysConfirmsChanges"])
    return None if value is None else value == "1"
