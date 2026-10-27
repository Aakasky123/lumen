"""Read-only web access for the brain: `web_search` and `fetch_url`.

Both run in the API process, never in a body, and only ever send GET requests. They are
capabilities (eyes on the public web), not intelligence: what to look up and what a result means
stays with the model. The deterministic floors here are the ones with zero judgment:

* Public hosts only. Every hop (the first request and each redirect) resolves its host and refuses
  loopback, private, link-local, multicast, reserved, and other non-global addresses, so a page or
  a redirect can never point the API at this machine (the Lumen API on 127.0.0.1, OSWorld env ports)
  or the LAN. Decimal, hex, and octal IPv4 spellings resolve to the address they name and are
  refused by that address. The connection is pinned to the checked address (TLS still verifies the
  host name via SNI), so a DNS answer that changes between check and connect cannot slip through.
* No personal data or codes go out. A query or URL that carries one of the user's stored facts
  (an exact match after normalization, facts of 4+ characters) is refused with
  `personal_data_in_query`; one carrying an OTP-like code is refused with `secret_in_query`.
* Bounded: 15 s for the whole call, at most 3 redirects, 2 MB downloaded, text, HTML, XML, and
  JSON only, and at most 6,000 characters of extracted text returned.

Results are untrusted web content. The loop shows them to the brain labelled as such and never as
instructions; fetched text is grounding evidence like text a page of the run showed, search
snippets are not (they are summaries).
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
import unicodedata
from html.parser import HTMLParser
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote_plus, unquote, unquote_plus, urljoin, urlsplit, urlunsplit

import httpx

from app.security import redact_otp_codes

FETCH_TIMEOUT_SECONDS = 15.0
FETCH_MAX_BYTES = 2_000_000
FETCH_MAX_REDIRECTS = 3
FETCH_TEXT_CHARS = 6000
SEARCH_MAX_RESULTS = 8
SEARCH_SNIPPET_CHARS = 300
PERSONAL_FACT_MIN_CHARS = 4
_MAX_URL_CHARS = 2048
_MAX_QUERY_CHARS = 400
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
_TEXT_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/xml",
        "application/xhtml+xml",
        "application/rss+xml",
        "application/atom+xml",
        "application/javascript",
    }
)
# Names that always mean this machine or a private network, whatever DNS says.
_LOCAL_NAME_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet")
# A host of numeric labels only (decimal, octal, or hex). Resolvers disagree on any spelling but the
# canonical dotted quad: Windows and glibc read 0177.0.0.1 and 127.1 as loopback, macOS reads the
# first as 177.0.0.1. No top-level domain is numeric, so such a host is never a name.
_NUMERIC_HOST = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+))*$")

Resolver = Callable[..., list[Any]]


class WebToolRefusal(Exception):
    def __init__(self, code: str, detail: str = "", *, status: int | None = None) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code
        self.status = status


# --- guards ---------------------------------------------------------------------------------------


_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_OWN_ADDRESSES: tuple[float, frozenset[str]] = (0.0, frozenset())


def _own_addresses() -> frozenset[str]:
    """The addresses this machine's own interfaces carry (refreshed every five minutes)."""
    global _OWN_ADDRESSES
    import socket
    import time

    stamp, cached = _OWN_ADDRESSES
    if time.monotonic() - stamp < 300 and stamp:
        return cached
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(str(info[4][0]).split("%", 1)[0])
    except OSError:
        pass
    _OWN_ADDRESSES = (time.monotonic(), frozenset(found))
    return _OWN_ADDRESSES[1]


