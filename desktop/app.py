"""AI News Radar — desktop visualiser (Windows .exe via PyInstaller).

Runs the radar's real collectors from this PC and animates the whole pipeline: collect → dedupe →
score → tag → brief → deliver. With a read-only GitHub token it also shows the bot's latest real
Gemini report (Persian headlines, scores, content ideas).

    python desktop/app.py              # open the app
    python desktop/app.py --selftest   # check everything imports and scores (no window)
    AI-News-Radar.exe --smoke          # open, wait for the UI to boot, exit 0 (CI check)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

if getattr(sys, "frozen", False):
    BASE = Path(sys._MEIPASS)                       # PyInstaller bundle
else:
    BASE = Path(__file__).resolve().parent.parent   # repo root
    sys.path.insert(0, str(BASE))

import requests  # noqa: E402
import yaml  # noqa: E402

from desktop.scoring import score  # noqa: E402
from radar.collectors import COLLECTORS  # noqa: E402
from radar.dedup import dedupe as radar_dedupe  # noqa: E402
from radar.models import NewsItem  # noqa: E402
from radar.persian import category_name, edition_name  # noqa: E402

VERSION = "1.0.0"
APP_DIR = Path(os.getenv("APPDATA") or Path.home()) / "AINewsRadar"
SETTINGS_FILE = APP_DIR / "settings.json"
DEFAULT_REPO = "erfantech200/ai-news-radar"
KIND = {"rss": "RSS", "hackernews": "HN", "hf_papers": "HF", "hf_models": "HF", "page": "PAGE"}
HOSTS = {"hackernews": "hn.algolia.com", "hf_papers": "huggingface.co/papers",
         "hf_models": "huggingface.co/api/models"}

APP_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=str(APP_DIR / "app.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("desktop")


def load_sources() -> list[dict]:
    """Digest sources + the alert watcher's extra sources (labs without RSS, new HF models)."""
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
    out, names = [], set()
    alert_sources = (cfg.get("alerts") or {}).get("sources", [])
    for src in cfg.get("sources", []) + alert_sources:
        if src["name"] in names or not src.get("enabled", True) or src["name"].startswith("HN "):
            continue
        if src.get("type") in ("rss", "page") and any(s.get("url") == src.get("url") for s in out):
            continue
        names.add(src["name"])
        out.append(src)
    out.sort(key=lambda s: (not s.get("official"), s.get("type") != "rss"))
    return out


def item_to_dict(item: NewsItem, src: dict) -> dict:
    return {"title": item.title, "url": item.url, "source": item.source, "origin": src["name"],
            "official": bool(src.get("official")), "signal": item.signal,
            "signal_value": item.signal_value, "summary": item.summary[:300],
            "also_covered_by": item.also_covered_by,
            "published": item.published.isoformat() if item.published else None}


def dict_to_item(d: dict) -> NewsItem:
    item = NewsItem(title=d.get("title", ""), url=d.get("url", ""), source=d.get("source", ""),
                    summary=d.get("summary", ""), signal=d.get("signal", ""),
                    signal_value=int(d.get("signal_value") or 0))
    item.also_covered_by = list(d.get("also_covered_by") or [])
    item.origin = d.get("origin", "")
    return item


def read_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


