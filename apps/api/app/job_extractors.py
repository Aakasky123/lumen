from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

from app.agents.job_understanding import detect_platform


EXTRACTOR_VERSION = "2026.05.25.1"


@dataclass
class ExtractedJob:
    company: str | None
    title: str | None
    location: str | None
    employment_type: str | None
    salary_range: str | None
    responsibilities: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    raw_visible_text: str = ""
    source_url: str = ""
    source_platform: str = "generic"
    extraction_confidence: float = 0.4
    warnings: list[str] = field(default_factory=list)
    extractor_name: str = "generic_visible_text"
    extractor_version: str = EXTRACTOR_VERSION


def extract_job_from_html(
    *,
    source_url: str,
    html_text: str,
    visible_text: str | None = None,
    platform: str | None = None,
) -> ExtractedJob:
    platform = platform or detect_platform(source_url)
    text = normalize_visible_text(visible_text or html_to_text(html_text))
    if platform == "greenhouse":
        return GreenhouseExtractor().extract(source_url=source_url, html_text=html_text, visible_text=text)
    if platform == "lever":
        return LeverExtractor().extract(source_url=source_url, html_text=html_text, visible_text=text)
    return GenericExtractor().extract(source_url=source_url, html_text=html_text, visible_text=text, platform=platform)


class GreenhouseExtractor:
    name = "greenhouse_deterministic"

    def extract(self, *, source_url: str, html_text: str, visible_text: str) -> ExtractedJob:
        json_ld = jobposting_json_ld(html_text)
        og_title = meta_content(html_text, "og:title")
        title_tag = first_match(html_text, [r"<title[^>]*>(.*?)</title>"])
        h1_title = first_match(html_text, [r"<h1[^>]*>(.*?)</h1>"])
        title = (
            clean(json_ld.get("title"))
            or h1_title
            or title_from_meta(og_title)
            or title_from_meta(title_tag)
            or first_match(html_text, [r'"title"\s*:\s*"([^"]+)"'])
            or first_nonempty_line(visible_text)
        )
        company = (
            clean(nested_value(json_ld, ["hiringOrganization", "name"]))
            or meta_content(html_text, "og:site_name")
            or first_match(html_text, [r'data-company-name=["\']([^"\']+)["\']', r'"company_name"\s*:\s*"([^"]+)"'])
            or company_from_url(source_url, "greenhouse")
        )
        location = (
            location_from_json_ld(json_ld)
            or first_match(html_text, [r'class=["\'][^"\']*location[^"\']*["\'][^>]*>(.*?)<', r'"location"\s*:\s*"([^"]+)"'])
            or guess_location(visible_text)
        )
        salary = extract_salary(visible_text)
        sections = sectionize(visible_text)
        responsibilities = pick_section_items(sections, ["responsibilities", "what you", "about the role", "you will"])
        requirements = pick_section_items(sections, ["requirements", "qualifications", "what we look", "you have"])
        preferred = pick_section_items(sections, ["preferred", "nice to have", "bonus"])
        warnings = []
        confidence = 0.86
        if not (responsibilities or requirements) and visible_text:
            responsibilities = clean_items(visible_lines(visible_text)[2:10], limit=5)
            warnings.append("Greenhouse section parsing used visible-text fallback.")
            confidence -= 0.08
        if not title:
            warnings.append("Title was not found with Greenhouse selectors.")
            confidence -= 0.2
        if not location:
            warnings.append("Location was not found with Greenhouse selectors.")
            confidence -= 0.1
        return ExtractedJob(
            company=clean(company),
            title=clean(title),
            location=clean(location),
            employment_type=extract_employment_type(visible_text),
            salary_range=salary,
            responsibilities=responsibilities,
            requirements=requirements,
            preferred_skills=preferred,
            raw_visible_text=visible_text,
            source_url=source_url,
            source_platform="greenhouse",
            extraction_confidence=max(0.25, confidence),
            warnings=warnings,
            extractor_name=self.name,
        )


