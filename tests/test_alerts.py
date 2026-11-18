"""Offline tests for the breaking-news watcher."""
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from radar import alerts, http, pipeline, telegram
from radar.collectors import collect_page
from radar.llm import LLMError
from radar.models import NewsItem

PAGE = """<html><body>
<a href="/news/roadshow-launch"><h3>Anthropic launches roadshow</h3></a>
<a href="/claude-fable-and-mythos-5-2">Announcements Introducing Claude Fable 5.2</a>
<a href="/careers">Careers</a>
<a href="https://twitter.com/anthropicai">X</a>
</body></html>"""


class Resp:
    def __init__(self, content=b"", payload=None):
        self.content, self._payload = content, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link><pubDate>{format_datetime(d)}</pubDate>"
        f"<description>{t} details</description></item>" for t, u, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


class World:
    """What the internet looks like during a test; tests mutate it between runs."""
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.now = now
        self.openai = [("Our approach to safety", "https://openai.com/index/safety", now - timedelta(hours=1))]
        self.hf = {"Qwen": [{"id": "Qwen/Qwen4-old", "createdAt": (now - timedelta(days=1)).isoformat(),
                             "likes": 10}]}
        self.hn = []

    def get(self, url, timeout=15, **kw):
        if "openai.com/news/rss" in url:
            return Resp(rss(self.openai))
        if "huggingface.co/api/models" in url:
            org = url.split("author=")[1].split("&")[0]
            return Resp(payload=self.hf.get(org, []))
        if "algolia" in url:
            return Resp(payload={"hits": self.hn})
        raise AssertionError(url)

    def get_text(self, url, timeout=15):
        if "anthropic.com/news" in url:
            return PAGE, url
        raise PermissionError("no article pages in tests")


class JudgeLLM:
    def __init__(self, scores=None, fail=False):
        self.scores, self.fail, self.calls = scores or {}, fail, []
        self.used_models = []

    def generate_json(self, system, prompt, max_tokens=0, temperature=0):
        self.calls.append(prompt)
        if self.fail:
            raise LLMError("quota")
        out = []
        for line in prompt.split("\n"):
            if line.startswith('{"id"'):
                item = json.loads(line)
                score = next((v for k, v in self.scores.items() if k in item["title"]), 3)
                out.append({"id": item["id"], "alert_score": score, "already_covered": False,
                            "is_model_release": True, "title_fa": "تیتر فوری", "summary_fa": "خلاصه",
                            "why_fa": "چون بزرگه", "quick_idea": {"format": "ریلز", "hook_fa": "هوک",
                                                                  "angle_fa": "زاویه"}})
        return {"items": out}


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World()
    monkeypatch.setattr(http, "get", w.get)
    monkeypatch.setattr(http, "get_text", w.get_text)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
profile: {channel: test}
settings: {timezone: UTC}
sources: []
alerts:
  min_score: 8
  quiet_hours: [0, 0]
  sources:
    - {name: OpenAI, type: rss, url: "https://openai.com/news/rss.xml", official: true}
    - {name: Anthropic, type: page, url: "https://www.anthropic.com/news", official: true,
       link_pattern: "^https://www\\\\.anthropic\\\\.com/(news/|claude-)[a-z0-9-]+$"}
    - {name: HF new models, type: hf_models, orgs: [Qwen]}
    - {name: HN viral, type: hackernews, min_points: 300, window_hours: 12}
