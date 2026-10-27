"""The controlled local eval pages are served (so ChromeExtensionBody can drive real Chrome against
pages we author). Whitelisted by filename; unknown names 404."""

from fastapi.testclient import TestClient

from app.agent_eval.tasks import real_tasks
from app.main import app

client = TestClient(app)


def test_download_page_serves_with_pricing_link() -> None:
    resp = client.get("/eval/pages/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "pricing-2026.pdf" in resp.text and "Download complete" in resp.text


def test_refunds_and_form_pages_serve() -> None:
    assert "Refund policy" in client.get("/eval/pages/refunds").text
    form = client.get("/eval/pages/form").text
    assert "Ready for review" in form and "Submit request" in form


def test_unknown_page_404() -> None:
    assert client.get("/eval/pages/nope").status_code == 404


def test_real_tasks_point_at_served_pages() -> None:
    tasks = real_tasks("http://127.0.0.1:8001")
    ids = {t.id for t in tasks}
    assert "real_download_pricing_pdf" in ids
    # every real task targets an /eval/pages/* url and carries NO fixture (-> ChromeExtensionBody)
    for task in tasks:
        assert "/eval/pages/" in task.initial_url
        assert task.fixture is None
