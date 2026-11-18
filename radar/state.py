"""Small JSON state kept in the repo so stories are not repeated on later days."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

SEEN_DAYS = 14
REPORTED_DAYS = 4


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        data = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        self.seen: dict[str, str] = data.get("seen", {})
        self.reported: list[dict] = data.get("reported", [])
        self.telegram_chat_id: str = str(data.get("telegram_chat_id") or "")

    def is_seen(self, canonical: str) -> bool:
        return canonical in self.seen

    def mark_seen(self, canonicals, today: date) -> None:
        for c in canonicals:
            self.seen.setdefault(c, today.isoformat())

    def recent_titles(self) -> list[str]:
        out = []
        for r in self.reported[-60:]:
            title = r.get("title", "")
            if r.get("title_fa"):
                title = f"{title} / {r['title_fa']}"
            out.append(title)
        return out

    def add_reported(self, items, today: date) -> None:
        for item in items:
            self.reported.append({"date": today.isoformat(), "title": item.title,
                                  "title_fa": item.title_fa, "url": item.canonical})

    def prune(self, today: date) -> None:
        seen_cut = (today - timedelta(days=SEEN_DAYS)).isoformat()
        rep_cut = (today - timedelta(days=REPORTED_DAYS)).isoformat()
        self.seen = {k: v for k, v in self.seen.items() if v >= seen_cut}
        self.reported = [r for r in self.reported if r.get("date", "") >= rep_cut]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"seen": self.seen, "reported": self.reported}
        if self.telegram_chat_id:
            data["telegram_chat_id"] = self.telegram_chat_id
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
