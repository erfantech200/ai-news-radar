"""The daily run: collect → dedupe → triage agent → pick → enrich → deep agent → deliver."""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import article, prompts, render
from .collectors import collect_all
from .dedup import dedupe, similar
from .llm import LLM, LLMError
from .models import NewsItem
from .persian import edition_name
from .state import AlertState, State
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("settings", {})
    cfg.setdefault("profile", {})
    cfg.setdefault("sources", [])
    return cfg


def _triage_payload(item: NewsItem) -> dict:
    payload = {"id": item.id, "title": item.title, "source": item.source,
               "summary": item.summary[:280]}
    if item.signal:
        payload["signal"] = item.signal
    if item.also_covered_by:
        payload["also_covered_by"] = item.also_covered_by[:5]
    if item.lang and item.lang != "en":
        payload["lang"] = item.lang
    return payload


def triage(llm: LLM, profile: dict, items: list[NewsItem], previous: list[str],
           batch_size: int) -> None:
    by_id = {i.id: i for i in items}
    system = prompts.editor_system(profile)
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        result = llm.generate_json(system, prompts.triage_prompt(
            [_triage_payload(i) for i in batch], previous), max_tokens=24000, temperature=0.2)
        for row in result.get("items", []):
            item = by_id.get(str(row.get("id")))
            if not item:
                continue
            try:
                item.importance = int(row.get("importance") or 0)
                item.worthiness = int(row.get("worthiness") or 0)
                item.viral = int(row.get("viral") or 0)
            except (TypeError, ValueError):
                continue
            item.category = str(row.get("category") or "")
            item.story_key = str(row.get("story_key") or item.id)
            item.topic_key = str(row.get("topic_key") or item.story_key)
            item.already_reported = bool(row.get("already_reported"))
            item.title_fa = str(row.get("title_fa") or "")
            item.one_liner_fa = str(row.get("one_liner_fa") or "")


def _source_rank(item: NewsItem, official: set[str]) -> tuple:
    """Within one story prefer the official source, then the item with most engagement."""
    return (item.source in official, item.signal_value, len(item.summary))


def pick(items: list[NewsItem], settings: dict, official: set[str]):
    """Choose content stories and other important stories, one item per underlying event."""
    stories: dict[str, list[NewsItem]] = {}
    for item in items:
        if item.importance <= 0 or item.already_reported:
            continue
        stories.setdefault(item.story_key or item.id, []).append(item)
    leaders = []
    for group in stories.values():
        group.sort(key=lambda i: _source_rank(i, official), reverse=True)
        lead = group[0]
        lead.importance = max(i.importance for i in group)
        lead.worthiness = max(i.worthiness for i in group)
        lead.viral = max(i.viral for i in group)
        for other in group[1:]:
            if other.source != lead.source and other.source not in lead.also_covered_by:
                lead.also_covered_by.append(other.source)
            for m in other.media:
                lead.add_media(m)
            if not lead.title_fa:
                lead.title_fa, lead.one_liner_fa = other.title_fa, other.one_liner_fa
        leaders.append(lead)

    n_content = int(settings.get("content_items", 6))
    n_other = int(settings.get("other_items", 10))
    min_worth = int(settings.get("min_worthiness", 6))
    min_imp = int(settings.get("min_importance_other", 6))

    ranked = sorted(leaders, key=lambda i: (0.45 * i.worthiness + 0.3 * i.importance
                                            + 0.25 * (i.viral or i.worthiness),
                                            i.signal_value), reverse=True)
    # Keep the content picks diverse: at most `max_per_topic` stories from one saga,
    # the rest of that saga drops to the "other important news" list.
    max_per_topic = int(settings.get("max_per_topic", 1))
    content, per_topic = [], {}
    for item in ranked:
        if len(content) >= n_content or item.worthiness < min_worth:
            continue
        key = item.topic_key or item.story_key or item.id
        if per_topic.get(key, 0) >= max_per_topic:
            continue
        per_topic[key] = per_topic.get(key, 0) + 1
        content.append(item)
    chosen = {id(i) for i in content}
    others = sorted((i for i in leaders if id(i) not in chosen
                     and (i.importance >= min_imp or i.viral >= 8)),
                    key=lambda i: (i.hot, i.importance, i.signal_value), reverse=True)[:n_other]
    return content, others


