"""Collectors: each source type turns into a list of NewsItem objects."""
from __future__ import annotations

import calendar
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urljoin, urlsplit

import feedparser
from bs4 import BeautifulSoup

from . import http
from .media import media_from_feed_entry
from .models import Media, NewsItem

log = logging.getLogger(__name__)
TAG_RE = re.compile(r"<[^>]+>")


def clean_html(text: str, limit: int = 600) -> str:
    text = unescape(TAG_RE.sub(" ", text or ""))
    return " ".join(text.split())[:limit]


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def _domain(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def collect_rss(src: dict, since: datetime) -> list[NewsItem]:
    resp = http.get(src["url"], timeout=20)
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"unreadable feed: {feed.bozo_exception}")
    items = []
    for entry in feed.entries[: src.get("max_items", 40)]:
        link = entry.get("link")
        title = clean_html(entry.get("title", ""), 300)
        if not link or not title:
            continue
        published = _entry_time(entry)
        if published and published < since:
            continue
        if published is None and not src.get("allow_undated", False):
            continue
        summary = clean_html(entry.get("summary") or entry.get("description") or "")
        item = NewsItem(title=title, url=link, source=src["name"], published=published,
                        summary=summary, lang=src.get("lang", ""))
        for m in media_from_feed_entry(entry):
            item.add_media(m)
        items.append(item)
    return items


def collect_hackernews(src: dict, since: datetime) -> list[NewsItem]:
    min_points = int(src.get("min_points", 120))
    url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=100"
           f"&numericFilters=created_at_i>{int(since.timestamp())},points>{min_points}")
    hits = http.get(url, timeout=20).json().get("hits", [])
    items = []
    for hit in hits:
        title = hit.get("title")
        if not title:
            continue
        hn_link = f"https://news.ycombinator.com/item?id={hit['objectID']}"
        link = hit.get("url") or hn_link
        points = int(hit.get("points") or 0)
        items.append(NewsItem(
            title=title, url=link,
            source=_domain(link) if hit.get("url") else "Hacker News",
            published=datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc),
            summary=clean_html(hit.get("story_text") or ""),
            signal=f"Hacker News: {points} امتیاز، {hit.get('num_comments') or 0} کامنت",
            signal_value=points, discussion_url=hn_link, lang="en"))
    items.sort(key=lambda i: i.signal_value, reverse=True)
    return items[: src.get("max_items", 40)]


def collect_hf_papers(src: dict, since: datetime) -> list[NewsItem]:
    min_upvotes = int(src.get("min_upvotes", 15))
    data = http.get("https://huggingface.co/api/daily_papers?limit=60", timeout=20).json()
    items = []
    for row in data:
        paper = row.get("paper") or row
        upvotes = int(paper.get("upvotes") or 0)
        if upvotes < min_upvotes or not paper.get("id"):
            continue
        published = None
        stamp = row.get("publishedAt") or paper.get("submittedOnDailyAt") or paper.get("publishedAt")
        if stamp:
            try:
                published = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                published = None
        if published and published < since - timedelta(days=2):
            continue
        item = NewsItem(
            title=row.get("title") or paper.get("title", ""),
            url=f"https://huggingface.co/papers/{paper['id']}",
            source="Hugging Face Papers", published=published,
            summary=clean_html(row.get("summary") or paper.get("summary") or "", 900),
            signal=f"Hugging Face: {upvotes} رأی", signal_value=upvotes, lang="en",
            discussion_url=paper.get("githubRepo") or paper.get("projectPage") or "")
        if row.get("thumbnail"):
            item.add_media(Media("image", row["thumbnail"], "hf"))
        items.append(item)
    items.sort(key=lambda i: i.signal_value, reverse=True)
    return items[: src.get("max_items", 15)]


