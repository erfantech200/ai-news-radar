"""Offline end-to-end run: fake feeds, fake article pages, fake LLM, fake Telegram."""
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from radar import http, pipeline, telegram
from radar.llm import LLMError

FIX = Path(__file__).parent / "fixtures"


class FakeHTTPResp:
    def __init__(self, content=b"", payload=None):
        self.content = content
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def fake_get(url, timeout=15, **kw):
    recent = format_datetime(datetime.now(timezone.utc) - timedelta(hours=3))
    if "news.example.com/feed" in url:
        return FakeHTTPResp((FIX / "feed.xml").read_text().replace("{RECENT}", recent).encode())
    if "algolia" in url:
        now = int(datetime.now(timezone.utc).timestamp())
        return FakeHTTPResp(payload={"hits": [
            {"objectID": "1", "title": "Acme releases Nova-2, an open model that beats GPT on coding",
             "url": "https://news.example.com/2026/11/acme-nova-2", "points": 512,
             "num_comments": 230, "created_at_i": now - 3600},
            {"objectID": "2", "title": "Show HN: I built a tiny Lisp", "url": "https://lisp.dev",
             "points": 160, "num_comments": 40, "created_at_i": now - 7200},
        ]})
    if "broken.example" in url:
        raise ConnectionError("boom")
    raise AssertionError(f"unexpected GET {url}")


def fake_get_text(url, timeout=15):
    if "acme-nova-2" in url:
        return (FIX / "article.html").read_text(), url
    raise PermissionError("blocked by robots.txt")


class FakeLLM:
    def __init__(self, fail_deep=False, fail_all=False):
        self.used_models = []
        self.fail_deep, self.fail_all = fail_deep, fail_all
        self.prompts = []

    def generate_json(self, system, prompt, max_tokens=0, temperature=0):
        self.prompts.append(prompt)
        if self.fail_all:
            raise LLMError("quota")
        self.used_models.append("fake-model")
        items = [json.loads(line) for line in prompt.split("\n") if line.startswith('{"id"')]
        if "Score every item" in prompt:
            out = []
            for it in items:
                if "Nova-2" in it["title"]:
                    out.append({"id": it["id"], "importance": 9, "worthiness": 9,
                                "category": "model_release", "story_key": "acme-nova-2",
                                "title_fa": "مدل باز نوا-۲", "one_liner_fa": "یک مدل باز جدید."})
                elif "glasses" in it["title"]:
                    out.append({"id": it["id"], "importance": 6, "worthiness": 4,
                                "category": "business_funding", "story_key": "glasses",
                                "title_fa": "عینک هوشمند", "one_liner_fa": "۴۰ میلیون دلار سرمایه."})
                else:
                    out.append({"id": it["id"], "importance": 3, "worthiness": 2,
                                "category": "other_tech", "story_key": it["id"]})
            return {"items": out}
        if self.fail_deep:
            raise LLMError("quota")
        return {"daily_brief_fa": "امروز روز مدل‌های باز بود.", "items": [{
            "id": it["id"], "title_fa": "نوا-۲ آمد", "summary_fa": "خلاصه فارسی.",
            "key_points_fa": ["۷۰ میلیارد پارامتر"], "why_it_matters_fa": "چون رایگانه.",
            "content_ideas": [{"format": "ریلز", "title_fa": "ایده", "hook_fa": "هوک",
                               "angle_fa": "زاویه", "outline_fa": ["۱", "۲", "۳"],
                               "visual_tip_fa": "از نمودار استفاده کن"}],
            "hashtags": ["#هوش_مصنوعی", "AI"], "caution_fa": ""} for it in items]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "get", fake_get)
    monkeypatch.setattr(http, "get_text", fake_get_text)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
profile: {channel: test}
settings: {content_items: 3, other_items: 5}
sources:
  - {name: Example News, type: rss, url: "https://news.example.com/feed", official: true}
  - {name: Hacker News, type: hackernews}
  - {name: Broken, type: rss, url: "https://broken.example/feed"}