def blocked_address(address: str) -> bool:
    """Whether an address is anything but a public unicast one."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is not None:
            return blocked_address(str(mapped))
        if ip.teredo is not None:
            return True
        if ip in _NAT64_PREFIX:
            # A NAT64 gateway turns these into IPv4: judge the IPv4 address it would reach.
            return blocked_address(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
    if str(ip) in _own_addresses():
        # This machine's own public address hairpins into services listening on every interface.
        return True
    return bool(
        not ip.is_global
        or ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _host_name(parts: Any) -> str:
    host = str(parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _canonical_ipv4(host: str) -> bool:
    try:
        return str(ipaddress.IPv4Address(host)) == host
    except ValueError:
        return False


def check_url(url: Any) -> Any:
    """Parse and structurally check one URL; returns its SplitResult or raises WebToolRefusal."""
    raw = str(url or "").strip()
    if not raw or len(raw) > _MAX_URL_CHARS or any(ord(ch) < 32 for ch in raw):
        raise WebToolRefusal("invalid_url", "the URL is empty, too long, or has control characters")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise WebToolRefusal("invalid_url", f"the URL does not parse: {exc}") from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise WebToolRefusal("unsupported_scheme", "only http and https URLs can be fetched")
    if parts.username or parts.password:
        raise WebToolRefusal("credentials_in_url", "a URL carrying a user name or password is never fetched")
    host = _host_name(parts)
    if not host:
        raise WebToolRefusal("invalid_url", "the URL has no host")
    if host == "localhost" or host.endswith(_LOCAL_NAME_SUFFIXES) or "." not in host.strip("[]") and ":" not in host:
        # A single-label name resolves through the LAN's own DNS (a router, a NAS).
        raise WebToolRefusal("blocked_host", "the host is this machine or a local network name")
    if _NUMERIC_HOST.match(host) and not _canonical_ipv4(host):
        raise WebToolRefusal("blocked_host", "the host is a numeric address in a non-canonical spelling")
    if port is not None and not 0 < port < 65536:
        raise WebToolRefusal("invalid_url", "the URL's port is out of range")
    return parts


def resolve_public(host: str, port: int, *, resolver: Resolver | None = None) -> str:
    """The first address the host resolves to, refusing when ANY of its addresses is not public
    (a name that answers with both a public and a private address is a rebinding setup)."""
    resolve = resolver or socket.getaddrinfo
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise WebToolRefusal("dns_failure", f"the host {host} does not resolve") from exc
    addresses = [str(info[4][0]) for info in infos or [] if info and len(info) > 4 and info[4]]
    if not addresses:
        raise WebToolRefusal("dns_failure", f"the host {host} does not resolve")
    if any(blocked_address(address) for address in addresses):
        raise WebToolRefusal(
            "blocked_host",
            f"the host {host} resolves to a loopback, private, link-local, or reserved address",
        )
    return addresses[0]


def _normalized_words(value: Any) -> str:
    text = str(value or "")
    for _ in range(2):  # URLs arrive percent-encoded, sometimes twice
        text = unquote_plus(text)
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"[^\W_]+", text))


def personal_data_match(text: Any, facts: Iterable[Any], *, exempt_text: Any = "") -> bool:
    """Whether text carries one of the user's stored facts: the fact's whole words in order (any
    case, punctuation, or URL encoding), or for a fact with 7+ digits (a phone, an account) the same
    digit string. Facts the user wrote into this task themselves (`exempt_text`) do not count.
    Value-free: only the boolean leaves this function."""
    words = f" {_normalized_words(text)} "
    digits = re.sub(r"\D", "", _normalized_words(text))
    exempt = f" {_normalized_words(exempt_text)} " if exempt_text else ""
    for raw in facts:
        fact = _normalized_words(raw)
        if len(fact.replace(" ", "")) < PERSONAL_FACT_MIN_CHARS:
            continue
        if exempt and f" {fact} " in exempt:
            continue
        if f" {fact} " in words:
            return True
        fact_digits = re.sub(r"\D", "", fact)
        if len(fact_digits) >= 7 and fact_digits in digits:
            return True
    return False


# A code-like token right after a word that names a code or credential ("code 482913",
# "pin=4821", "password: hunter2x"), and a URL parameter that names one, whatever its value.
_SECRET_CONTEXT = re.compile(
    r"\b(?:otp|totp|pin|passcode|password|passwd|pwd|code|token|secret|cvv|cvc|verification)\b"
    r"[^\w]{0,20}([A-Za-z0-9_-]{4,64})",
    re.IGNORECASE,
)
_SECRET_PARAMETERS = frozenset(
    {
        "otp", "totp", "pin", "passcode", "password", "passwd", "pwd", "code", "token", "secret",
        "access_token", "id_token", "refresh_token", "api_key", "apikey", "key", "session", "sid",
        "auth", "authorization", "cvv", "cvc",
    }
)


def carries_secret(text: Any) -> bool:
    """Whether text carries a credential or one-time code: a digit-bearing token after a word
    that names one, a URL parameter that names one, or anything `security.redact_otp_codes`
    redacts. Plain pattern bookkeeping; refusing costs the brain one rephrase."""
    decoded = unquote_plus(unquote_plus(str(text or "")))
    if redact_otp_codes(decoded) != decoded:
        return True
    if any(any(ch.isdigit() for ch in match.group(1)) for match in _SECRET_CONTEXT.finditer(decoded)):
        return True
    try:
        query = urlsplit(str(text or "")).query if "://" in str(text or "") else ""
    except ValueError:
        query = ""
    return any(
        name.strip().lower() in _SECRET_PARAMETERS and any(value.strip() for value in values)
        for name, values in parse_qs(query, keep_blank_values=True).items()
    )


def _check_outbound(text: str, facts: Iterable[Any], exempt_text: Any) -> None:
    if personal_data_match(text, facts, exempt_text=exempt_text):
        raise WebToolRefusal(
            "personal_data_in_query",
            "it carries one of the user's stored personal facts; the web tools never send the user's data out",
        )
    if carries_secret(text):
        raise WebToolRefusal(
            "secret_in_query",
            "it carries a code-like token next to a word such as code, pin, or otp; codes never leave the run",
        )


# --- text extraction ----------------------------------------------------------------------------

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head", "iframe", "object", "canvas", "nav"})
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dd", "dt", "dl", "hr", "main",
        "nav", "aside", "form", "figure", "figcaption", "td", "th", "title", "option",
    }
)


_MAIN_TAGS = frozenset({"main", "article"})
# A page's own main content, once it holds this much text, stands in for the whole page (site menus
# and sidebars would otherwise fill the returned characters first).
_MAIN_CONTENT_MIN_CHARS = 400


class _ReadableText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.main_parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._main_depth = 0
        self._in_title = False
        self._title_done = False

    def _emit(self, text: str) -> None:
        self.parts.append(text)
        if self._main_depth:
            self.main_parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title" and not self._title_done:
            self._in_title = True
        elif tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _MAIN_TAGS:
            self._main_depth += 1
        if tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            if self._in_title:
                self._title_done = True
            self._in_title = False
        elif tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _MAIN_TAGS and self._main_depth:
            self._main_depth -= 1
        if tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif not self._skip_depth:
            self._emit(data)


def html_to_text(html: str) -> tuple[str, str]:
    """(title, readable text) of an HTML document: scripts, styles, and head dropped, block
    elements on their own lines, whitespace collapsed; the page's main/article content alone when
    it has one with real text."""
    parser = _ReadableText()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup keeps what was read
        pass
    title = " ".join("".join(parser.title_parts).split())
    main = _collapse_lines("".join(parser.main_parts))
    text = main if len(main) >= _MAIN_CONTENT_MIN_CHARS else _collapse_lines("".join(parser.parts))
    return title[:300], text


def _collapse_lines(text: str) -> str:
    lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
    return "\n".join(out)


# --- transport ----------------------------------------------------------------------------------


def _content_type(headers: Any) -> tuple[str, str]:
    raw = str(headers.get("content-type") or "")
    media = raw.split(";", 1)[0].strip().lower()
    charset_match = re.search(r"charset=([\w.:-]+)", raw, re.IGNORECASE)
    return media, (charset_match.group(1).strip("\"'") if charset_match else "")


def _textual(media: str) -> bool:
    return (
        not media
        or media.startswith("text/")
        or media in _TEXT_CONTENT_TYPES
        or media.endswith("+json")
        or media.endswith("+xml")
    )


def _decode(body: bytes, charset: str, media: str) -> str:
    encoding = charset
    if not encoding and ("html" in media or not media):
        meta = re.search(rb"<meta[^>]+charset=[\"']?([\w.:-]+)", body[:4096], re.IGNORECASE)
        if meta:
            encoding = meta.group(1).decode("ascii", "ignore")
    try:
        return body.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _get(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    timeout_seconds: float = FETCH_TIMEOUT_SECONDS,
    max_bytes: int = FETCH_MAX_BYTES,
    max_redirects: int = FETCH_MAX_REDIRECTS,
    on_redirect: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """GET one public URL, following up to max_redirects redirects, every hop re-checked and pinned
    to the address that was checked. Raises WebToolRefusal on any refusal or failure."""
    deadline = time.monotonic() + timeout_seconds
    current = url
    redirects = 0
    # A fresh client per call: each hop connects to a checked address with its own SNI, and a
    # pooled connection made for one host name must never carry another's request.
    with httpx.Client(transport=transport, follow_redirects=False, trust_env=False) as client:
        while True:
            parts = check_url(current)
            host = _host_name(parts)
            scheme = parts.scheme.lower()
            port = parts.port or (443 if scheme == "https" else 80)
            address = resolve_public(host.strip("[]"), port, resolver=resolver)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebToolRefusal("timeout", f"the fetch took longer than {int(timeout_seconds)} s")
            pinned_host = f"[{address}]" if ":" in address else address
            netloc = pinned_host if parts.port is None else f"{pinned_host}:{parts.port}"
            pinned = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
            bracketed = f"[{host}]" if ":" in host else host
            host_header = bracketed if parts.port is None else f"{bracketed}:{parts.port}"
            request = client.build_request(
                "GET",
                pinned,
                headers={
                    "Host": host_header,
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.5",
                    "Accept-Language": "en-US,en;q=0.8",
                },
                extensions={
                    "timeout": httpx.Timeout(remaining).as_dict(),
                    **({"sni_hostname": host} if scheme == "https" else {}),
                },
            )
            try:
                response = client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise WebToolRefusal("timeout", f"the fetch took longer than {int(timeout_seconds)} s") from exc
            except httpx.HTTPError as exc:
                raise WebToolRefusal("network_error", f"{type(exc).__name__}: {str(exc)[:160]}") from exc
            try:
                if response.status_code in _REDIRECT_STATUSES and response.headers.get("location"):
                    redirects += 1
                    if redirects > max_redirects:
                        raise WebToolRefusal("too_many_redirects", f"more than {max_redirects} redirects")
                    current = urljoin(current, str(response.headers.get("location")))
                    if on_redirect is not None:
                        on_redirect(current)
                    continue
                media, charset = _content_type(response.headers)
                if not _textual(media):
                    raise WebToolRefusal(
                        "unsupported_content_type",
                        f"the response is {media}; only text, HTML, XML, and JSON are read",
                        status=response.status_code,
                    )
                chunks: list[bytes] = []
                size = 0
                download_truncated = False
                try:
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= max_bytes:
                            download_truncated = True
                            break
                        if time.monotonic() > deadline:
                            raise WebToolRefusal("timeout", f"the fetch took longer than {int(timeout_seconds)} s")
                except httpx.TimeoutException as exc:
                    raise WebToolRefusal("timeout", f"the fetch took longer than {int(timeout_seconds)} s") from exc
                except httpx.HTTPError as exc:
                    raise WebToolRefusal("network_error", f"{type(exc).__name__}: {str(exc)[:160]}") from exc
                body = b"".join(chunks)[:max_bytes]
                return {
                    "final_url": current,
                    "host": host,
                    "status": response.status_code,
                    "content_type": media,
                    "body": _decode(body, charset, media),
                    "bytes": len(body),
                    "download_truncated": download_truncated,
                    "redirects": redirects,
                }
            finally:
                response.close()


# --- tools ----------------------------------------------------------------------------------------


def fetch_url(
    url: Any,
    *,
    personal_facts: Iterable[Any] = (),
    exempt_text: Any = "",
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    text_chars: int = FETCH_TEXT_CHARS,
    find: Any = "",
) -> dict[str, Any]:
    """GET a public page and return its readable text: from its start, or with `find` from a little
    before that text's first occurrence (any letter case), so a long page's later part is
    reachable. Never raises: a refusal or failure is {"ok": False, "error": code, "detail": ...}."""
    started = time.monotonic()
    requested = str(url or "").strip()
    facts = list(personal_facts or ())
    result: dict[str, Any] = {"tool": "fetch_url", "url": requested[:_MAX_URL_CHARS]}
    try:
        parts = check_url(requested)
        result["host"] = _host_name(parts)
        _check_outbound(requested, facts, exempt_text)

        def recheck(next_url: str) -> None:
            # A redirect's target is a request this tool makes too: same structural and data rules.
            check_url(next_url)
            _check_outbound(next_url, facts, exempt_text)

        fetched = _get(requested, transport=transport, resolver=resolver, on_redirect=recheck)
    except WebToolRefusal as refusal:
        result.update({"ok": False, "error": refusal.code, "detail": refusal.detail})
        if refusal.status is not None:
            result["status"] = refusal.status
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        return result
    media = fetched["content_type"]
    body = fetched["body"]
    if "html" in media or (not media and "<html" in body[:2000].lower()):
        title, text = html_to_text(body)
    else:
        title, text = "", _collapse_lines(body)
    status = int(fetched["status"])
    limit = max(0, int(text_chars))
    offset = 0
    needle = " ".join(str(find or "").split())
    found: bool | None = None
    if needle:
        # Any whitespace between the words matches (a table cell or a line break between them).
        match = re.search(r"\s+".join(re.escape(word) for word in needle.split()), text, re.IGNORECASE)
        found = match is not None
        if match is not None:
            offset = max(0, min(match.start() - limit // 10, len(text) - limit))
    window = text[offset : offset + limit]
    result.update(
        {
            "ok": 200 <= status < 300,
            "final_url": fetched["final_url"],
            "host": fetched["host"],
            "status": status,
            "content_type": media or "unknown",
            "title": title,
            "text": window,
            "text_offset": offset,
            "text_truncated": len(text) > len(window),
            "text_chars": len(text),
            **({"find": needle[:200], "found": found} if needle else {}),
            "bytes": fetched["bytes"],
            "download_truncated": fetched["download_truncated"],
            "redirects": fetched["redirects"],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    )
    if not result["ok"]:
        result["error"] = "http_error"
        result["detail"] = f"the server answered HTTP {status}"
    return result


class _DuckDuckGoResults(HTMLParser):
    """Result links and snippets of DuckDuckGo's HTML endpoint (class result__a / result__snippet)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._field = ""
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if tag == "a" and "result__a" in classes:
            self.results.append({"title": "", "url": _ddg_target(attributes.get("href", "")), "snippet": ""})
            self._field, self._buffer = "title", []
        elif "result__snippet" in classes and self.results:
            self._field, self._buffer = "snippet", []

    def handle_endtag(self, tag: str) -> None:
        if self._field and tag in {"a", "div", "td", "span"}:
            text = " ".join("".join(self._buffer).split())
            if text and self.results and not self.results[-1][self._field]:
                self.results[-1][self._field] = text
            self._field, self._buffer = "", []

    def handle_data(self, data: str) -> None:
        if self._field:
            self._buffer.append(data)


