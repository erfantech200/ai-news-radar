"""Find downloadable media (images, video files, video embeds) in an article page."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .models import Media

VIDEO_FILE_RE = re.compile(r"\.(mp4|webm|mov|m4v|m3u8)(\?|$)", re.I)
IMAGE_FILE_RE = re.compile(r"\.(jpe?g|png|webp|avif|gif)(\?|$)", re.I)
JUNK_IMAGE_RE = re.compile(
    r"(logo|icon|avatar|sprite|pixel|spacer|blank|placeholder|badge|emoji|gravatar|"
    r"tracking|1x1|favicon|author|headshot|profile|button|banner-ad|/ads?/)", re.I)
EMBED_HOSTS = (
    "youtube.com", "youtube-nocookie.com", "youtu.be", "vimeo.com", "player.vimeo.com",
    "twitter.com", "x.com", "platform.twitter.com", "tiktok.com", "instagram.com",
    "streamable.com", "loom.com", "dailymotion.com", "bsky.app",
)
MAX_IMAGES = 6


def youtube_id(url: str) -> str | None:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if "youtu.be" in host:
        return parts.path.strip("/").split("/")[0] or None
    if "youtube" in host:
        if parts.path.startswith(("/embed/", "/shorts/", "/live/", "/v/")):
            return parts.path.split("/")[2] or None
        return parse_qs(parts.query).get("v", [None])[0]
    return None


def youtube_watch_url(url: str) -> str:
    vid = youtube_id(url)
    return f"https://www.youtube.com/watch?v={vid}" if vid else url


def youtube_thumbnail(url: str) -> str | None:
    vid = youtube_id(url)
    return f"https://img.youtube.com/vi/{vid}/maxresdefault.jpg" if vid else None


def _is_embed(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in EMBED_HOSTS)


def _normalise_embed(url: str) -> str:
    if youtube_id(url):
        return youtube_watch_url(url)
    return url


def _largest_from_srcset(srcset: str) -> str | None:
    best, best_w = None, -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1:
            m = re.match(r"(\d+)", bits[1])
            w = int(m.group(1)) if m else 0
        if w >= best_w:
            best, best_w = bits[0], w
    return best


def _small(tag) -> bool:
    for attr in ("width", "height"):
        val = tag.get(attr)
        if val:
            m = re.match(r"\s*(\d+)", str(val))
            if m and int(m.group(1)) < 300:
                return True
    return False


def extract_media(html: str, base_url: str) -> list[Media]:
    """Return media found in a page, best candidates first."""
    soup = BeautifulSoup(html, "lxml")
    found: list[Media] = []
    seen: set[str] = set()

    def add(kind: str, url: str | None, origin: str) -> None:
        if not url:
            return
        url = urljoin(base_url, url.strip())
        if not url.startswith("http"):
            return
        if kind == "embed":
            url = _normalise_embed(url)
        if kind == "image" and JUNK_IMAGE_RE.search(url):
            return
        if url in seen:
            return
        seen.add(url)
        found.append(Media(kind=kind, url=url, origin=origin))

    def meta(*names: str) -> list[str]:
        out = []
        for name in names:
            for tag in soup.find_all("meta", attrs={"property": name}) + \
                    soup.find_all("meta", attrs={"name": name}):
                if tag.get("content"):
                    out.append(tag["content"])
        return out

    # 1. Open Graph / Twitter cards: the publisher's own hero image and video.
    for url in meta("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"):
        add("image", url, "og")
    for url in meta("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
        add("video" if VIDEO_FILE_RE.search(url) else "embed", url, "og")
    for url in meta("twitter:player"):
        add("embed", url, "og")

    body = soup.find("article") or soup.find("main") or soup.body or soup

    # 2. Native <video> elements.
    for video in body.find_all("video"):
        add("video", video.get("src"), "article")
        for source in video.find_all("source"):
            add("video", source.get("src"), "article")
        if video.get("poster"):
            add("image", video["poster"], "article")

    # 3. Embedded players (YouTube, X, Vimeo, ...).
    for iframe in body.find_all("iframe"):
        src = iframe.get("src") or iframe.get("data-src")
        if src and _is_embed(urljoin(base_url, src)):
            add("embed", src, "article")
    for quote in body.select("blockquote.twitter-tweet, blockquote.instagram-media, blockquote.tiktok-embed"):
        for a in quote.find_all("a", href=True):
            if re.search(r"/(status|p|reel|video)/", a["href"]):
                add("embed", a["href"], "article")
                break
    for a in body.find_all("a", href=True):
        if youtube_id(a["href"]):
            add("embed", a["href"], "article")

    # 4. Inline images in the article body.
    images = 0
    for img in body.find_all("img"):
        if images >= MAX_IMAGES:
            break
        if _small(img):
            continue
        src = None
        for attr in ("data-src", "data-lazy-src", "data-original", "src"):
            if img.get(attr) and not img[attr].startswith("data:"):
                src = img[attr]
                break
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            src = _largest_from_srcset(srcset) or src
        if not src or src.lower().endswith(".svg"):
            continue
        before = len(found)
        add("image", src, "article")
        if len(found) > before:
            images += 1

    # YouTube embeds come with a free high-res thumbnail.
    for m in list(found):
        if m.kind == "embed" and youtube_id(m.url):
            add("image", youtube_thumbnail(m.url), "youtube")

    return found


def media_from_feed_entry(entry) -> list[Media]:
    """Media declared inside an RSS/Atom entry (media:content, enclosures, thumbnails)."""
    out: list[Media] = []
    for mc in entry.get("media_content", []) or []:
        url = mc.get("url")
        mtype = (mc.get("type") or mc.get("medium") or "").lower()
        if not url:
            continue
        if "video" in mtype or VIDEO_FILE_RE.search(url):
            out.append(Media("video", url, "rss"))
        elif "image" in mtype or IMAGE_FILE_RE.search(url) or not mtype:
            out.append(Media("image", url, "rss"))
    for th in entry.get("media_thumbnail", []) or []:
        if th.get("url"):
            out.append(Media("image", th["url"], "rss"))
    for enc in entry.get("enclosures", []) or []:
        url = enc.get("href") or enc.get("url")
        etype = (enc.get("type") or "").lower()
        if url and etype.startswith("image"):
            out.append(Media("image", url, "rss"))
        elif url and etype.startswith("video"):
            out.append(Media("video", url, "rss"))
    return [m for m in out if not JUNK_IMAGE_RE.search(m.url)]