def collect_page(src: dict, since: datetime) -> list[NewsItem]:
    """News pages without RSS (e.g. Anthropic): every link matching `link_pattern` is a post.
    Pages carry no dates, so freshness comes from 'not seen before' in the caller."""
    html, final_url = http.get_text(src["url"], timeout=20)
    pattern = re.compile(src["link_pattern"])
    items, seen = [], set()
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        url = urljoin(final_url, a["href"]).split("#")[0].split("?")[0]
        if url in seen or not pattern.search(url):
            continue
        seen.add(url)
        title = " ".join(a.get_text(" ", strip=True).split())
        heading = a.find(["h1", "h2", "h3", "h4"])
        if heading:
            title = " ".join(heading.get_text(" ", strip=True).split())
        title = title or a.get("aria-label", "")
        if len(title) < 6:
            continue
        items.append(NewsItem(title=title[:220], url=url, source=src["name"],
                              lang=src.get("lang", "en")))
        if len(items) >= src.get("max_items", 15):
            break
    return items


def collect_hf_models(src: dict, since: datetime) -> list[NewsItem]:
    """Newest public models of watched Hugging Face organisations. Labs often upload weights
    shortly before (or instead of) a blog post, so this catches open releases early."""
    per_org = int(src.get("per_org", 8))
    max_age = datetime.now(timezone.utc) - timedelta(days=int(src.get("max_age_days", 7)))

    def org_models(org: str) -> list[NewsItem]:
        url = (f"https://huggingface.co/api/models?author={org}&sort=lastModified"
               f"&direction=-1&limit={per_org}")
        out = []
        for m in http.get(url, timeout=15).json():
            created = None
            if m.get("createdAt"):
                try:
                    created = datetime.fromisoformat(m["createdAt"].replace("Z", "+00:00"))
                except ValueError:
                    created = None
            if m.get("private") or (created and created < max_age):
                continue
            likes = int(m.get("likes") or 0)
            out.append(NewsItem(
                title=f"New Hugging Face model: {m['id']}",
                url=f"https://huggingface.co/{m['id']}", source=f"Hugging Face · {org}",
                published=created, lang="en", signal_value=likes,
                signal=f"Hugging Face: {likes} لایک، {m.get('downloads') or 0} دانلود",
                summary=(f"organisation: {org}; task: {m.get('pipeline_tag') or '?'}; "
                         f"library: {m.get('library_name') or '?'}; created: {m.get('createdAt')}")))
        return out

    items: list[NewsItem] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for got in pool.map(lambda o: _soft(org_models, o), src.get("orgs", [])):
            items.extend(got)
    return items


def _soft(fn, arg):
    try:
        return fn(arg)
    except Exception as exc:  # noqa: BLE001
        log.info("%s(%s) failed: %s", fn.__name__, arg, exc)
        return []


COLLECTORS = {
    "rss": collect_rss,
    "hackernews": collect_hackernews,
    "hf_papers": collect_hf_papers,
    "page": collect_page,
    "hf_models": collect_hf_models,
}


def collect_all(sources: list[dict], since: datetime, workers: int = 8):
    """Run every enabled source in parallel. Returns (items, failures)."""
    enabled = [s for s in sources if s.get("enabled", True)]
    items: list[NewsItem] = []
    failures: list[str] = []

    def run(src):
        fn = COLLECTORS.get(src.get("type", "rss"))
        if fn is None:
            raise ValueError(f"unknown source type {src.get('type')}")
        src_since = since
        if src.get("window_hours"):
            src_since = datetime.now(timezone.utc) - timedelta(hours=float(src["window_hours"]))
        got = fn(src, src_since)
        for item in got:
            item.origin = src["name"]
        return got

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, s): s for s in enabled}
        for future, src in futures.items():
            try:
                got = future.result()
                log.info("%-28s %3d items", src["name"], len(got))
                items.extend(got)
            except Exception as exc:  # noqa: BLE001
                log.warning("source failed: %s (%s)", src["name"], exc)
                failures.append(src["name"])
    return items, failures