def _ddg_target(href: str) -> str:
    """DuckDuckGo wraps each result in a redirect (/l/?uddg=<target>); unwrap it."""
    link = href if not href.startswith("//") else f"https:{href}"
    try:
        parts = urlsplit(link)
    except ValueError:
        return ""
    if parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg", [""])[0]
        return unquote(target)
    return link


def search_duckduckgo_html(
    query: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
) -> list[dict[str, str]]:
    """Keyless search through DuckDuckGo's HTML endpoint (the same guarded GET as fetch_url)."""
    fetched = _get(
        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
        transport=transport,
        resolver=resolver,
    )
    if not 200 <= int(fetched["status"]) < 300:
        raise WebToolRefusal("search_unavailable", f"the search backend answered HTTP {fetched['status']}")
    parser = _DuckDuckGoResults()
    try:
        parser.feed(fetched["body"])
        parser.close()
    except Exception:  # noqa: BLE001 - partial markup keeps what was read
        pass
    return parser.results


# One function per backend; `LUMEN_WEB_SEARCH_BACKEND` picks one. A keyed API slots in here
# with the same signature: (query, *, transport, resolver) -> [{"title", "url", "snippet"}].
SEARCH_BACKENDS: dict[str, Callable[..., list[dict[str, str]]]] = {
    "duckduckgo_html": search_duckduckgo_html,
}


