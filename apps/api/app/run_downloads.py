"""Files the browser downloaded during a run, read from the user's downloads folder.

A run snapshots the folder when it starts; every step then lists the files that appeared since,
newest first, so the brain knows what the run downloaded (name, size, extension, full path, and
whether the browser is still writing it). Only top-level files count: the extension stages its own
uploads in a subfolder, and a download lands in the folder itself.

The snapshot stays small however full the folder is: it keeps the start time plus the names of the
few files created just before it, and a file is new when its name is not one of those and it was
CREATED after the run started (creation time on Windows, change time on Linux): a file that was already
there and was only edited during the run is not a download of the run. On Windows a completed file must
also carry the Mark of the Web (its Zone.Identifier stream) that browsers write on what they download, so a
document the user saved into the folder themselves is never listed as the run's.

The brain may open a listed, completed document in its default app (``open_file``) and attach a
listed, completed file to a page (``upload`` with the file's path). Opening is limited to document
types: nothing that can execute is ever opened.
"""

from __future__ import annotations

import collections
import ctypes
import functools
import os
import sys
import time
from pathlib import Path
from typing import Any

# Suffixes a browser writes while a download is still arriving (Chrome/Edge .crdownload, Firefox
# .part beside an empty placeholder, Safari .download, generic .tmp/.partial).
IN_PROGRESS_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".tmp")
# Document types open_file may open in their default app. Archives and anything that can run
# (executables, installers, scripts, shortcuts) are never opened.
OPENABLE_EXTENSIONS = frozenset(
    {
        # No html/htm/xml (they open in the user's real browser profile with scripts running, outside the
        # run's tab) and no csv (a spreadsheet app evaluates its formulas): the brain reads those in its tab.
        "pdf", "txt", "json", "md",
        "png", "jpg", "jpeg", "gif", "webp",
        "docx", "xlsx", "pptx", "odt", "ods",
    }
)
LIST_LIMIT = 10
READ_BYTES_LIMIT = 20 * 1024
# Text types read_download may return (the brain reads them as text, never executes them).
READABLE_EXTENSIONS = frozenset({"txt", "csv", "json", "md", "xml", "log", "tsv", "pdf", "docx"})
# Of those, the ones whose text is extracted by a capped, timed child process rather than decoded.
DOCUMENT_EXTENSIONS = frozenset({"pdf", "docx"})
# Files touched this long before the snapshot are remembered by name, so a write still landing as
# the run starts is not mistaken for a download of this run.
_RECENT_MARGIN_SECONDS = 300.0
# A file this young without the browser's mark may be a download whose mark is not written yet.
_MARK_GRACE_SECONDS = 5.0


# Which run did what, so a file the shared downloads folder gained while two runs were active is listed
# only for the run whose action came right before it appeared. Per run: the wall-clock start times of its
# recent actions, compared with the file's creation time.
_ACTIONS: dict[int, "collections.deque[float]"] = {}
_ACTIONS_MAX = 200


def note_action(run_id: int | None, at: float | None = None) -> None:
    """Record that `run_id` is about to act on the browser or screen (called when an action is dispatched)."""
    if not run_id:
        return
    _ACTIONS.setdefault(int(run_id), collections.deque(maxlen=_ACTIONS_MAX)).append(
        float(at if at is not None else time.time())
    )


def forget_run(run_id: int | None) -> None:
    _ACTIONS.pop(int(run_id or 0), None)


def _file_belongs_to_run(run_id: int, created_at: float, started_at: float) -> bool:
    """Whether the file created at `created_at` is this run's: its latest action at or before that moment is
    later than every other run's. A file no run's action precedes is this run's only when no other run has
    acted since this one started (so a lone run, or one resumed after a restart, still sees its downloads)."""
    best_run = 0
    best_at = -1.0
    for other, times in _ACTIONS.items():
        # Only actions inside this run's window count: an older run's history says nothing about this file.
        latest = max((t for t in times if started_at <= t <= created_at + 1.0), default=-1.0)
        if latest > best_at:
            best_run, best_at = other, latest
    if best_at >= 0.0:
        return best_run == int(run_id)
    return not any(
        other != int(run_id) and any(t >= started_at for t in times) for other, times in _ACTIONS.items()
    )


