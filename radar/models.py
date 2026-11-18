"""Core data types shared by every stage of the pipeline."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "ref", "ref_src", "ref_url", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid",
    "cmpid", "taid", "guccounter", "smid", "source", "via",
}


def canonical_url(url: str) -> str:
    """Normalise a URL so the same article from different feeds compares equal."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") and host.count(".") >= 2:
        host = host[2:]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def clean_url(url: str) -> str:
    """Drop tracking parameters but otherwise keep the URL exactly as published."""
    parts = urlsplit(url.strip())
    if not parts.query:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid", "ref_src")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def short_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


@dataclass
class Media:
    kind: str          # "image" | "video" | "embed"
    url: str
    origin: str = ""   # where it was found: og, rss, article, youtube ...

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published: datetime | None = None
    summary: str = ""
    lang: str = ""
    signal: str = ""               # e.g. "HN 340 points", "HF 85 upvotes"
    signal_value: int = 0
    discussion_url: str = ""
    media: list[Media] = field(default_factory=list)
    also_covered_by: list[str] = field(default_factory=list)
    text: str = ""                  # extracted article body (filled for finalists)

    # Filled by the triage agent
    importance: int = 0
    worthiness: int = 0
    category: str = ""
    story_key: str = ""
    already_reported: bool = False
    title_fa: str = ""
    one_liner_fa: str = ""

    # Filled by the deep-dive agent
    deep: dict = field(default_factory=dict)

    @property
    def canonical(self) -> str:
        return canonical_url(self.url)

    @property
    def id(self) -> str:
        return short_id(self.canonical or self.title)

    def add_media(self, media: Media) -> None:
        if not media.url or media.url.startswith("data:"):
            return
        if any(m.url == media.url for m in self.media):
            return
        self.media.append(media)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.id
        d["published"] = self.published.isoformat() if self.published else None
        d.pop("text", None)
        return d