""", encoding="utf-8")
    return tmp_path, cfg


def test_full_run(setup, capsys):
    tmp, cfg = setup
    llm = FakeLLM()
    result = pipeline.run(cfg, dry_run=True, llm=llm, out_dir=tmp)

    assert result.failed_sources == ["Broken"]
    assert len(result.content_items) == 1
    lead = result.content_items[0]
    # RSS (official) and HN copies merged into one story; HN engagement kept
    assert lead.source == "Example News"
    assert lead.discussion_url.startswith("https://news.ycombinator.com")
    assert lead.also_covered_by == []
    assert lead.deep["title_fa"] == "نوا-۲ آمد"
    kinds = {m.kind for m in lead.media}
    assert {"image", "video", "embed"} <= kinds
    assert [i.title_fa for i in result.other_items] == ["عینک هوشمند"]
    assert result.daily_brief == "امروز روز مدل‌های باز بود."
    assert len(llm.prompts) == 2  # one triage call + one deep call: light on free quota

    printed = capsys.readouterr().out
    assert "نوا-۲ آمد" in printed and "مدیا برای دانلود" in printed
    reports = list((tmp / "reports").rglob("*.md"))
    assert reports and "نوا-۲ آمد" in reports[0].read_text()
    state = json.loads((tmp / "state/state.json").read_text())
    assert state["seen"] and state["reported"]

    # Second run the same day: everything already seen, nothing repeated.
    result2 = pipeline.run(cfg, dry_run=True, llm=FakeLLM(), out_dir=tmp)
    assert result2.scanned == 0 and not result2.content_items


def test_deep_failure_still_delivers_headlines(setup):
    tmp, cfg = setup
    result = pipeline.run(cfg, dry_run=True, llm=FakeLLM(fail_deep=True), out_dir=tmp)
    assert result.content_items and not result.content_items[0].deep
    assert result.degraded


def test_llm_down_sends_raw_list_and_retries_tomorrow(setup):
    tmp, cfg = setup
    result = pipeline.run(cfg, dry_run=True, llm=FakeLLM(fail_all=True), out_dir=tmp)
    assert result.degraded and result.other_items and not result.content_items
    state = json.loads((tmp / "state/state.json").read_text())
    assert state["seen"] == {}  # not marked, so tomorrow's run judges them properly


def test_telegram_delivery(setup, monkeypatch):
    tmp, cfg = setup
    calls = []

    def fake_call(self, method, payload):
        calls.append((method, payload))
        if method == "sendMediaGroup":
            n = len(payload["media"])
            raise telegram.TelegramError(
                f'sendMediaGroup: failed to send message #{n} with "WEBPAGE_CURL_FAILED"')
        if method == "getUpdates":
            return [{"message": {"chat": {"id": 42}}}]
        return {}

    monkeypatch.setattr(telegram.Telegram, "call", fake_call)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    pipeline.run(cfg, dry_run=False, llm=FakeLLM(), out_dir=tmp)
    methods = [m for m, _ in calls]
    assert methods[0] == "getUpdates"
    assert "sendPhoto" in methods          # album failed → single photo fallback
    assert "sendVideo" in methods
    assert all(p.get("chat_id") == "42" for m, p in calls if m != "getUpdates")
    texts = [p["text"] for m, p in calls if m == "sendMessage"]
    assert any("ربات وصل شد" in t for t in texts)
    albums = [len(p["media"]) for m, p in calls if m == "sendMediaGroup"]
    assert albums[:2] == [albums[0], albums[0] - 1]   # bad picture dropped, album retried
    # chat id remembered: the next run needs no getUpdates
    assert json.loads((tmp / "state/state.json").read_text())["telegram_chat_id"] == "42"
    calls.clear()
    (tmp / "state/state.json").write_text(json.dumps({"telegram_chat_id": "42"}))
    pipeline.run(cfg, dry_run=False, llm=FakeLLM(), out_dir=tmp)
    assert "getUpdates" not in [m for m, _ in calls]
    assert any("رادار هوش مصنوعی" in t for t in texts)
    assert any("خبرهای مهم دیگه" in t for t in texts)


def test_telegram_problem_fails_before_using_llm(setup, monkeypatch):
    tmp, cfg = setup

    def no_chat(self, method, payload):
        if method == "getUpdates":
            return []
        return {}

    monkeypatch.setattr(telegram.Telegram, "call", no_chat)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    llm = FakeLLM()
    with pytest.raises(telegram.TelegramError):
        pipeline.run(cfg, dry_run=False, llm=llm, out_dir=tmp)
    assert llm.prompts == []   # no Gemini quota spent