def downloads_folder() -> Path:
    """The user's downloads folder: LUMEN_DOWNLOADS_DIR when set, else the Windows known folder
    (which the user may have moved), else ~/Downloads."""
    override = str(os.environ.get("LUMEN_DOWNLOADS_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        known = _windows_known_downloads_folder()
        if known:
            return Path(known)
    return Path.home() / "Downloads"


def _windows_known_downloads_folder() -> str:
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        raw = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}").bytes_le  # FOLDERID_Downloads
        guid = _GUID.from_buffer_copy(raw)
        path_ptr = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
        try:
            return str(path_ptr.value or "") if result == 0 else ""
        finally:
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
    except Exception:  # noqa: BLE001 - the home-folder fallback still names a downloads folder
        return ""


def scan_folder(folder: Path | str | None = None) -> dict[str, Any]:
    """Raw stat of the folder's top-level files: {folder, now, files: [{name, path, size, mtime, ctime}]}."""
    root = Path(folder) if folder else downloads_folder()
    files: list[dict[str, Any]] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if entry.name.startswith(".") or entry.name.casefold() == "desktop.ini":
                        continue
                    if not entry.is_file(follow_symlinks=False) and not entry.is_symlink():
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                files.append(
                    {
                        "name": entry.name,
                        "path": os.path.join(str(root), entry.name),
                        "size": int(stat.st_size),
                        "mtime": float(stat.st_mtime),
                        "ctime": float(getattr(stat, "st_birthtime", stat.st_ctime)),
                    }
                )
    except OSError:
        pass
    return {"folder": str(root), "now": time.time(), "files": files}


def make_snapshot(scan: dict[str, Any]) -> dict[str, Any]:
    """What a run keeps of the folder at its start (small whatever the folder holds)."""
    started_at = float(scan.get("now") or time.time())
    recent = sorted(
        str(item.get("name"))
        for item in scan.get("files") or []
        if isinstance(item, dict) and _touched_at(item) >= started_at - _RECENT_MARGIN_SECONDS
    )
    return {"folder": str(scan.get("folder") or ""), "started_at": started_at, "recent_names": recent}


def _touched_at(item: dict[str, Any]) -> float:
    """When the file was created (Windows) or last changed (Linux): never its write time, which an edit of
    a file the user already had would move past the run's start."""
    try:
        return float(item.get("ctime") or 0)
    except (TypeError, ValueError):
        return 0.0


_MAC_QUARANTINE_ATTRIBUTE = b"com.apple.quarantine"


@functools.lru_cache(maxsize=1)
def _mac_getxattr() -> Any:
    """libc getxattr(path, name, value, size, position, options) as macOS declares it."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    getxattr = libc.getxattr
    getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    getxattr.restype = ctypes.c_ssize_t
    return getxattr


def _mac_has_quarantine(path: str) -> bool:
    """Whether macOS quarantined the file on download (the com.apple.quarantine extended attribute, which
    browsers such as Chrome and Safari set on what they save). A size query with no buffer answers without
    reading the value; a missing attribute or file is a negative return, which reads as not quarantined."""
    try:
        size = _mac_getxattr()(os.fsencode(path), _MAC_QUARANTINE_ATTRIBUTE, None, 0, 0, 0)
    except (OSError, ValueError, TypeError):
        return False
    return size >= 0


def has_mark_of_the_web(path: str) -> bool:
    """Whether a browser marked the file as downloaded: the Windows Zone.Identifier stream, or the macOS
    quarantine attribute. True on other systems (the OSWorld VM has no such mark)."""
    if sys.platform == "darwin":
        return _mac_has_quarantine(path)
    if sys.platform != "win32":
        return True
    try:
        with open(f"{path}:Zone.Identifier", "rb") as stream:
            return b"ZoneId" in stream.read(4096)
    except OSError:
        return False


def file_extension(name: str) -> str:
    return Path(str(name or "")).suffix.lstrip(".").casefold()


def is_in_progress_name(name: str) -> bool:
    return str(name or "").casefold().endswith(IN_PROGRESS_SUFFIXES)


def list_downloads(
    snapshot: dict[str, Any],
    scan: dict[str, Any],
    *,
    limit: int = LIST_LIMIT,
    require_mark_of_the_web: bool = True,
) -> list[dict[str, Any]]:
    """The files that appeared in the folder since the snapshot, newest first, at most `limit`:
    {name, size, extension, path, state} where state is complete or in_progress."""
    if not isinstance(snapshot, dict) or not isinstance(scan, dict):
        return []
    try:
        started_at = float(snapshot.get("started_at") or 0)
    except (TypeError, ValueError):
        return []
    if not started_at:
        return []
    known = {str(name) for name in snapshot.get("recent_names") or []}
    files = [item for item in scan.get("files") or [] if isinstance(item, dict) and item.get("name")]
    present = {str(item["name"]).casefold() for item in files}
    fresh = [
        item
        for item in files
        if str(item["name"]) not in known and _touched_at(item) >= started_at - 1.0
    ]
    run_id = int(snapshot.get("run_id") or 0)
    if run_id:
        # The folder is shared by every run on this computer: list only what this run's own action produced.
        fresh = [item for item in fresh if _file_belongs_to_run(run_id, _touched_at(item), started_at)]
    fresh.sort(key=_touched_at, reverse=True)
    listing: list[dict[str, Any]] = []
    for item in fresh[: max(0, int(limit))]:
        name = str(item["name"])
        # Firefox writes into name.part beside an empty placeholder named name.
        arriving = is_in_progress_name(name) or any(
            f"{name}{suffix}".casefold() in present for suffix in IN_PROGRESS_SUFFIXES
        )
        if require_mark_of_the_web and not arriving and not has_mark_of_the_web(str(item.get("path") or "")):
            if time.time() - _touched_at(item) > _MARK_GRACE_SECONDS:
                # Created during the run, but no browser downloaded it (the user saved it there).
                continue
            # A browser writes the mark just after the file lands: listed as still arriving, never readable yet.
            arriving = True
        listing.append(
            {
                "name": name,
                "size": int(item.get("size") or 0),
                "extension": file_extension(name),
                "path": str(item.get("path") or ""),
                "state": "in_progress" if arriving else "complete",
            }
        )
    return listing


def find_listed(listing: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    """The listed download whose path is exactly `path` (case-insensitive on Windows), else None."""
    wanted = _path_key(path)
    if not wanted:
        return None
    for item in listing or []:
        if isinstance(item, dict) and _path_key(item.get("path")) == wanted:
            return item
    return None


LATEST_TOKEN = "latest"


def find_read_target(listing: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    """The download of this run's own `listing` that `target` names, as the packet shows it: its exact path
    (either slash style), its listed name, or "latest" (the newest listed). Never matches anything outside
    the listing, so no other path, other run's file, or traversal can be named."""
    text = str(target or "").strip().strip('"')
    if not text:
        return None
    entries = [item for item in listing or [] if isinstance(item, dict)]
    found = find_listed(entries, text)
    if found is not None:
        return found
    if text.casefold() == LATEST_TOKEN:
        return entries[0] if entries else None
    if "/" in text or "\\" in text:
        return None
    folded = text.casefold()
    return next((item for item in entries if str(item.get("name") or "").casefold() == folded), None)


def _path_key(path: Any) -> str:
    text = str(path or "").strip().strip('"')
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text))


