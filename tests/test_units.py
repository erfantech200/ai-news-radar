from datetime import date
from pathlib import Path

import feedparser
import pytest
import requests

from radar import llm as llm_mod
from radar.article import extract_text
from radar.dedup import dedupe
from radar.media import extract_media, media_from_feed_entry, youtube_id
from radar.models import Media, NewsItem, canonical_url
from radar.persian import fa_digits, gregorian_to_jalali, jalali_label
from radar.render import split_message, tg_item

FIX = Path(__file__).parent / "fixtures"


def test_canonical_url_strips_tracking_and_www():
    a = canonical_url("https://www.Example.com/a/b/?utm_source=x&id=5#frag")
    b = canonical_url("http://example.com/a/b?id=5")
    assert a == b == "https://example.com/a/b?id=5"


def test_dedupe_merges_same_url_and_similar_titles():
    items = [
        NewsItem("OpenAI launches GPT-6 with video", "https://a.com/x?utm_source=1", "A"),
        NewsItem("OpenAI launches GPT-6 with video", "https://www.a.com/x", "B"),
        NewsItem("OpenAI Launches GPT-6, With Video!", "https://c.com/y", "C"),
        NewsItem("Nvidia earnings beat estimates", "https://d.com/z", "D"),
    ]
    out = dedupe(items)
    assert len(out) == 2
    assert out[0].also_covered_by == ["C"]  # same URL is the same outlet, not "also covered"


def test_extract_media_finds_everything_useful():
    html = (FIX / "article.html").read_text()
    media = extract_media(html, "https://news.example.com/2026/11/acme-nova-2")
    urls = [m.url for m in media]
    kinds = {m.url: m.kind for m in media}
    assert urls[0] == "https://cdn.example.com/og-nova2.jpg"          # hero first
    assert urls.count("https://cdn.example.com/og-nova2.jpg") == 1     # no duplicates
    assert kinds["https://cdn.example.com/demo.mp4"] == "video"
    assert kinds["https://www.youtube.com/watch?v=AbC123xyz"] == "embed"
    assert kinds["https://twitter.com/acme/status/123456789"] == "embed"
    assert kinds["https://news.example.com/media/clip.webm"] == "video"  # relative resolved
    assert "https://cdn.example.com/benchmark-chart.png" in urls
    assert "https://cdn.example.com/lazy-photo.jpg" in urls             # data-src used
    assert "https://cdn.example.com/team-1600.jpg" in urls              # largest srcset
    assert "https://img.youtube.com/vi/AbC123xyz/maxresdefault.jpg" in urls
    assert not any("logo" in u or "tiny" in u for u in urls)


def test_youtube_id_variants():
    assert youtube_id("https://youtu.be/abc") == "abc"
    assert youtube_id("https://www.youtube.com/watch?v=abc&t=3") == "abc"
    assert youtube_id("https://www.youtube.com/shorts/abc") == "abc"
    assert youtube_id("https://www.youtube.com/@channel") is None


def test_extract_text_skips_noise():
    text, lang = extract_text((FIX / "article.html").read_text())
    assert lang == "en"
    assert "70-billion-parameter" in text and "Nova-2 Mini" in text
    assert "newsletter" not in text.lower()
    assert "should not appear" not in text


def test_feed_entry_media():
    raw = (FIX / "feed.xml").read_text().replace("{RECENT}", "Tue, 17 Nov 2026 10:00:00 +0000")
    feed = feedparser.parse(raw)
    assert media_from_feed_entry(feed.entries[0])[0].url == "https://cdn.example.com/nova2-hero.jpg"
    assert media_from_feed_entry(feed.entries[1])[0].url == "https://cdn.example.com/glasses.png"


def test_jalali():
    assert gregorian_to_jalali(2026, 11, 18) == (1405, 8, 27)
    assert gregorian_to_jalali(2026, 3, 21) == (1405, 1, 1)
    assert gregorian_to_jalali(2025, 3, 20) == (1403, 12, 30)
    assert jalali_label(date(2026, 11, 18)) == "چهارشنبه ۲۷ آبان ۱۴۰۵"
    assert fa_digits("10/10") == "۱۰/۱۰"


def test_split_message_keeps_blocks_and_limit():
    blocks = [f"<b>block {i}</b>\n" + "متن " * 150 for i in range(12)]
    parts = split_message("\n\n".join(blocks))
    assert len(parts) > 1
    assert all(len(p) <= 4000 for p in parts)
    assert all(p.count("<b>") == p.count("</b>") for p in parts)


def test_tg_item_escapes_html():
    item = NewsItem("A <b> & C", "https://x.com/a?b=1&c=2", "Src <x>")
    item.importance, item.worthiness = 8, 9
    item.deep = {"title_fa": "عنوان <خطرناک>", "summary_fa": "خلاصه & جزئیات",
                 "content_ideas": [{"format": "ریلز", "title_fa": "ایده", "hook_fa": "هوک",
                                    "outline_fa": ["یک", "دو"]}]}
    item.add_media(Media("image", "https://cdn.x.com/i.jpg"))
    out = tg_item(item, 1)
    assert "&lt;خطرناک&gt;" in out and "خلاصه &amp; جزئیات" in out
    assert 'href="https://x.com/a?b=1&amp;c=2"' in out
    assert "Src &lt;x&gt;" in out
    assert "<blockquote expandable>" in out and "</blockquote>" in out


def test_parse_json_tolerates_fences_and_chatter():
    assert llm_mod.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm_mod.parse_json('Sure! {"a": [1,2]} done') == {"a": [1, 2]}
    assert llm_mod.parse_json('[{"id": 1}]') == {"items": [{"id": 1}]}
    with pytest.raises(llm_mod.LLMError):
        llm_mod.parse_json("no json here")


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_llm_falls_back_to_next_model(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        if "model-a" in url:
            return FakeResp(429, {"error": {"message": "Quota exceeded per day: PerDay"}})
        if "model-b" in url:
            return FakeResp(404, {"error": {"message": "not found"}})
        return FakeResp(200, {"candidates": [{"content": {"parts": [
            {"text": "thinking...", "thought": True}, {"text": '{"ok": true}'}]}}]})

    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    client = llm_mod.LLM("key", models=["model-a", "model-b", "model-c"])
    assert client.generate_json("sys", "prompt") == {"ok": True}
    assert client.used_models == ["model-c"]
    # dead models are skipped on the next call
    calls.clear()
    client.generate_json("sys", "prompt")
    assert len(calls) == 1 and "model-c" in calls[0]


def test_llm_all_failing_raises(monkeypatch):
    monkeypatch.setattr(llm_mod.requests, "post",
                        lambda *a, **k: FakeResp(403, {"error": "denied"}))
    with pytest.raises(llm_mod.LLMError):
        llm_mod.LLM("key", models=["m1"]).generate_json("s", "p")