def mark_alerted(items: list[NewsItem], alert_state_path: Path) -> None:
    """Flag stories that already went out as an instant alert (state restored from cache)."""
    if not alert_state_path.exists():
        return
    alerts = AlertState(alert_state_path).alerted
    urls = {a.get("url") for a in alerts}
    titles = [a.get("title", "") for a in alerts]
    for item in items:
        if item.canonical in urls or any(similar(item.title, t, 0.75) for t in titles):
            item.alerted = True


def _deep_payload(item: NewsItem) -> dict:
    images = sum(1 for m in item.media if m.kind == "image")
    videos = sum(1 for m in item.media if m.kind == "video")
    embeds = [m.url for m in item.media if m.kind == "embed"][:3]
    return {
        "id": item.id, "title": item.title, "source": item.source, "url": item.url,
        "published": item.published.isoformat() if item.published else None,
        "also_covered_by": item.also_covered_by[:5], "signal": item.signal,
        "media": {"images": images, "video_files": videos, "video_embeds": embeds},
        "text": item.text or item.summary,
    }


def deep_dive(llm: LLM, profile: dict, items: list[NewsItem], ideas: int) -> str:
    if not items:
        return ""
    result = llm.generate_json(prompts.editor_system(profile),
                               prompts.deep_prompt([_deep_payload(i) for i in items], ideas),
                               max_tokens=32000, temperature=0.6)
    by_id = {i.id: i for i in items}
    for row in result.get("items", []):
        item = by_id.get(str(row.get("id")))
        if item:
            item.deep = row
            if row.get("title_fa"):
                item.title_fa = row["title_fa"]
    return str(result.get("daily_brief_fa") or "")


def fallback_pick(items: list[NewsItem], n: int) -> list[NewsItem]:
    """No LLM available: surface the most-discussed / newest items untouched."""
    epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
    return sorted(items, key=lambda i: (i.signal_value, i.published or epoch), reverse=True)[:n]


def archive_url(rel_path: str) -> str:
    repo = os.getenv("GITHUB_REPOSITORY")
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    branch = os.getenv("GITHUB_REF_NAME", "main")
    return f"{server}/{repo}/blob/{branch}/{rel_path}" if repo else ""