class Api:
    """Methods here are callable from the UI as window.pywebview.api.<name>(...)."""

    def __init__(self, smoke: threading.Event | None = None):
        self._sources = load_sources()
        self._window = None
        self._smoke = smoke

    # ------------------------------------------------------------- pipeline
    def get_meta(self) -> dict:
        s = read_settings()
        sources = []
        for src in self._sources:
            host = HOSTS.get(src.get("type")) or urlsplit(src.get("url", "")).netloc.replace("www.", "")
            sources.append({"name": src["name"], "kind": KIND.get(src.get("type"), "RSS"), "host": host})
        return {"version": VERSION, "sources": sources, "has_token": bool(s.get("token")),
                "every": int(s.get("every") or 10)}

    def fetch_source(self, index: int) -> dict:
        src = self._sources[int(index)]
        since = datetime.now(timezone.utc) - timedelta(hours=26)
        t0 = time.perf_counter()
        try:
            items = COLLECTORS[src.get("type", "rss")](src, since)
            ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": True, "ms": ms, "items": [item_to_dict(i, src) for i in items]}
        except Exception as exc:  # noqa: BLE001 - show it as an offline source, never crash
            log.info("source %s failed: %s", src["name"], exc)
            return {"ok": False, "ms": int((time.perf_counter() - t0) * 1000),
                    "error": str(exc)[:160], "items": []}

    def dedupe(self, items: list) -> list:
        meta = {d.get("url"): d for d in items}
        out = []
        for item in radar_dedupe([dict_to_item(d) for d in items]):
            d = dict(meta.get(item.url, {}))
            d.update({"also_covered_by": item.also_covered_by, "signal": item.signal,
                      "signal_value": item.signal_value})
            out.append(d)
        return out

    def score(self, items: list) -> list:
        return [score(d) for d in items]

    # -------------------------------------------------- the bot's real report
    def load_report(self) -> dict:
        s = read_settings()
        token, repo = s.get("token", ""), s.get("repo") or DEFAULT_REPO
        if not token:
            return {"ok": False, "error": "no GitHub token in settings"}
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        try:
            files = []
            year = datetime.now().year
            for y in (year, year - 1):
                r = requests.get(f"https://api.github.com/repos/{repo}/contents/reports/{y}",
                                 headers=headers, timeout=15)
                if r.status_code == 200:
                    files = [f for f in r.json() if f.get("name", "").endswith(".json")]
                    if files:
                        break
                elif r.status_code in (401, 403):
                    return {"ok": False, "error": f"GitHub refused the token ({r.status_code})"}
            if not files:
                return {"ok": False, "error": "no reports found in the repository yet"}
            latest = sorted(files, key=lambda f: f["name"])[-1]
            raw = requests.get(latest["url"], timeout=20,
                               headers={**headers, "Accept": "application/vnd.github.raw+json"})
            raw.raise_for_status()
            data = raw.json()
        except (requests.RequestException, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:160]}

        items, ideas_total = [], 0
        for it in (data.get("content") or []) + (data.get("others") or []):
            deep = it.get("deep") or {}
            ideas = len(deep.get("content_ideas") or [])
            ideas_total += ideas
            importance, viral = int(it.get("importance") or 0), int(it.get("viral") or 0)
            tags = [t if t.startswith("#") else f"#{t}" for t in (deep.get("hashtags") or [])][:3]
            items.append({
                "title": it.get("title", ""), "source": it.get("source", ""),
                "title_fa": deep.get("title_fa") or it.get("title_fa") or it.get("title", ""),
                "importance": importance, "worthiness": int(it.get("worthiness") or 0),
                "viral": viral, "hot": viral >= 8 or importance >= 9, "ideas": ideas,
                "category_fa": category_name(it.get("category", "")), "tags": tags,
            })
        m = re.search(r"-(\d{2})(\d{2})\.json$", latest["name"])
        edition = f"نسخهٔ {edition_name(int(m.group(1)))}" if m else ""
        return {"ok": True, "file": latest["name"], "edition_fa": edition, "items": items,
                "ideas": ideas_total, "model": ", ".join(dict.fromkeys(data.get("models") or []))}

    # ---------------------------------------------------------------- settings
    def get_settings(self) -> dict:
        s = read_settings()
        return {"token": s.get("token", ""), "repo": s.get("repo") or DEFAULT_REPO,
                "every": int(s.get("every") or 10)}

    def save_settings(self, token: str, repo: str, every) -> dict:
        every = max(2, min(120, int(every or 10)))
        data = {"token": (token or "").strip(), "repo": (repo or "").strip() or DEFAULT_REPO,
                "every": every}
        SETTINGS_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
        return {"has_token": bool(data["token"]), "every": every}

    # ------------------------------------------------------------------ window
    def toggle_fullscreen(self) -> None:
        if self._window:
            self._window.toggle_fullscreen()

    def ui_ready(self) -> None:
        log.info("UI booted")
        if self._smoke:
            self._smoke.set()


def selftest() -> int:
    api = Api()
    meta = api.get_meta()
    assert meta["sources"], "no sources in config.yaml"
    assert (BASE / "desktop" / "ui" / "index.html").exists(), "UI missing"
    sample = [{"title": "Introducing GPT-7, OpenAI's new model", "url": "https://openai.com/x",
               "source": "OpenAI", "official": True, "signal": "Hacker News: 900 امتیاز، 400 کامنت"},
              {"title": "Introducing GPT-7, OpenAI's new model!", "url": "https://tc.com/y",
               "source": "TechCrunch"}]
    scored = api.score(api.dedupe(sample))
    assert len(scored) == 1 and scored[0]["hot"], scored
    print(f"selftest ok · {len(meta['sources'])} sources · {scored[0]['tags']}")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    import webview  # imported late so --selftest works without a GUI stack

    smoke = threading.Event() if "--smoke" in sys.argv else None
    api = Api(smoke)
    window = webview.create_window(
        "AI News Radar · رادار هوش مصنوعی", url=str(BASE / "desktop" / "ui" / "index.html"),
        js_api=api, width=1440, height=880, min_size=(1100, 700), background_color="#03050b")
    api._window = window
    result = {"code": 0}
    if smoke:
        def watchdog():
            ok = smoke.wait(90)
            (APP_DIR / "smoke.txt").write_text("UI_READY" if ok else "TIMEOUT", encoding="utf-8")
            result["code"] = 0 if ok else 1
            time.sleep(4 if ok else 0)      # let a few real fetches run first
            window.destroy()
        threading.Thread(target=watchdog, daemon=True).start()
    log.info("starting AI News Radar %s", VERSION)
    webview.start()
    return result["code"]


if __name__ == "__main__":
    sys.exit(main())