class LeverExtractor:
    name = "lever_deterministic"

    def extract(self, *, source_url: str, html_text: str, visible_text: str) -> ExtractedJob:
        title = first_match(html_text, [r'class=["\'][^"\']*posting-headline[^"\']*["\'][\s\S]*?<h2[^>]*>(.*?)</h2>', r'<h1[^>]*>(.*?)</h1>']) or first_nonempty_line(visible_text)
        location = first_match(html_text, [r'class=["\'][^"\']*location[^"\']*["\'][^>]*>(.*?)<'])
        company = first_match(html_text, [r'property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)["\']']) or company_from_url(source_url, "lever")
        sections = sectionize(visible_text)
        responsibilities = pick_section_items(sections, ["responsibilities", "what you", "impact", "role"])
        requirements = pick_section_items(sections, ["requirements", "qualifications", "about you"])
        preferred = pick_section_items(sections, ["preferred", "bonus", "nice to have"])
        warnings = []
        confidence = 0.84
        if not title:
            warnings.append("Title was not found with Lever selectors.")
            confidence -= 0.2
        if not responsibilities and not requirements:
            warnings.append("Lever section parsing produced limited structured content.")
            confidence -= 0.15
        return ExtractedJob(
            company=clean(company),
            title=clean(title),
            location=clean(location),
            employment_type=extract_employment_type(visible_text),
            salary_range=extract_salary(visible_text),
            responsibilities=responsibilities,
            requirements=requirements,
            preferred_skills=preferred,
            raw_visible_text=visible_text,
            source_url=source_url,
            source_platform="lever",
            extraction_confidence=max(0.25, confidence),
            warnings=warnings,
            extractor_name=self.name,
        )


class GenericExtractor:
    name = "generic_visible_text"

    def extract(self, *, source_url: str, html_text: str, visible_text: str, platform: str = "generic") -> ExtractedJob:
        lines = visible_lines(visible_text)
        title = first_match(html_text, [r'<title[^>]*>(.*?)</title>', r'<h1[^>]*>(.*?)</h1>']) or (lines[0] if lines else None)
        company = company_from_url(source_url, platform)
        sections = sectionize(visible_text)
        responsibilities = pick_section_items(sections, ["responsibilities", "what you", "role"])
        requirements = pick_section_items(sections, ["requirements", "qualifications", "skills"])
        warnings = ["Generic extractor used; structured selectors were not available."]
        return ExtractedJob(
            company=clean(company),
            title=clean(title),
            location=guess_location(visible_text),
            employment_type=extract_employment_type(visible_text),
            salary_range=extract_salary(visible_text),
            responsibilities=responsibilities,
            requirements=requirements,
            preferred_skills=pick_section_items(sections, ["preferred", "nice to have", "bonus"]),
            raw_visible_text=visible_text,
            source_url=source_url,
            source_platform=platform,
            extraction_confidence=0.58 if title else 0.35,
            warnings=warnings,
            extractor_name=self.name,
        )


def html_to_text(html_text: str) -> str:
    parser = _TextParser()
    parser.feed(html_text)
    return normalize_visible_text("\n".join(parser.parts))


def normalize_visible_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sectionize(text: str) -> dict[str, list[str]]:
    lines = visible_lines(text)
    sections: dict[str, list[str]] = {}
    current = "overview"
    sections[current] = []
    for line in lines:
        lowered = line.lower().strip(":")
        if len(line) < 80 and any(key in lowered for key in ("responsibilities", "requirements", "qualifications", "preferred", "bonus", "about you", "what you", "role")):
            current = lowered
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def pick_section_items(sections: dict[str, list[str]], keys: list[str], limit: int = 8) -> list[str]:
    items: list[str] = []
    for heading, lines in sections.items():
        if any(key in heading for key in keys):
            items.extend(lines)
    return clean_items(items, limit=limit)


def visible_lines(text: str) -> list[str]:
    return [clean(line) for line in text.splitlines() if clean(line)]


def clean_items(items: list[str], limit: int = 8) -> list[str]:
    cleaned = []
    seen = set()
    for item in items:
        value = clean(re.sub(r"^[\-*•]\s*", "", item))
        if not value or value.lower() in seen or len(value) < 8:
            continue
        seen.add(value.lower())
        cleaned.append(value)
        if len(cleaned) >= limit:
            break
    return cleaned


