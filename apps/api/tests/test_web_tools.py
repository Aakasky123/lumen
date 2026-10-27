import asyncio
import functools
import json

import httpx
import pytest

from app import extension_run, vision_agent, web_tools
from app.db import init_db
from app.extension_run import run_vision_loop

PUBLIC = {
    "docs.example.org": "93.184.215.14",
    "www.example.org": "93.184.215.15",
    "html.duckduckgo.com": "52.142.124.215",
    "rebind.example.org": "93.184.215.16",
}


def fake_resolver(host, port, type=None, **_kw):
    if host.replace(".", "").isdigit():
        return [(2, 1, 6, "", (host, port))]
    if host == "rebind.example.org":
        # One public answer and one private one: a rebinding setup.
        return [(2, 1, 6, "", ("93.184.215.16", port)), (2, 1, 6, "", ("10.0.0.5", port))]
    if host == "lan-alias.example.org":
        return [(2, 1, 6, "", ("192.168.1.20", port))]
    if host == "v6-loopback.example.org":
        return [(23, 1, 6, "", ("::1", port, 0, 0))]
    if host in PUBLIC:
        return [(2, 1, 6, "", (PUBLIC[host], port))]
    raise OSError("no such host")


def transport_for(pages, calls=None):
    """pages: {(host, path): httpx.Response or callable(request) -> Response}."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        if calls is not None:
            calls.append({"connect": request.url.host, "host": host, "path": request.url.path, "method": request.method})
        page = pages.get((host, request.url.path))
        if page is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/plain"})
        return page(request) if callable(page) else page

    return httpx.MockTransport(handler)


def html(body: str, status: int = 200, **headers) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **headers})


# --- URL guard -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8001/runs",
        "http://localhost:8001/",
        "http://api.localhost/",
        "http://printer.local/",
        "http://router/",
        "http://2130706433/",  # decimal 127.0.0.1
        "http://0x7f000001/",  # hex 127.0.0.1
        "http://[::1]:7010/",
        "http://[::ffff:127.0.0.1]/",
        "http://10.0.0.8/",
        "http://172.16.4.4/",
        "http://192.168.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://lan-alias.example.org/",
        "http://v6-loopback.example.org/",
        "http://rebind.example.org/",
    ],
)
def test_private_loopback_and_reserved_hosts_are_refused(url) -> None:
    calls: list = []
    result = web_tools.fetch_url(url, transport=transport_for({}, calls), resolver=fake_resolver)
    assert result["ok"] is False
    assert result["error"] in {"blocked_host", "dns_failure"}
    assert calls == [], "nothing is sent to a refused host"


def test_real_resolution_refuses_numeric_loopback_spellings() -> None:
    # The system resolver reads these spellings as 127.0.0.1; the address check refuses it.
    for url in ("http://127.1/", "http://0177.0.0.1/", "http://127.0.0.1./"):
        result = web_tools.fetch_url(url, transport=transport_for({}))
        assert result["ok"] is False and result["error"] in {"blocked_host", "dns_failure"}, url


def test_numeric_hosts_in_a_non_canonical_spelling_are_refused_before_resolution() -> None:
    # Resolvers disagree on these (macOS reads 0177.0.0.1 as 177.0.0.1), so none reaches one.
    for url in ("http://127.1/", "http://0177.0.0.1/", "http://0x7f.0.0.1/", "http://0x7f.1/", "http://8.8.010.8/"):
        assert web_tools.fetch_url(url, transport=transport_for({}), resolver=fake_resolver)["error"] == "blocked_host", url
    web_tools.check_url("http://93.184.215.14/")
    web_tools.check_url("https://123.example.org/")


@pytest.mark.parametrize(
    "url,error",
    [
        ("file:///C:/Windows/win.ini", "unsupported_scheme"),
        ("ftp://docs.example.org/x", "unsupported_scheme"),
        ("javascript:alert(1)", "unsupported_scheme"),
        ("https://user:pw@docs.example.org/", "credentials_in_url"),
        ("", "invalid_url"),
        ("https://docs.example.org:99999/", "invalid_url"),
    ],
)
def test_structurally_unsafe_urls_are_refused(url, error) -> None:
    assert web_tools.fetch_url(url, transport=transport_for({}), resolver=fake_resolver)["error"] == error


def test_blocked_address_covers_mapped_and_special_ranges() -> None:
    for address in ("127.0.0.1", "::1", "::ffff:10.0.0.1", "2002:7f00:1::", "fe80::1", "fc00::1", "198.18.0.1", "not-an-ip"):
        assert web_tools.blocked_address(address), address
    for address in ("93.184.215.14", "2606:4700::6810:84e5"):
        assert not web_tools.blocked_address(address), address


def test_connection_is_pinned_to_the_checked_address_with_the_real_host_name() -> None:
    calls: list = []
    pages = {("docs.example.org", "/guide"): html("<html><title>Guide</title><body><p>Hello</p></body></html>")}
    result = web_tools.fetch_url("https://docs.example.org/guide", transport=transport_for(pages, calls), resolver=fake_resolver)
    assert result["ok"] is True
    assert calls == [{"connect": PUBLIC["docs.example.org"], "host": "docs.example.org", "path": "/guide", "method": "GET"}]


def test_a_redirect_to_a_private_host_is_rechecked_and_refused() -> None:
    calls: list = []
    pages = {
        ("docs.example.org", "/go"): httpx.Response(302, headers={"location": "http://127.0.0.1:8001/runs"}),
        ("www.example.org", "/hop"): httpx.Response(301, headers={"location": "http://lan-alias.example.org/admin"}),
    }
    transport = transport_for(pages, calls)
    first = web_tools.fetch_url("https://docs.example.org/go", transport=transport, resolver=fake_resolver)
    second = web_tools.fetch_url("https://www.example.org/hop", transport=transport, resolver=fake_resolver)
    assert first["error"] == "blocked_host" and second["error"] == "blocked_host"
    assert [call["host"] for call in calls] == ["docs.example.org", "www.example.org"], "the private hop is never requested"


def test_redirects_are_followed_up_to_three_and_relative_locations_resolve() -> None:
    pages = {
        ("docs.example.org", "/a"): httpx.Response(301, headers={"location": "/b"}),
        ("docs.example.org", "/b"): httpx.Response(302, headers={"location": "https://www.example.org/c"}),
        ("www.example.org", "/c"): html("<p>landed</p>"),
    }
    result = web_tools.fetch_url("https://docs.example.org/a", transport=transport_for(pages), resolver=fake_resolver)
    assert result["ok"] and result["final_url"] == "https://www.example.org/c" and result["redirects"] == 2
    assert result["text"] == "landed"

    loop = {("docs.example.org", f"/{n}"): httpx.Response(302, headers={"location": f"/{n + 1}"}) for n in range(6)}
    assert web_tools.fetch_url("https://docs.example.org/0", transport=transport_for(loop), resolver=fake_resolver)["error"] == "too_many_redirects"


def test_a_redirect_carrying_personal_data_is_refused() -> None:
    calls: list = []
    pages = {("docs.example.org", "/r"): httpx.Response(302, headers={"location": "https://www.example.org/?who=Jordan+Quill"})}
    result = web_tools.fetch_url(
        "https://docs.example.org/r",
        personal_facts=["Jordan Quill"],
        transport=transport_for(pages, calls),
        resolver=fake_resolver,
    )
    assert result["error"] == "personal_data_in_query"
    assert len(calls) == 1


# --- personal data and secrets -----------------------------------------------------------------


def test_personal_data_match_is_exact_after_normalization() -> None:
    facts = ["Jordan Quill", "jordan.quill@example.com", "(973) 555-0142", "Newark, NJ", "abc"]
    assert web_tools.personal_data_match("who is jordan  QUILL", facts)
    assert web_tools.personal_data_match("https://x.example.org/?e=jordan.quill%40example.com", facts)
    assert web_tools.personal_data_match("reverse lookup 9735550142", facts)
    assert web_tools.personal_data_match("weather newark nj", facts)
    assert not web_tools.personal_data_match("jordanquillfans", facts), "a fact is matched as whole words"
    assert not web_tools.personal_data_match("abc news", facts), "facts under 4 characters never match"
    assert not web_tools.personal_data_match("weather in newark", facts), "only the whole fact matches"
    assert not web_tools.personal_data_match(
        "weather newark nj", facts, exempt_text="What's the weather in Newark, NJ?"
    ), "a fact the user wrote into the task is theirs to look up"


def test_search_and_fetch_refuse_personal_data_and_codes_without_sending() -> None:
    calls: list = []
    transport = transport_for({}, calls)
    search = web_tools.web_search("Jordan Quill linkedin", personal_facts=["Jordan Quill"], transport=transport, resolver=fake_resolver)
    fetch = web_tools.fetch_url("https://docs.example.org/?q=jordan+quill", personal_facts=["Jordan Quill"], transport=transport, resolver=fake_resolver)
    code = web_tools.web_search("verification code 482913", transport=transport, resolver=fake_resolver)
    token = web_tools.fetch_url("https://docs.example.org/cb?token=zz", transport=transport, resolver=fake_resolver)
    assert search["error"] == fetch["error"] == "personal_data_in_query"
    assert code["error"] == token["error"] == "secret_in_query"
    assert calls == []


# --- content -------------------------------------------------------------------------------------


def test_html_extraction_drops_scripts_and_keeps_blocks_on_lines() -> None:
    title, text = web_tools.html_to_text(
        "<html><head><title>A &amp; B</title><style>p{}</style><script>var x='hidden'</script></head>"
        "<body><h1>Heading</h1><p>First <b>bold</b> line.</p><noscript>no js</noscript>"
        "<ul><li>one</li><li>two</li></ul><svg><text>icon</text></svg><p>Caf&eacute; 5&nbsp;km</p></body></html>"
    )
    assert title == "A & B"
    assert text == "Heading\nFirst bold line.\none\ntwo\nCafé 5 km"


def test_fetch_caps_text_bytes_and_refuses_binary_types() -> None:
    long_page = "<p>" + ("word " * 5000) + "</p>"
    pages = {
        ("docs.example.org", "/long"): html(long_page),
        ("docs.example.org", "/data.json"): httpx.Response(200, json={"a": 1}),
        ("docs.example.org", "/file.pdf"): httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"}),
        ("docs.example.org", "/big"): httpx.Response(200, content=b"x" * 3_000_000, headers={"content-type": "text/plain"}),
    }
    transport = transport_for(pages)
    long_result = web_tools.fetch_url("https://docs.example.org/long", transport=transport, resolver=fake_resolver)
    assert len(long_result["text"]) == web_tools.FETCH_TEXT_CHARS and long_result["text_truncated"]
    assert json.loads(web_tools.fetch_url("https://docs.example.org/data.json", transport=transport, resolver=fake_resolver)["text"]) == {"a": 1}
    assert web_tools.fetch_url("https://docs.example.org/file.pdf", transport=transport, resolver=fake_resolver)["error"] == "unsupported_content_type"
    big = web_tools.fetch_url("https://docs.example.org/big", transport=transport, resolver=fake_resolver)
    assert big["download_truncated"] and big["bytes"] == web_tools.FETCH_MAX_BYTES
    found = web_tools.fetch_url("https://docs.example.org/long", transport=transport, resolver=fake_resolver, find="WORD WORD")
    assert found["found"] is True and found["text_offset"] == 0
    tail_page = {("docs.example.org", "/tail"): html("<p>" + ("menu " * 3000) + "</p><h2>Height</h2><p>828 m</p>")}
    tail = web_tools.fetch_url("https://docs.example.org/tail", transport=transport_for(tail_page), resolver=fake_resolver, find="height")
    assert tail["found"] and tail["text_offset"] > 0 and "Height\n828 m" in tail["text"]
    assert web_tools.fetch_url("https://docs.example.org/tail", transport=transport_for(tail_page), resolver=fake_resolver, find="absent")["found"] is False
    missing = web_tools.fetch_url("https://docs.example.org/nope", transport=transport, resolver=fake_resolver)
    assert missing["ok"] is False and missing["error"] == "http_error" and missing["status"] == 404


DDG_PAGE = """
<div class="result results_links web-result"><h2 class="result__title">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.org%2Fguide&amp;rut=abc">The <b>Guide</b></a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">A short <b>summary</b> of the guide.</a></div>
<div class="result"><a class="result__a" href="https://www.example.org/direct">Direct</a>
<div class="result__snippet">Second snippet</div></div>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.org%2Fguide">Dup</a></div>
"""


def test_web_search_parses_duckduckgo_results() -> None:
    calls: list = []
    pages = {("html.duckduckgo.com", "/html/"): html(DDG_PAGE)}
    result = web_tools.web_search("example guide", transport=transport_for(pages, calls), resolver=fake_resolver)
    assert result["ok"] and result["result_count"] == 2
    assert result["results"] == [
        {"title": "The Guide", "url": "https://docs.example.org/guide", "snippet": "A short summary of the guide."},
        {"title": "Direct", "url": "https://www.example.org/direct", "snippet": "Second snippet"},
    ]
    assert calls[0]["method"] == "GET"


# --- the loop ------------------------------------------------------------------------------------


def test_normalizer_accepts_web_actions_and_rejects_bad_ones() -> None:
    search = vision_agent.normalize_action({"action": "search_web", "query": "tallest building", "reason": "r"})
    assert search["action"] == "web_search" and not search["invalid_reason"]
    fetch = vision_agent.normalize_action({"action": "fetch_url", "url": "https://docs.example.org/guide", "reason": "r"})
    assert fetch["action"] == "fetch_url" and fetch["text"] == "https://docs.example.org/guide" and not fetch["invalid_reason"]
    assert vision_agent.normalize_action({"action": "web_search", "reason": "r"})["invalid_reason"] == "missing_search_query"
    assert vision_agent.normalize_action({"action": "fetch_url", "text": "file:///c:/x", "reason": "r"})["invalid_reason"] == "invalid_fetch_url"
    prompt = vision_agent.build_prompt({"width": 800, "height": 600}, {"goal": "g", "web_results": [{"tool": "web_search", "ok": True}]})
    assert prompt["web_results"] and "web_search and fetch_url read the public web" in prompt["instruction"]
    assert "web_results" not in vision_agent.build_prompt({"width": 800, "height": 600}, {"goal": "g"})


class _Body:
    def __init__(self) -> None:
        self.url = "https://task.example.org/form"

    async def capture_screenshot(self, *args, **kwargs) -> dict:
        return {
            "ok": True,
            "image": "aGVsbG8=",
            "width": 1200,
            "height": 800,
            "url": self.url,
            "title": "Form",
            "scroll_y": 0,
            "scroll_height": 800,
            "page_evidence": {"viewport_text": "Enter the height of the Burj Khalifa.", "controls": []},
        }

    async def vision_wait(self, wait_ms: int, **_kw) -> dict:
        return {"ok": True, "action": "wait", "waited_ms": wait_ms}


def test_loop_runs_web_tools_server_side_and_feeds_results_back(monkeypatch) -> None:
    init_db()
    calls: list = []
    pages = {
        ("html.duckduckgo.com", "/html/"): html(DDG_PAGE),
        ("docs.example.org", "/guide"): html("<title>Burj</title><p>The Burj Khalifa is 828 m tall.</p><script>ignore()</script>"),
    }
    transport = transport_for(pages, calls)
    monkeypatch.setattr(web_tools, "fetch_url", functools.partial(web_tools.fetch_url, transport=transport, resolver=fake_resolver))
    monkeypatch.setattr(web_tools, "web_search", functools.partial(web_tools.web_search, transport=transport, resolver=fake_resolver))
    monkeypatch.setattr(extension_run, "_web_tool_personal_facts", lambda user_id: ["Jordan Quill"])
    events: list = []
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, kind, payload=None, *a, **k: events.append((kind, payload)))
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    decisions = iter(
        [
            {"action": "web_search", "query": "Burj Khalifa height", "reason": "Look it up."},
            {"action": "fetch_url", "text": "https://docs.example.org/guide", "reason": "Read the result."},
            {"action": "web_search", "query": "Jordan Quill", "reason": "Search the user."},
            {"action": "fetch_url", "text": "https://docs.example.org/guide", "reason": "Again."},
        ]
    )
    done = {"action": "done", "reason": "Done."}
    seen: list[dict] = []

    def decide(user_id, image, view, context):
        seen.append(context)
        return dict(next(decisions, done))

    async def narrate(kind: str, data: dict) -> None:
        pass

    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)
    asyncio.run(run_vision_loop(_Body(), url=None, narrate=narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=8))

    after_search = seen[1]
    assert after_search["web_results"][-1]["results"][0]["url"] == "https://docs.example.org/guide"
    assert "fetched_pages" not in after_search, "search snippets are never grounding evidence"
    assert any(line.startswith("web_search_result:") for line in after_search["recent_actions"])
    after_fetch = seen[2]
    assert after_fetch["web_results"][-1]["text"] == "The Burj Khalifa is 828 m tall."
    assert after_fetch["fetched_pages"][-1]["host"] == "docs.example.org"
    typed = {"action": "type", "text": "828 m", "grounding_source": "page", "grounding_quote": "Burj Khalifa is 828 m tall", "control": 1}
    check = extension_run._vision_grounding_citation_check(typed, after_fetch, {"tag": "input", "type": "text", "index": 1})
    # Review B1: a page the brain chose can echo what it put in the URL, so a quote found in fetched text
    # proves nothing by itself; only the grounding judge weighs it.
    assert not check["ok"], "fetched text is no deterministic grounding"
    entries = extension_run._grounding_judge_page_entries(
        typed, {"reason": "value_not_supported_by_quote", "source": "page"}, decision_context=after_fetch,
        bound_control={"tag": "input"}, page_evidence={}, receiving_surface_is_page=True,
    )
    assert any("docs.example.org" in entry["label"] and "828 m" in entry["value"] for entry in entries)
    echo = dict(after_fetch, fetched_pages=[dict(after_fetch["fetched_pages"][-1], requested="https://echo.example/anything?a=828+m")])
    echo_entries = extension_run._grounding_judge_page_entries(
        typed, {"reason": "value_not_supported_by_quote", "source": "page"}, decision_context=echo,
        bound_control={"tag": "input"}, page_evidence={}, receiving_surface_is_page=True,
    )
    assert not any("fetch_url" in entry["label"] for entry in echo_entries), "a page whose request carried the value supports nothing"
    after_refusal = seen[3]
    assert after_refusal["web_results"][-1]["error"] == "personal_data_in_query"
    assert any(line.startswith("web_search_failed: personal_data_in_query") for line in after_refusal["recent_actions"])
    assert any(line.startswith("web_tool_already_answered") for line in seen[4]["recent_actions"])
    assert [call["host"] for call in calls] == ["html.duckduckgo.com", "docs.example.org"], "one request each; the refusal and the repeat send nothing"
    web_events = [payload for kind, payload in events if kind == "VISION_WEB_TOOL"]
    assert [event["tool"] for event in web_events] == ["web_search", "fetch_url", "web_search"]
    assert web_events[1]["host"] == "docs.example.org" and web_events[1]["status"] == 200 and web_events[1]["bytes"] > 0
    assert web_events[2].get("argument_redacted") and "query" not in web_events[2], "a refused personal query is never logged"


def test_grounding_judge_labels_fetched_text_with_its_host() -> None:
    entries = extension_run._grounding_judge_page_entries(
        {"action": "type", "text": "828 m"},
        {"reason": "value_not_supported_by_quote", "source": "page"},
        decision_context={"fetched_pages": [{"host": "docs.example.org", "text": ("filler " * 200) + "The tower is 828 m tall."}]},
        bound_control=None,
        page_evidence={},
        receiving_surface_is_page=True,
    )
    fetched = [entry for entry in entries if "fetch_url" in entry["label"]]
    assert fetched and "docs.example.org" in fetched[0]["label"] and "untrusted" in fetched[0]["label"]
    assert "828 m" in fetched[0]["value"], "the window is cut around the value"


def test_review_nat64_and_own_addresses_are_blocked_and_answers_are_no_exemption(monkeypatch) -> None:
    from app import extension_run, web_tools

    assert web_tools.blocked_address("64:ff9b::7f00:1"), "NAT64 to 127.0.0.1"
    assert web_tools.blocked_address("64:ff9b::c0a8:101"), "NAT64 to 192.168.1.1"
    assert not web_tools.blocked_address("64:ff9b::808:808"), "NAT64 to a public address stays allowed"
    monkeypatch.setattr(web_tools, "_own_addresses", lambda: frozenset({"8.8.4.4"}))
    assert web_tools.blocked_address("8.8.4.4"), "this machine's own address"
    stated = extension_run._web_tool_user_stated_text(
        "Find flights", {"human_answers": [{"answer": "1990-04-02"}], "operator_instructions": ["use Newark"]}
    )
    assert "use Newark" in stated and "1990-04-02" not in stated, "an answer to a form question is no permission to search it"
