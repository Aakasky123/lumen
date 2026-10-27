"""RFC 6238 time-based one-time codes: a capability, never a judgment.

The brain asks for a code (use_credential, credential_slot=totp); this module only computes it from a seed the
runtime holds. A seed is a CREDENTIAL: it is never returned to the brain, logged, stored in an event, or typed
anywhere. The runtime decides where a seed may come from (see `seed_source_allowed_origin` and the docs); nothing
in this file reads a page or judges what a field means.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import struct
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

_ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}
_BASE32_BODY = re.compile(r"^[A-Z2-7]+=*$")
# A seed written in the groups authenticator apps print ("JBSW Y3DP EHPK 3PXP"), or a bare one, or an otpauth URI.
_OTPAUTH_URI = re.compile(r"otpauth://totp/[^\s\"'<>]+", re.IGNORECASE)
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_PREFERRED_LENGTHS = (32, 26, 16, 52)  # 160-, 128-, 80- and 256-bit keys: what authenticators print
# Words around a pasted key that say it is an authenticator key. Evidence for scrubbing, never page meaning.
_AUTH_CONTEXT = re.compile(
    r"otpauth|authenticator|\b2fa\b|\bmfa\b|two[- ]?factor|2[- ]?step|\btotp\b|secret key|setup key|"
    r"one[- ]time (?:code|password)|(?:verification|security|6-digit|six-digit|login|auth(?:entication)?) code",
    re.IGNORECASE,
)
MIN_SECONDS_LEFT = 5.0  # wait for the next window rather than type a code about to expire
_MIN_SEED_BYTES = 10  # 80 bits: shorter is not a TOTP seed
_MAX_SEED_CHARS = 512


@dataclass(frozen=True)
class TotpParams:
    key: bytes = b""
    digits: int = 6
    period: int = 30
    algorithm: str = "SHA1"
    issuer: str = ""

    def __repr__(self) -> str:  # never print the key
        return f"TotpParams(digits={self.digits}, period={self.period}, algorithm={self.algorithm})"


def canonical_base32(text: str) -> str:
    """Upper-case base32 with grouping spaces/hyphens and padding removed ('' when it is not base32)."""
    cleaned = re.sub(r"[\s\-]", "", str(text or "")).upper().rstrip("=")
    return cleaned if cleaned and _BASE32_BODY.match(cleaned) else ""


def _decode_base32(canonical: str) -> bytes | None:
    try:
        padded = canonical + "=" * (-len(canonical) % 8)
        return base64.b32decode(padded, casefold=True)
    except (binascii.Error, ValueError):
        return None


def parse_seed(text: str) -> TotpParams | None:
    """A base32 seed or an otpauth://totp URI -> parameters (None when it is neither or is too short)."""
    raw = str(text or "").strip()
    if not raw or len(raw) > _MAX_SEED_CHARS:
        return None
    digits, period, algorithm, issuer = 6, 30, "SHA1", ""
    if raw.lower().startswith("otpauth://"):
        parts = urlsplit(raw)
        if parts.scheme.lower() != "otpauth" or parts.netloc.lower() != "totp":
            return None
        query = parse_qs(parts.query)

        def first(name: str) -> str:
            for key, values in query.items():
                if key.lower() == name and values:
                    return str(values[0])
            return ""

        secret = first("secret")
        algorithm = (first("algorithm") or "SHA1").upper()
        try:
            digits = int(first("digits") or 6)
            period = int(first("period") or 30)
        except ValueError:
            return None
        issuer = first("issuer") or unquote(parts.path.lstrip("/")).split(":", 1)[0]
    else:
        secret = raw
    if algorithm not in _ALGORITHMS or digits not in (6, 7, 8) or not 1 <= period <= 300:
        return None
    canonical = canonical_base32(secret)
    key = _decode_base32(canonical) if canonical else None
    if not key or len(key) < _MIN_SEED_BYTES:
        return None
    return TotpParams(key=key, digits=digits, period=period, algorithm=algorithm, issuer=issuer[:80])


def hotp(key: bytes, counter: int, *, digits: int = 6, algorithm: str = "SHA1") -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), _ALGORITHMS[algorithm]).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