def run(config_path: Path, dry_run: bool = False, llm: LLM | None = None,
        out_dir: Path | None = None) -> render.DayResult:
    cfg = load_config(config_path)
    settings, profile = cfg["settings"], cfg["profile"]
    out_dir = out_dir or ROOT
    tz = ZoneInfo(settings.get("timezone", "Australia/Melbourne"))
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(tz)
    today = local_now.date()
    since = now - timedelta(hours=float(settings.get("lookback_hours", 26)))

    state = State(out_dir / "state" / "state.json")
    # Check Telegram first, so a setup problem fails fast without spending Gemini quota.
    tg = None if dry_run else prepare_telegram(state)

    raw, failures = collect_all(cfg["sources"], since)
    fresh = [i for i in raw if not state.is_seen(i.canonical)]
    items = dedupe(fresh)
    max_items = int(settings.get("max_triage_items", 300))
    if len(items) > max_items:
        items = fallback_pick(items, max_items)
    log.info("collected %d raw, %d fresh, %d unique", len(raw), len(fresh), len(items))

    llm = llm or LLM()
    official = {s["name"] for s in cfg["sources"] if s.get("official")}
    result = render.DayResult(day=today, content_items=[], other_items=[], scanned=len(items),
                              source_count=len([s for s in cfg["sources"] if s.get("enabled", True)]),
                              failed_sources=failures, edition=edition_name(local_now.hour),
                              clock=local_now.strftime("%H:%M"))
    llm_ok = True
    try:
        if items:
            triage(llm, profile, items, state.recent_titles(),
                   int(settings.get("triage_batch_size", 150)))
        content, others = pick(items, settings, official)
    except LLMError as exc:
        log.error("triage failed: %s", exc)
        llm_ok = False
        content, others = [], fallback_pick(items, int(settings.get("other_items", 10)) + 5)
        result.degraded = ("مدل هوش مصنوعی امروز در دسترس نبود (سهمیه یا خطا). "
                           "فهرست پربحث‌ترین خبرها بدون ترجمه آمده؛ فردا خودکار درست می‌شه.")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: article.enrich(i, int(settings.get("article_chars", 6000))),
                      content))
    if llm_ok and content:
        try:
            result.daily_brief = deep_dive(llm, profile, content,
                                           int(settings.get("ideas_per_item", 2)))
        except LLMError as exc:
            log.error("deep dive failed: %s", exc)
            result.degraded = "ترجمه و ایده‌پردازی امروز انجام نشد (سهمیهٔ مدل). امتیازها و تیترها آماده‌ست."
    mark_alerted(content + others, out_dir / "state" / "alerts.json")
    result.content_items, result.other_items = content, others
    result.models = llm.used_models

    # ---- archive
    # One file per run: several editions a day must not overwrite each other.
    stem = f"reports/{today.year}/{today.isoformat()}-{local_now.strftime('%H%M')}"
    rel = f"{stem}.md"
    result.archive_url = archive_url(rel)
    report_path = out_dir / rel
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render.markdown(result), encoding="utf-8")
    data_path = out_dir / f"{stem}.json"
    data_path.write_text(json.dumps({
        "content": [i.to_dict() for i in content],
        "others": [i.to_dict() for i in others],
        "scored": [i.to_dict() for i in items if i.importance >= 4],
        "failed_sources": failures, "models": result.models,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- deliver
    if dry_run:
        print(render.tg_header(result))
        for n, item in enumerate(content, 1):
            print("\n" + "=" * 60 + "\n" + render.tg_item(item, n))
        print("\n" + "=" * 60 + "\n" + render.tg_others(others))
        print("\n" + render.tg_footer(result))
    elif tg:
        deliver(result, tg)

    # Only remember items once they were actually judged, so a failed day retries tomorrow.
    if llm_ok:
        state.mark_seen((i.canonical for i in items), today)
        state.add_reported(content + others, today)
    state.prune(today)
    state.save()
    return result


def prepare_telegram(state, fallback_chat: str = "") -> Telegram | None:
    """Chat ID comes from the TELEGRAM_CHAT_ID secret, else from state (remembered after the
    first successful discovery), else from the bot's recent messages."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set; Telegram delivery is off")
        return None
    tg = Telegram(token, os.getenv("TELEGRAM_CHAT_ID", "").strip() or state.telegram_chat_id
                  or fallback_chat)
    if not tg.chat_id:
        tg.chat_id = tg.discover_chat_id()
        state.telegram_chat_id = tg.chat_id
        state.save()  # remember it right away, even if a later step fails
        tg.text("👋 ربات وصل شد. از این به بعد گزارش روزانه همین‌جا میاد.")
    return tg


def deliver(result: render.DayResult, tg: Telegram) -> None:
    tg.text(render.tg_header(result))
    for n, item in enumerate(result.content_items, 1):
        try:
            tg.item_media(item, n)
            tg.text(render.tg_item(item, n))
        except TelegramError as exc:
            log.error("failed to send item %s: %s", item.url, exc)
    if result.other_items:
        tg.text(render.tg_others(result.other_items))
    footer = render.tg_footer(result)
    if footer:
        tg.text(footer)
    log.info("telegram: %d API calls", tg.sent)