def extract_salary(text: str) -> str | None:
    match = re.search(r"\$[\d,]+(?:\s*-\s*\$?[\d,]+)?(?:\s*(?:USD|per year|annually|/year))?", text, re.IGNORECASE)
    return clean(match.group(0)) if match else None


def extract_employment_type(text: str) -> str | None:
    lowered = text.lower()
    for label in ("full-time", "full time", "part-time", "part time", "contract", "internship"):
        if label in lowered:
            return "Full-time" if label in {"full-time", "full time"} else label.title()
    return None


def guess_location(text: str) -> str | None:
    for line in visible_lines(text)[:15]:
        if any(token in line.lower() for token in ("remote", "united states", "new york", "san francisco", "hybrid")):
            return line
    return None


def first_nonempty_line(text: str) -> str | None:
    lines = visible_lines(text)
    return lines[0] if lines else None


def first_match(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return clean(strip_tags(match.group(1)))
    return None


def meta_content(html_text: str, key: str) -> str | None:
    escaped = re.escape(key)
    patterns = [
        rf'<meta[^>]+(?:property|name)=["\']{escaped}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{escaped}["\']',
    ]
    return first_match(html_text, patterns)


def title_from_meta(value: str | None) -> str | None:
    value = clean(value)
    if not value:
        return None
    for separator in (" | ", " - ", " at "):
        if separator in value:
            return clean(value.split(separator)[0])
    return value


def jobposting_json_ld(html_text: str) -> dict:
    for match in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>', html_text, re.IGNORECASE):
        raw = html.unescape(match.group(1)).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidate = find_jobposting(payload)
        if candidate:
            return candidate
    return {}


def find_jobposting(payload) -> dict:
    if isinstance(payload, list):
        for item in payload:
            found = find_jobposting(item)
            if found:
                return found
    if isinstance(payload, dict):
        item_type = payload.get("@type")
        if item_type == "JobPosting" or (isinstance(item_type, list) and "JobPosting" in item_type):
            return payload
        for key in ("@graph", "mainEntity"):
            found = find_jobposting(payload.get(key))
            if found:
                return found
    return {}


def nested_value(payload: dict, path: list[str]) -> str | None:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, str) else None


def location_from_json_ld(payload: dict) -> str | None:
    location = payload.get("jobLocation") if isinstance(payload, dict) else None
    if isinstance(location, list):
        location = location[0] if location else None
    if isinstance(location, dict):
        address = location.get("address")
        if isinstance(address, dict):
            parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
            return clean(", ".join(str(part) for part in parts if part))
        if isinstance(address, str):
            return clean(address)
    return None


def clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = strip_tags(html.unescape(str(value)))
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def company_from_url(source_url: str, platform: str) -> str:
    parsed = urlparse(source_url)
    parts = [part for part in parsed.path.split("/") if part]
    if platform in {"greenhouse", "lever"} and parts:
        return parts[0].replace("-", " ").title()
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}
    for key in ("company", "employer", "organization", "org", "for"):
        value = str(query.get(key) or "").strip()
        if value:
            return _company_label(value)
    ignored_path = {"jobs", "job", "apply", "application", "applications", "embed"}
    for part in parts:
        value = unquote(part).strip()
        lowered = value.lower()
        if value and lowered not in ignored_path and not lowered.isdigit() and not re.fullmatch(r"[0-9a-f-]{12,}", lowered):
            return _company_label(value)
    ignored_host = {"www", "ats", "jobs", "job-boards", "boards", "careers", "career", "apply"}
    for segment in (parsed.hostname or "").split("."):
        lowered = segment.lower().strip()
        if lowered and lowered not in ignored_host:
            return _company_label(segment)
    return "Unknown Company"


def _company_label(value: str) -> str:
    words = re.sub(r"[-_]+", " ", value).split()
    return " ".join(word[:1].upper() + word[1:] for word in words) or value


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)