def totp_code(params: TotpParams, *, at: float | None = None) -> str:
    moment = time.time() if at is None else float(at)
    return hotp(params.key, int(moment // params.period), digits=params.digits, algorithm=params.algorithm)


def seconds_left(params: TotpParams, *, at: float | None = None) -> float:
    moment = time.time() if at is None else float(at)
    return params.period - (moment % params.period)


def _bare_seed_spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, canonical) of each base32 key written in a text, any case, grouped by single spaces or dashes.

    A key is one token of 16+ base32 characters, or consecutive equal-width groups (the last may be shorter) that
    decode to a plausible key length. Both need 8+ distinct characters and a digit 2-7 (a plain word has none).
    Pure pattern extraction; whether a text may be scrubbed or used is the caller's call."""
    tokens = list(_TOKEN.finditer(text[:20000]))
    runs: list[list[re.Match[str]]] = []
    current: list[re.Match[str]] = []
    for token in tokens:
        valid = re.fullmatch(r"[A-Za-z2-7]+", token.group(0)) is not None
        joined = bool(current) and text[current[-1].end() : token.start()] in (" ", "-")
        if valid and (not current or joined):
            current.append(token)
            continue
        if current:
            runs.append(current)
        current = [token] if valid else []
    if current:
        runs.append(current)

    def usable(group: list[re.Match[str]]) -> str:
        canonical = canonical_base32("".join(t.group(0) for t in group))
        if len(canonical) < 16 or len(set(canonical)) < 8 or not re.search(r"[2-7]", canonical):
            return ""
        return canonical if parse_seed(canonical) is not None else ""

    spans: list[tuple[int, int, str]] = []
    for run in runs:
        ranked: list[tuple[tuple[int, int], list[re.Match[str]], str]] = []
        for lo in range(len(run)):
            first = len(run[lo].group(0))
            windows: list[list[re.Match[str]]] = []
            if first >= 16:
                windows.append(run[lo : lo + 1])
            elif 3 <= first <= 8:
                for hi in range(lo + 1, min(len(run), lo + 22) + 1):
                    width = len(run[hi - 1].group(0))
                    if hi - lo > 1 and width > first:
                        break
                    windows.append(run[lo:hi])
                    if width != first:
                        break  # a shorter group ends a key
            for group in windows:
                canonical = usable(group)
                if canonical:
                    rank = (0 if len(canonical) in _PREFERRED_LENGTHS else 1, -len(canonical))
                    ranked.append((rank, group, canonical))
        if not ranked:
            continue
        top = min(item[0] for item in ranked)
        tied = [item for item in ranked if item[0] == top]
        # Equal-ranked readings of one run (a neighbouring word as wide as a group) cannot be told apart without
        # reading meaning: the whole overlap is covered, and each reading is kept, so a use of it is refused as
        # ambiguous instead of typing a wrong code.
        start = min(item[1][0].start() for item in tied)
        end = max(item[1][-1].end() for item in tied)
        for _rank, _group, canonical in tied:
            spans.append((start, end, canonical))
    return spans


def find_seeds(text: str) -> list[str]:
    """Every distinct seed (otpauth URI or base32 key) a text shows, first-seen order.

    Pure pattern extraction, no meaning: the caller decides whether the one found may be used."""
    found: list[str] = []
    seen: set[str] = set()
    haystack = str(text or "")
    for match in _OTPAUTH_URI.finditer(haystack):
        params = parse_seed(match.group(0))
        if params is None:
            continue
        canonical = canonical_base32(base64.b32encode(params.key).decode())
        if canonical not in seen:
            seen.add(canonical)
            found.append(match.group(0))
    for _start, _end, canonical in _bare_seed_spans(haystack):
        if canonical not in seen:
            seen.add(canonical)
            found.append(canonical)
    return found


def seed_identity(seed: str) -> str:
    """The canonical base32 of a seed or URI, for redaction and comparison ('' when it is not one)."""
    params = parse_seed(seed)
    if params is None:
        return ""
    return canonical_base32(base64.b32encode(params.key).decode())


def seed_redaction_strings(seed: str) -> list[str]:
    """Every textual form of a seed a log could carry: as given, canonical, spaced in fours."""
    identity = seed_identity(seed)
    if not identity:
        return []
    forms = {str(seed).strip(), identity, " ".join(identity[i : i + 4] for i in range(0, len(identity), 4))}
    return sorted((form for form in forms if len(form) >= 8), key=len, reverse=True)


def _host_labels(url: str) -> tuple[str, list[str]]:
    host = (urlsplit(str(url or "")).hostname or "").lower().strip(".")
    labels = [label for label in host.split(".") if label]
    return host, labels


_DEV_HOSTS = {"localhost", "127.0.0.1", "::1"}


def goal_names_origin(goal_text: str, page_url: str) -> bool:
    """Whether the user's own words name this page's exact host (a leading www. is ignored).

    Exact host only: a brand or a registrable name proves nothing on shared hosting (github.io, blogspot.com),
    so "github" never matches attacker.github.io and "github.com" never matches github.com.evil.test. A
    development host must be named with its port. A goal that does not name the host refuses a seed it carries."""
    text = str(goal_text or "").lower()
    parts = urlsplit(str(page_url or ""))
    host = (parts.hostname or "").lower().strip(".")
    if not host or not text:
        return False
    if host in _DEV_HOSTS or re.fullmatch(r"[\d.]+", host):
        named = f"{host}:{parts.port}" if parts.port else host
        return re.search(rf"(?<![a-z0-9.-]){re.escape(named)}(?![a-z0-9])", text) is not None
    bare = host[4:] if host.startswith("www.") else host
    return re.search(rf"(?<![a-z0-9.-])(?:www\.)?{re.escape(bare)}(?![a-z0-9-])(?!\.[a-z0-9])", text) is not None


# --- the goal never carries a seed past run creation ------------------------------------------------

SEED_PLACEHOLDER = "[totp seed provided]"


def redact_seeds(text: str) -> tuple[str, list[str]]:
    """The text with every seed replaced by a placeholder, and the seeds that were in it.

    An otpauth URI is always a seed. A bare base32 key is scrubbed only when the same text signals an
    authenticator context (authenticator, 2FA, TOTP, secret/setup key, a verification-code instruction): an
    ordinary upper-case ID elsewhere is left alone."""
    haystack = str(text or "")
    spans: list[tuple[int, int, str]] = []
    for match in _OTPAUTH_URI.finditer(haystack):
        if parse_seed(match.group(0)) is not None:
            spans.append((match.start(), match.end(), match.group(0)))
    if _AUTH_CONTEXT.search(haystack):
        for start, end, canonical in _bare_seed_spans(haystack):
            if not any(lo <= start < hi for lo, hi, seed in spans if seed.lower().startswith("otpauth")):
                spans.append((start, end, canonical))
    out = haystack
    for start, end in sorted({(lo, hi) for lo, hi, _ in spans}, reverse=True):
        out = out[:start] + SEED_PLACEHOLDER + out[end:]
    seeds: list[str] = []
    for _start, _end, seed in sorted(spans):
        if seed_identity(seed) not in {seed_identity(item) for item in seeds}:
            seeds.append(seed)
    return out, seeds


def protect_goal(goal: str, encryption_key: str | None) -> tuple[str, list[str]]:
    """(goal safe to persist and show, encrypted seeds to keep with the run).

    Without an encryption key the seeds are dropped (the goal is still cleaned): nothing is stored in plaintext."""
    cleaned, seeds = redact_seeds(goal)
    if not seeds or not encryption_key:
        return cleaned, []
    from app.security import encrypt_secret

    return cleaned, [encrypt_secret(seed, encryption_key) for seed in seeds]


def reveal_seeds(encrypted: Any, encryption_key: str | None) -> list[str]:
    from app.security import decrypt_secret

    if not encryption_key or not isinstance(encrypted, list):
        return []
    revealed: list[str] = []
    for item in encrypted[:8]:
        try:
            revealed.append(decrypt_secret(str(item), encryption_key))
        except Exception:  # noqa: BLE001 - an unreadable seed is simply not available
            continue
    return revealed
