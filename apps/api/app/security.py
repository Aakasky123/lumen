import base64
import hashlib
import re
from typing import Any

from cryptography.fernet import Fernet


OTP_CONTEXT_RE = re.compile(
    r"((?:otp|pin|code|verification(?:\s+code)?|security\s+code|login\s+code|one[-\s]?time\s+code)[^\w]{0,20})([A-Z0-9]{4,12})",
    re.IGNORECASE,
)
OTP_TOKEN_RE = re.compile(r"\b[A-Z0-9]{4,12}\b", re.IGNORECASE)


def redact_otp_codes(value: Any, *, force: bool = False) -> Any:
    if isinstance(value, dict):
        return {key: redact_otp_codes(item, force=force or _otp_key(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_otp_codes(item, force=force) for item in value]
    if not isinstance(value, str):
        return value
    if force:
        # A value under a code-named key (otp, pin, code) is a code even when it reads like a word ("Falcon").
        return OTP_TOKEN_RE.sub(
            lambda match: "[REDACTED_OTP]" if _looks_like_otp_token(match.group(0), words_are_prose=False) else match.group(0),
            value,
        )
    return OTP_CONTEXT_RE.sub(_redact_otp_match, value)


def encrypt_secret(raw: str, key: str) -> str:
    return _fernet(key).encrypt(raw.encode("utf-8")).decode("utf-8")


def decrypt_secret(encrypted: str, key: str) -> str:
    return _fernet(key).decrypt(encrypted.encode("utf-8")).decode("utf-8")


def _fernet(key: str) -> Fernet:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


_OTP_KEY_WORDS = frozenset({"otp", "totp", "pin", "code", "passcode", "pincode", "otpcode"})


def _otp_key(key: str) -> bool:
    """A key whose value is a code: one of its words is otp/pin/code (``otp_code``, ``smsCode``, ``pin``).

    Words, not substrings: ``typing_requires_caret``, ``shipping`` or ``mapping`` merely contain "pin".
    """
    words = re.findall(r"[a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower())
    return any(word in _OTP_KEY_WORDS for word in words)


def _redact_otp_match(match: re.Match[str]) -> str:
    prefix, token = match.groups()
    if not _looks_like_otp_token(token):
        return match.group(0)
    return f"{prefix}[REDACTED_OTP]"


def _looks_like_otp_token(token: str, *, words_are_prose: bool = True) -> bool:
    stripped = token.strip()
    if not 4 <= len(stripped) <= 12:
        return False
    if any(char.isdigit() for char in stripped):
        return True
    if stripped.upper() == stripped and any(char.isalpha() for char in stripped):
        return True
    if words_are_prose and stripped.isalpha() and stripped[0].isupper() and stripped[1:].islower():
        # A Capitalized word is prose, not a code: "Enter the code: Continue" lost its button label
        # and "archive code. Opened the console" became "[REDACTED_OTP] the console" in model input.
        return False
    if stripped.lower() != stripped and stripped.upper() != stripped:
        return True
    return False
