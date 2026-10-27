from pathlib import Path

from app.job_extractors import company_from_url, extract_job_from_html, html_to_text


FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "fixtures" / "job-pages"


def test_greenhouse_saved_html_fixture_extraction() -> None:
    html = (FIXTURE_ROOT / "greenhouse_saved.html").read_text(encoding="utf-8")
    extracted = extract_job_from_html(
        source_url="https://boards.greenhouse.io/exampleco/jobs/12345",
        html_text=html,
        visible_text=html_to_text(html),
    )

    assert extracted.source_platform == "greenhouse"
    assert extracted.extractor_name == "greenhouse_deterministic"
    assert extracted.company == "ExampleCo"
    assert extracted.title == "Backend Software Engineer"
    assert extracted.location == "New York, NY / Remote"
    assert extracted.salary_range == "$110,000 - $145,000 per year"
    assert any("Spring Boot" in item for item in extracted.requirements)
    assert extracted.extraction_confidence >= 0.75


def test_greenhouse_job_boards_fixture_uses_modern_fallbacks() -> None:
    html = (FIXTURE_ROOT / "greenhouse_job_boards_saved.html").read_text(encoding="utf-8")
    extracted = extract_job_from_html(
        source_url="https://job-boards.greenhouse.io/warp/jobs/123",
        html_text=html,
        visible_text=html_to_text(html),
    )

    assert extracted.source_platform == "greenhouse"
    assert extracted.company == "Warp"
    assert extracted.title == "Software Engineer"
    assert extracted.location == "New York, NY, US"
    assert any("Python" in item for item in extracted.requirements)
    assert extracted.extraction_confidence >= 0.75


def test_lever_saved_html_fixture_extraction() -> None:
    html = (FIXTURE_ROOT / "lever_saved.html").read_text(encoding="utf-8")
    extracted = extract_job_from_html(
        source_url="https://jobs.lever.co/leverworks/abc123",
        html_text=html,
        visible_text=html_to_text(html),
    )

    assert extracted.source_platform == "lever"
    assert extracted.extractor_name == "lever_deterministic"
    assert extracted.company == "LeverWorks"
    assert extracted.title == "Full Stack Software Engineer"
    assert extracted.location == "San Francisco, CA"
    assert any("React" in item for item in extracted.requirements)
    assert extracted.extraction_confidence >= 0.65


def test_generic_saved_html_fixture_extraction() -> None:
    html = (FIXTURE_ROOT / "generic_saved.html").read_text(encoding="utf-8")
    extracted = extract_job_from_html(
        source_url="https://careers.genericlabs.example/jobs/ai-engineer",
        html_text=html,
        visible_text=html_to_text(html),
    )

    assert extracted.source_platform == "generic"
    assert extracted.extractor_name == "generic_visible_text"
    assert extracted.title == "AI Engineer - Generic Labs"
    assert extracted.location == "Generic Labs is hiring a full-time AI Engineer in Austin, TX with hybrid flexibility."
    assert "Generic extractor used; structured selectors were not available." in extracted.warnings


def test_ashby_detection_routes_to_generic_extraction() -> None:
    html = (FIXTURE_ROOT / "generic_saved.html").read_text(encoding="utf-8")
    extracted = extract_job_from_html(
        source_url="https://jobs.ashbyhq.com/genericlabs/ai-engineer",
        html_text=html,
        visible_text=html_to_text(html),
    )

    assert extracted.source_platform == "ashby"
    assert extracted.extractor_name == "generic_visible_text"


def test_generic_ats_company_from_url_uses_company_path_slug() -> None:
    company = company_from_url(
        "https://ats.rippling.com/bridger-photonics-inc/jobs/32a26d88-c079-402e-b176-8510b0eb4c98",
        "generic",
    )

    assert company == "Bridger Photonics Inc"