def web_search(
    query: Any,
    *,
    personal_facts: Iterable[Any] = (),
    exempt_text: Any = "",
    backend: str = "duckduckgo_html",
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
) -> dict[str, Any]:
    """Search the public web; up to SEARCH_MAX_RESULTS {title, url, snippet}. Never raises."""
    started = time.monotonic()
    text = " ".join(str(query or "").split())
    result: dict[str, Any] = {"tool": "web_search", "query": text[:_MAX_QUERY_CHARS], "backend": backend}
    try:
        if not text:
            raise WebToolRefusal("missing_query", "the search query is empty")
        if len(text) > _MAX_QUERY_CHARS:
            raise WebToolRefusal("query_too_long", f"a search query is at most {_MAX_QUERY_CHARS} characters")
        _check_outbound(text, list(personal_facts or ()), exempt_text)
        search = SEARCH_BACKENDS.get(backend)
        if search is None:
            raise WebToolRefusal("search_unavailable", f"no search backend named {backend}")
        raw_results = search(text, transport=transport, resolver=resolver)
    except WebToolRefusal as refusal:
        result.update({"ok": False, "error": refusal.code, "detail": refusal.detail})
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        return result
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_results:
        link = str(item.get("url") or "")
        if not link.lower().startswith(("http://", "https://")) or link in seen:
            continue
        seen.add(link)
        results.append(
            {
                "title": str(item.get("title") or "")[:200],
                "url": link[:_MAX_URL_CHARS],
                "snippet": str(item.get("snippet") or "")[:SEARCH_SNIPPET_CHARS],
            }
        )
        if len(results) >= SEARCH_MAX_RESULTS:
            break
    result.update(
        {
            "ok": True,
            "results": results,
            "result_count": len(results),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    )
    return result


def resume_contact_tokens(resume_text: Any) -> list[str]:
    """Email addresses and phone-shaped numbers in a resume's text: the identifying spans a free
    text resume holds that the stored facts may not repeat. Plain pattern bookkeeping."""
    text = str(resume_text or "")
    emails = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
    phones = [match for match in re.findall(r"\+?\d[\d\s().-]{6,}\d", text) if sum(ch.isdigit() for ch in match) >= 7]
    return [*emails[:10], *phones[:10]]
