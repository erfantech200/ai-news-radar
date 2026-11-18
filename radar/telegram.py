"""Minimal Telegram Bot API sender (HTML messages, photos, albums, videos)."""
from __future__ import annotations

import logging
import re
import time

import requests

from .models import NewsItem
from .render import media_groups, split_message, tg_caption

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str = "", pause: float = 1.1):
        self.token = token
        self.chat_id = chat_id
        self.pause = pause
        self.sent = 0

    def call(self, method: str, payload: dict) -> dict:
        for _ in range(4):
            resp = requests.post(API.format(token=self.token, method=method), json=payload,
                                 timeout=60)
            data = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else {}
            if resp.status_code == 429:
                wait = data.get("parameters", {}).get("retry_after", 5)
                time.sleep(wait + 1)
                continue
            if not data.get("ok"):
                raise TelegramError(f"{method}: {data.get('description') or resp.text[:200]}")
            time.sleep(self.pause)
            self.sent += 1
            return data["result"]
        raise TelegramError(f"{method}: rate limited too many times")

    def discover_chat_id(self) -> str:
        """Find the chat of whoever last messaged the bot (works within ~24h of that message)."""
        updates = self.call("getUpdates", {"limit": 50, "timeout": 0})
        for upd in reversed(updates):
            msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member")
            if msg and msg.get("chat", {}).get("id"):
                return str(msg["chat"]["id"])
        # Explain which bot this token belongs to, so a token/bot mix-up is obvious in the log.
        me = self.call("getMe", {}) or {}
        hook = self.call("getWebhookInfo", {}) or {}
        raise TelegramError(
            f"no chat found for bot @{me.get('username', '?')} "
            f"(webhook: {hook.get('url') or 'none'}, pending updates: "
            f"{hook.get('pending_update_count', '?')}). Send any message to this bot, then re-run")

    # ------------------------------------------------------------------ senders
    def text(self, html: str) -> None:
        for part in split_message(html):
            payload = {"chat_id": self.chat_id, "text": part, "parse_mode": "HTML",
                       "link_preview_options": {"is_disabled": True}}
            try:
                self.call("sendMessage", payload)
            except TelegramError as exc:
                if "parse" not in str(exc).lower() and "tag" not in str(exc).lower():
                    raise
                log.warning("HTML rejected, sending plain text: %s", exc)
                plain = re.sub(r"<[^>]+>", "", part)
                self.call("sendMessage", {"chat_id": self.chat_id, "text": plain,
                                          "link_preview_options": {"is_disabled": True}})

    def item_media(self, item: NewsItem, n: int, max_images: int = 6, max_videos: int = 2) -> None:
        """Send the story's pictures (as an album) and direct video files. Never raises."""
        images, videos, _ = media_groups(item)
        caption = tg_caption(item, n)
        images = images[:max_images]
        sent_photo = False
        for _ in range(3):  # Telegram names the picture it couldn't fetch; drop it and retry
            if len(images) < 2:
                break
            album = [{"type": "photo", "media": m.url} for m in images]
            album[0].update(caption=caption, parse_mode="HTML")
            try:
                self.call("sendMediaGroup", {"chat_id": self.chat_id, "media": album})
                sent_photo = True
                break
            except TelegramError as exc:
                log.info("album failed for %s: %s", item.url, exc)
                bad = re.search(r"message #(\d+)", str(exc))
                if not bad or not 1 <= int(bad.group(1)) <= len(images):
                    break
                images = images[:int(bad.group(1)) - 1] + images[int(bad.group(1)):]
        if not sent_photo:
            for m in images[:3]:
                try:
                    self.call("sendPhoto", {"chat_id": self.chat_id, "photo": m.url,
                                            "caption": caption, "parse_mode": "HTML"})
                    break
                except TelegramError as exc:
                    log.info("photo failed %s: %s", m.url, exc)
        for m in videos[:max_videos]:
            try:
                self.call("sendVideo", {"chat_id": self.chat_id, "video": m.url,
                                        "caption": caption, "parse_mode": "HTML",
                                        "supports_streaming": True})
            except TelegramError as exc:
                log.info("video failed %s: %s", m.url, exc)