def open_refusal(entry: dict[str, Any] | None) -> str:
    """Why a listed download may not be opened ("" when it may)."""
    if not isinstance(entry, dict):
        return "open_file_not_downloaded"
    if entry.get("state") != "complete":
        return "open_file_download_incomplete"
    if str(entry.get("extension") or "") not in OPENABLE_EXTENSIONS:
        return "open_file_type_not_allowed"
    return ""


def local_openable_path(path: str, folder: Path | str | None = None) -> Path | None:
    """The real file a local open would start, when it is a regular file directly in the downloads
    folder whose own type (after following any link) is an allowed document type; else None."""
    try:
        root = (Path(folder) if folder else downloads_folder()).resolve()
        candidate = Path(str(path or "")).expanduser()
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if candidate.parent.resolve() != root or resolved.parent != root:
        return None
    if not resolved.is_file():
        return None
    if file_extension(candidate.name) not in OPENABLE_EXTENSIONS or file_extension(resolved.name) not in OPENABLE_EXTENSIONS:
        return None
    return resolved


def read_refusal(entry: dict[str, Any] | None) -> str:
    """Why a listed download may not be read as text ("" when it may)."""
    if not isinstance(entry, dict):
        return "read_download_not_downloaded"
    if entry.get("state") != "complete":
        return "read_download_incomplete"
    if str(entry.get("extension") or "") not in READABLE_EXTENSIONS:
        return "read_download_type_not_allowed"
    return ""


def read_text(path: str, folder: Path | str | None = None, limit: int = READ_BYTES_LIMIT) -> dict[str, Any] | None:
    """The first `limit` bytes of a regular text file directly in the downloads folder, decoded as text
    ({text, size, truncated}); None when the path is not such a file (links are resolved and re-checked)."""
    try:
        root = (Path(folder) if folder else downloads_folder()).resolve()
        candidate = Path(str(path or "")).expanduser()
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if candidate.parent.resolve() != root or resolved.parent != root or not resolved.is_file():
        return None
    if file_extension(candidate.name) not in READABLE_EXTENSIONS or file_extension(resolved.name) not in READABLE_EXTENSIONS:
        return None
    if file_extension(resolved.name) in DOCUMENT_EXTENSIONS:
        from app.document_text import extract_text

        if file_extension(candidate.name) != file_extension(resolved.name):
            return None
        try:
            size = resolved.stat().st_size
        except OSError:
            return None
        got = extract_text(str(resolved), file_extension(resolved.name), int(limit))
        return None if got is None else {**got, "size": int(size)}
    try:
        size = resolved.stat().st_size
        with open(resolved, "rb") as handle:
            raw = handle.read(int(limit) + 1)
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    return {"text": raw[: int(limit)].decode("utf-8", errors="replace"), "size": int(size), "truncated": len(raw) > int(limit)}


def local_snapshot() -> dict[str, Any]:
    return make_snapshot(scan_folder())


def local_listing(snapshot: dict[str, Any], *, limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
    folder = str((snapshot or {}).get("folder") or "") or None
    return list_downloads(snapshot, scan_folder(folder), limit=limit)
