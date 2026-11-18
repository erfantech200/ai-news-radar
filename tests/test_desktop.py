"""Desktop app backend (no GUI): sources, fetching, dedupe, scoring, bot report."""
import json

import pytest

import desktop.app as app
from radar.models import NewsItem


@pytest.fixture(autouse=True)
def appdata(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "settings.json")


def test_sources_merge_digest_and_alert_lists():
    names = [s["name"] for s in app.load_sources()]
    assert "Anthropic" in names and "HF new models" in names and "TechCrunch" in names
    assert len(names) == len(set(names))
    assert not any(n.startswith("HN ") for n in names)


def test_fetch_source_ok_and_failure(monkeypatch):
    api = app.Api()
    monkeypatch.setitem(app.COLLECTORS, "rss", lambda src, since: [
        NewsItem("Introducing GPT-7", "https://openai.com/gpt-7", src["name"])])
    i = next(k for k, s in enumerate(api._sources) if s.get("type") == "rss")
    res = api.fetch_source(i)
    assert res["ok"] and res["items"][0]["title"] == "Introducing GPT-7"
    json.dumps(res)  # must be serialisable for the UI

    def boom(src, since):
        raise ConnectionError("offline")
    monkeypatch.setitem(app.COLLECTORS, "rss", boom)
    res = api.fetch_source(i)
    assert not res["ok"] and "offline" in res["error"]


def test_dedupe_and_score_roundtrip():
    api = app.Api()
    items = [{"title": "OpenAI launches GPT-7 model", "url": "https://a.com/1", "source": "A", "official": True},
             {"title": "OpenAI launches GPT-7 model!", "url": "https://b.com/2", "source": "B"},
             {"title": "Show HN: tiny Lisp", "url": "https://c.com/3", "source": "c.com",
              "signal": "Hacker News: 160 امتیاز، 20 کامنت", "signal_value": 160}]
    out = api.score(api.dedupe(items))
    assert len(out) == 2
    gpt = next(o for o in out if "GPT-7" in o["title"])
    assert gpt["also_covered_by"] == ["B"] and gpt["hot"] and "#OpenAI" in gpt["tags"]
    assert gpt["category_fa"] == "مدل جدید"


def test_settings_and_report(monkeypatch):
    api = app.Api()
    assert api.load_report()["ok"] is False                     # no token yet
    assert api.save_settings("tok", "", 1) == {"has_token": True, "every": 2}
    assert api.get_settings()["repo"] == app.DEFAULT_REPO

    report = {"models": ["gemini-3.8-flash"], "content": [{
        "title": "Introducing GPT-7", "source": "OpenAI", "importance": 10, "worthiness": 9, "viral": 9,
        "category": "model_release",
        "deep": {"title_fa": "جی‌پی‌تی ۷ آمد", "hashtags": ["هوش_مصنوعی", "#OpenAI"],
                 "content_ideas": [{}, {}]}}], "others": []}

    class R:
        def __init__(self, status, payload):
            self.status_code, self._p = status, payload
        def json(self):
            return self._p
        def raise_for_status(self):
            pass

    def fake_get(url, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer tok"
        if url.endswith(".json-raw"):
            return R(200, report)
        return R(200, [{"name": "2026-11-18-0717.json", "url": "x-0717.json-raw"},
                       {"name": "2026-11-18-1317.json", "url": "x-1317.json-raw"},
                       {"name": "2026-11-18-1317.md", "url": "md"}])
    monkeypatch.setattr(app.requests, "get", fake_get)
    rep = api.load_report()
    assert rep["ok"] and rep["file"] == "2026-11-18-1317.json" and rep["edition_fa"] == "نسخهٔ ظهر"
    assert rep["items"][0]["title_fa"] == "جی‌پی‌تی ۷ آمد" and rep["items"][0]["hot"]
    assert rep["ideas"] == 2 and rep["items"][0]["tags"] == ["#هوش_مصنوعی", "#OpenAI"]


def test_selftest_passes(capsys):
    assert app.selftest() == 0
    assert "selftest ok" in capsys.readouterr().out
