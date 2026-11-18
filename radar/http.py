"""One shared HTTP session with a browser-like user agent and sane timeouts."""
from __future__ import annotations

import logging
import threading
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

BOT_NAME = "ai-news-radar"
USER_AGENT = f"Mozilla/5.0 (compatible; {BOT_NAME}/1.0; personal news reader)"
MAX_BYTES = 4 * 1024 * 1024

_session: requests.Session | None = None
_robots: dict[str, RobotFileParser | None] = {}
_robots_lock = threading.Lock()


def allowed_by_robots(url: str) -> bool:
    """Respect robots.txt for article pages. Unreachable robots.txt means allowed."""
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    with _robots_lock:
        if base not in _robots:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                resp = session().get(base + "/robots.txt", timeout=8)
                if resp.status_code == 200:
                    parser.parse(resp.text.splitlines())
                else:
                    parser = None
            except requests.RequestException:
                parser = None
            _robots[base] = parser
        parser = _robots[base]
    return parser is None or parser.can_fetch(BOT_NAME, url)


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9,*;q=0.5",
        })
        retry = Retry(total=2, backoff_factor=1.5, status_forcelist=(502, 503, 504),
                      allowed_methods=("GET",))
        s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
        s.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
        _session = s
    return _session


def get(url: str, timeout: float = 15, **kwargs) -> requests.Response:
    resp = session().get(url, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp


def get_text(url: str, timeout: float = 15) -> tuple[str, str]:
    """Fetch an HTML page. Returns (html, final_url). Large bodies are truncated."""
    if not allowed_by_robots(url):
        raise PermissionError("blocked by robots.txt")
    resp = session().get(url, timeout=timeout, stream=True, allow_redirects=True)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if ctype and "html" not in ctype and "xml" not in ctype:
        resp.close()
        raise ValueError(f"not an HTML page ({ctype})")
    chunks, size = [], 0
    for chunk in resp.iter_content(64 * 1024):
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_BYTES:
            break
    resp.close()
    raw = b"".join(chunks)
    encoding = resp.encoding if resp.encoding and resp.encoding.lower() != "iso-8859-1" else None
    try:
        html = raw.decode(encoding or resp.apparent_encoding or "utf-8", errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")
    return html, resp.url