""", encoding="utf-8")
    return w, cfg, tmp_path


def test_collect_page_finds_posts_only():
    import radar.collectors as c
    orig = c.http.get_text
    c.http.get_text = lambda url, timeout=20: (PAGE, url)
    try:
        items = collect_page({"name": "A", "url": "https://www.anthropic.com/news",
                              "link_pattern": r"^https://www\.anthropic\.com/(news/|claude-)[a-z0-9-]+$"}, None)
    finally:
        c.http.get_text = orig
    assert [i.url for i in items] == ["https://www.anthropic.com/news/roadshow-launch",
                                      "https://www.anthropic.com/claude-fable-and-mythos-5-2"]
    assert items[0].title == "Anthropic launches roadshow"


def test_group_hf_merges_org_and_drops_quantised():
    def m(name, likes):
        return NewsItem(f"New Hugging Face model: Qwen/{name}", f"https://huggingface.co/Qwen/{name}",
                        "Hugging Face · Qwen", signal_value=likes)
    out = alerts.group_hf([m("Qwen5-72B", 50), m("Qwen5-7B", 20), m("Qwen5-72B-GGUF", 90)])
    assert len(out) == 1
    assert "2 new models" in out[0].title and "GGUF" not in out[0].title


def test_first_run_learns_silently_then_alerts_on_new(world, capsys):
    w, cfg, tmp = world
    llm = JudgeLLM({"GPT-7": 10})
    assert alerts.run_alerts(cfg, dry_run=True, llm=llm, out_dir=tmp) == []
    assert llm.calls == []                                   # bootstrap: nothing judged
    state = json.loads((tmp / "state/alerts.json").read_text())
    assert set(state["bootstrapped"]) == {"OpenAI", "Anthropic", "HF new models", "HN viral"}

    # Nothing new → still no LLM call (no quota spent on quiet runs)
    alerts.run_alerts(cfg, dry_run=True, llm=llm, out_dir=tmp)
    assert llm.calls == []

    # A big launch and a routine post appear
    w.openai.insert(0, ("Introducing GPT-7", "https://openai.com/index/gpt-7", w.now - timedelta(minutes=12)))
    w.hf["Qwen"].insert(0, {"id": "Qwen/Qwen5-72B", "createdAt": w.now.isoformat(), "likes": 5})
    w.hf["Qwen"].insert(0, {"id": "Qwen/Qwen5-72B-GGUF", "createdAt": w.now.isoformat(), "likes": 9})
    sent = alerts.run_alerts(cfg, dry_run=True, llm=llm, out_dir=tmp)
    assert len(llm.calls) == 1                               # one batched judgement
    assert "Qwen5-72B" in llm.calls[0] and "GGUF" not in llm.calls[0]
    assert [i.url for i in sent] == ["https://openai.com/index/gpt-7"]
    printed = capsys.readouterr().out
    assert "خبر فوری" in printed and "تیتر فوری" in printed and "دقیقه پیش" in printed

    # Same news again → never re-alerted
    assert alerts.run_alerts(cfg, dry_run=True, llm=llm, out_dir=tmp) == []
    assert len(llm.calls) == 1


def test_llm_down_still_alerts_on_clear_model_launch(world):
    w, cfg, tmp = world
    alerts.run_alerts(cfg, dry_run=True, llm=JudgeLLM(), out_dir=tmp)       # bootstrap
    w.openai.insert(0, ("Introducing GPT-7", "https://openai.com/index/gpt-7", w.now))
    w.openai.insert(0, ("Customer story: Acme", "https://openai.com/index/acme", w.now))
    sent = alerts.run_alerts(cfg, dry_run=True, llm=JudgeLLM(fail=True), out_dir=tmp)
    assert [i.url for i in sent] == ["https://openai.com/index/gpt-7"]


def test_quiet_hours_send_silently(world, monkeypatch):
    w, cfg, tmp = world
    cfg.write_text(cfg.read_text().replace("quiet_hours: [0, 0]", "quiet_hours: [0, 24]"))
    calls = []

    def fake_call(self, method, payload):
        if method.startswith("send") and self.silent:
            payload = {**payload, "disable_notification": True}
        calls.append((method, payload))
        return {}

    monkeypatch.setattr(telegram.Telegram, "call", fake_call)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    alerts.run_alerts(cfg, llm=JudgeLLM(), out_dir=tmp)                     # bootstrap
    w.openai.insert(0, ("Introducing GPT-7", "https://openai.com/index/gpt-7", w.now))
    alerts.run_alerts(cfg, llm=JudgeLLM({"GPT-7": 9}), out_dir=tmp)
    sends = [p for m, p in calls if m == "sendMessage"]
    assert sends and all(p.get("disable_notification") for p in sends)


def test_digest_marks_alerted_story(tmp_path):
    from radar.state import AlertState
    st = AlertState(tmp_path / "alerts.json")
    item = NewsItem("Introducing GPT-7", "https://openai.com/index/gpt-7", "OpenAI")
    item.alert = {"title_fa": "جی‌پی‌تی ۷"}
    st.add_alerted([item], "2026-11-18T01:00:00+00:00")
    st.save()
    same_url = NewsItem("Introducing GPT-7", "https://openai.com/index/gpt-7?utm_source=x", "OpenAI")
    similar_title = NewsItem("OpenAI introducing GPT-7!", "https://techcrunch.com/gpt7", "TechCrunch")
    unrelated = NewsItem("Nvidia earnings", "https://x.com/n", "X")
    pipeline.mark_alerted([same_url, similar_title, unrelated], tmp_path / "alerts.json")
    assert same_url.alerted and similar_title.alerted and not unrelated.alerted
