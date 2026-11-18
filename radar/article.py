"""Fetch an article page and pull out its readable text, language and media."""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from . import http
from .media import extract_media, youtube_id, youtube_thumbnail
from .models import Media, NewsItem

log = logging.getLogger(__name__)

NOISE_CLASS_RE = re.compile(r"(newsletter|subscribe|related|promo|advert|share|social|"
                            r"comment|sidebar|cookie|paywall|recommend)", re.I)


def extract_text(html: str, max_chars: int = 6000) -> tuple[str, str]:
    """Return (body_text, page_language)."""
    soup = BeautifulSoup(html, "lxml")
    lang = ""
    if soup.html and soup.html.get("lang"):
        lang = soup.html["lang"].split("-")[0].lower()
    for tag in soup.find_all(["script", "style", "noscript", "nav", "footer", "aside", "form", "svg"]):
        tag.decompose()
    for tag in soup.find_all(class_=NOISE_CLASS_RE):
        if tag.name not in ("html", "body", "article", "main"):
            tag.decompose()

    root = soup.find("article") or soup.find("main") or soup.body or soup
    parts: list[str] = []
    for el in root.find_all(["h2", "h3", "p", "li", "blockquote"]):
        text = " ".join(el.get_text(" ", strip=True).split())
        if el.name == "p" and len(text) < 40:
            continue
        if el.name == "li" and len(text) < 25:
            continue
        if text and (not parts or parts[-1] != text):
            parts.append(text)
    body = "\n".join(parts)
    if len(body) < 300:  # fall back to meta description when the body is thin
        desc = soup.find("meta", attrs={"property": "og:description"}) or \
            soup.find("meta", attrs={"name": "description"})
        if desc and desc.get("content"):
            body = (desc["content"] + "\n" + body).strip()
    return body[:max_chars], lang


def enrich(item: NewsItem, max_chars: int = 6000) -> NewsItem:
    """Download the article page; fill item.text, item.lang and item.media in place."""
    if youtube_id(item.url):
        item.add_media(Media("embed", item.url, "youtube"))
        thumb = youtube_thumbnail(item.url)
        if thumb:
            item.add_media(Media("image", thumb, "youtube"))
    try:
        html, final_url = http.get_text(item.url)
    except Exception as exc:  # noqa: BLE001 - one bad page must never stop the run
        log.info("could not fetch %s: %s", item.url, exc)
        if not item.text:
            item.text = item.summary
        return item
    text, lang = extract_text(html, max_chars)
    item.text = text or item.summary
    if lang and not item.lang:
        item.lang = lang
    page_media = extract_media(html, final_url)
    # Page media goes first (publisher's hero image beats an RSS thumbnail).
    existing = item.media
    item.media = []
    for m in page_media + existing:
        item.add_media(m)
    return item
