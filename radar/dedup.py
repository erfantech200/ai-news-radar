"""Merge duplicate stories (same URL, or near-identical headlines from different outlets)."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .models import NewsItem

WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def norm_title(title: str) -> str:
    title = WORD_RE.sub(" ", title.lower())
    return " ".join(title.split())


def similar(a: str, b: str, threshold: float = 0.82) -> bool:
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


def _merge(keep: NewsItem, other: NewsItem, other_outlet: bool = True) -> None:
    if other_outlet and other.source != keep.source and other.source not in keep.also_covered_by:
        keep.also_covered_by.append(other.source)
    for s in other.also_covered_by:
        if s != keep.source and s not in keep.also_covered_by:
            keep.also_covered_by.append(s)
    if len(other.summary) > len(keep.summary):
        keep.summary = other.summary
    if other.signal_value > keep.signal_value:
        keep.signal, keep.signal_value = other.signal, other.signal_value
    if other.discussion_url and not keep.discussion_url:
        keep.discussion_url = other.discussion_url
    for m in other.media:
        keep.add_media(m)


def dedupe(items: list[NewsItem]) -> list[NewsItem]:
    by_url: dict[str, NewsItem] = {}
    for item in items:
        key = item.canonical
        if key in by_url:
            _merge(by_url[key], item, other_outlet=False)  # same article, not another outlet
        else:
            by_url[key] = item
    unique: list[NewsItem] = []
    for item in by_url.values():
        for kept in unique:
            if similar(kept.title, item.title):
                _merge(kept, item)
                break
        else:
            unique.append(item)
    return unique
