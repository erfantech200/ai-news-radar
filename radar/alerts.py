"""Breaking-news watcher: runs every 20 minutes, alerts instantly on very big news.

Cheap by design: no LLM call unless there is a fresh candidate, one batched call when there is,
and if the LLM is unavailable the strongest candidates still go out untranslated."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import article, prompts, render
from .collectors import collect_all
from .dedup import dedupe
from .llm import LLM, LLMError
from .models import NewsItem
from .pipeline import ROOT, load_config, prepare_telegram
from .state import AlertState, State

log = logging.getLogger(__name__)

DEFAULT_ALERT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"]
# Titles worth a look from busy, non-official feeds.
BIG_RE = re.compile(
    r"\b(launch\w*|unveil\w*|introduc\w*|releas\w*|announc\w*|debut\w*|acqui\w*|buy\w*|"
    r"billion|ceo|steps down|fired|ousted|ban\w*|lawsuit|sues|open[- ]?source|open[- ]weights?|"
    r"gpt|gemini|claude|llama|grok|deepseek|qwen|mistral|sora|veo|kimi|glm|o\d|agi|"
    r"model|chip|nvidia|openai|anthropic|outage|leak\w*)\b", re.I)
MODEL_RE = re.compile(r"\b(introduc\w*|launch\w*|releas\w*|now available|gpt|gemini|claude|"
                      r"llama|grok|deepseek|qwen|mistral|sora|veo|model)\b", re.I)
QUANT_RE = re.compile(r"(gguf|awq|gptq|exl2|mlx|int4|int8|fp8|nvfp4|bnb|4bit|8bit|-q\d)", re.I)


def group_hf(items: list[NewsItem]) -> list[NewsItem]:
    """Many repos from one org in one run = one release. Quantised copies are dropped."""
    by_org: dict[str, list[NewsItem]] = {}
    rest = []
    for item in items:
        if item.source.startswith("Hugging Face · "):
            if not QUANT_RE.search(item.url):
                by_org.setdefault(item.source, []).append(item)
        else:
            rest.append(item)
    for org_source, group in by_org.items():
        group.sort(key=lambda i: i.signal_value, reverse=True)
        lead = group[0]
        if len(group) > 1:
            names = ", ".join(i.url.rsplit("/", 1)[-1] for i in group[:6])
            org = org_source.split("· ", 1)[-1]
            lead.title = f"{org} published {len(group)} new models on Hugging Face: {names}"
            lead.summary += f"; other new repos: {names}"
        rest.append(lead)
    return rest


def strong_without_llm(item: NewsItem, official: set[str]) -> bool:
    """When the LLM is down, only the clearest signals still trigger an alert."""
    if item.origin in official and MODEL_RE.search(item.title):
        return True
    if item.signal.startswith("Hacker News") and item.signal_value >= 600:
        return True
    return False


def _payload(item: NewsItem) -> dict:
    p = {"id": item.id, "title": item.title, "source": item.source, "feed": item.origin,
         "summary": item.summary[:400]}
    if item.signal:
        p["signal"] = item.signal
    if item.published:
        p["published"] = item.published.isoformat()
    return p


def run_alerts(config_path: Path, dry_run: bool = False, llm: LLM | None = None,
               out_dir: Path | None = None) -> list[NewsItem]:
    cfg = load_config(config_path)
    acfg = cfg.get("alerts") or {}
    if not acfg.get("enabled", True):
        return []
    out_dir = out_dir or ROOT
    tz = ZoneInfo(cfg["settings"].get("timezone", "Australia/Melbourne"))
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(tz)
    today = local_now.date()

    astate = AlertState(out_dir / "state" / "alerts.json")
    digest = State(out_dir / "state" / "state.json")     # read-only here
    tg = None if dry_run else prepare_telegram(astate, digest.telegram_chat_id)
    quiet = acfg.get("quiet_hours") or []
    if tg and len(quiet) == 2 and quiet[0] <= local_now.hour < quiet[1]:
        tg.silent = True

    sources = acfg.get("sources", [])
    by_name = {s["name"]: s for s in sources}
    official = {s["name"] for s in sources if s.get("official")}
    since = now - timedelta(hours=float(acfg.get("window_hours", 3)))
    raw, failures = collect_all(sources, since)

    fresh = []
    for item in raw:
        if astate.is_seen(item.canonical) or digest.is_seen(item.canonical):
            continue
        src = by_name.get(item.origin, {})
        if item.origin not in astate.bootstrapped:
            continue          # first time we read this source: learn what exists, alert nothing
        if src.get("require_keywords") and not BIG_RE.search(item.title):
            continue
        fresh.append(item)
    astate.mark_seen((i.canonical for i in raw), today)
    astate.bootstrapped |= {s["name"] for s in sources if s["name"] not in failures}

    candidates = dedupe(group_hf(fresh))[: int(acfg.get("max_candidates", 25))]
    log.info("alerts: %d raw, %d candidates, failed sources: %s", len(raw), len(candidates),
             ", ".join(failures) or "none")
    if not candidates:
        astate.prune(today)
        astate.save()
        return []

    min_score = int(acfg.get("min_score", 8))
    llm = llm or LLM(models=acfg.get("models") or DEFAULT_ALERT_MODELS)
    previous = digest.recent_titles() + astate.recent_titles()
    by_id = {i.id: i for i in candidates}
    try:
        result = llm.generate_json(prompts.editor_system(cfg["profile"]),
                                   prompts.alert_prompt([_payload(i) for i in candidates], previous),
                                   max_tokens=8000, temperature=0.3)
        for row in result.get("items", []):
            item = by_id.get(str(row.get("id")))
            if item:
                item.alert = row
        chosen = [i for i in candidates if i.alert and not i.alert.get("already_covered")
                  and int(i.alert.get("alert_score") or 0) >= min_score]
        chosen.sort(key=lambda i: int(i.alert.get("alert_score") or 0), reverse=True)
    except LLMError as exc:
        log.error("alert judge unavailable: %s", exc)
        chosen = [i for i in candidates if strong_without_llm(i, official)]

    chosen = chosen[: int(acfg.get("max_per_run", 3))]
    for item in chosen:
        article.enrich(item, 2000)       # picks up the hero image / video
    if dry_run:
        for item in chosen:
            print(render.tg_alert(item, now) + "\n" + "=" * 60)
    elif tg:
        for item in chosen:
            tg.text(render.tg_alert(item, now))
            tg.item_media(item, 0, max_images=4, max_videos=1,
                          caption=f"🚨 {render.h(item.alert.get('title_fa') or item.title)}")
    astate.add_alerted(chosen, now.isoformat())
    astate.prune(today)
    astate.save()
    log.info("alerts sent: %d", len(chosen))
    return chosen
