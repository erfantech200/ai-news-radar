"""Turn the day's results into Telegram HTML messages and a Markdown archive page."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from html import escape
from urllib.parse import urlsplit

from .models import Media, NewsItem, clean_url
from .persian import category_name, fa_digits, jalali_label, language_name

TG_LIMIT = 4000


@dataclass
class DayResult:
    day: date
    content_items: list[NewsItem]
    other_items: list[NewsItem]
    daily_brief: str = ""
    scanned: int = 0
    source_count: int = 0
    failed_sources: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    archive_url: str = ""
    degraded: str = ""        # set when the LLM was unavailable


def h(text) -> str:
    return escape(str(text or ""), quote=False)


def ha(url: str) -> str:
    return escape(url or "", quote=True)


def _embed_label(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    for key, label in (("youtu", "یوتیوب"), ("x.com", "پست X"), ("twitter", "پست X"),
                       ("vimeo", "ویمئو"), ("tiktok", "تیک‌تاک"), ("instagram", "اینستاگرام"),
                       ("loom", "لوم"), ("bsky", "بلواسکای")):
        if key in host:
            return label
    return "ویدیو"


def media_groups(item: NewsItem) -> tuple[list[Media], list[Media], list[Media]]:
    images = [m for m in item.media if m.kind == "image"]
    videos = [m for m in item.media if m.kind == "video"]
    embeds = [m for m in item.media if m.kind == "embed"]
    return images, videos, embeds


def _published_label(item: NewsItem) -> str:
    bits = []
    if item.lang and item.lang != "fa":
        bits.append(f"زبان اصلی: {language_name(item.lang)}")
    return " · ".join(bits)


# --------------------------------------------------------------------- Telegram
def tg_header(r: DayResult) -> str:
    lines = [f"📡 <b>رادار هوش مصنوعی</b>",
             f"🗓 {jalali_label(r.day)} ({r.day.isoformat()})", ""]
    if r.degraded:
        lines += [f"⚠️ {h(r.degraded)}", ""]
    if r.daily_brief:
        lines += [f"🧭 <b>تصویر کلی امروز:</b> {h(r.daily_brief)}", ""]
    lines.append(f"📊 {fa_digits(r.scanned)} خبر از {fa_digits(r.source_count)} منبع بررسی شد · "
                 f"{fa_digits(len(r.content_items))} سوژهٔ محتوا · "
                 f"{fa_digits(len(r.other_items))} خبر مهم دیگه")
    if r.failed_sources:
        lines.append(f"🔌 منابعی که امروز جواب ندادن: {h('، '.join(r.failed_sources))}")
    return "\n".join(lines)


def tg_caption(item: NewsItem, n: int) -> str:
    title = item.deep.get("title_fa") or item.title_fa or item.title
    return f"<b>{fa_digits(n)}. {h(title)}</b>\n📰 {h(item.source)}"[:1000]


def tg_item(item: NewsItem, n: int) -> str:
    d = item.deep
    title = d.get("title_fa") or item.title_fa or item.title
    out = [f"🔥 <b>{fa_digits(n)}. {h(title)}</b>"]
    meta = []
    if item.importance:
        meta.append(f"⭐ اهمیت {fa_digits(item.importance)}/۱۰")
    if item.worthiness:
        meta.append(f"🎯 ارزش محتوا {fa_digits(item.worthiness)}/۱۰")
    if item.category:
        meta.append(f"🏷 {h(category_name(item.category))}")
    if meta:
        out.append(" · ".join(meta))
    if not d:  # no deep brief (LLM unavailable): show what we have
        out.append("")
        out.append(h(item.one_liner_fa or item.summary[:400]))
    if d.get("summary_fa"):
        out += ["", f"📝 {h(d['summary_fa'])}"]
    if d.get("key_points_fa"):
        out += ["", "<b>نکات کلیدی:</b>"] + [f"• {h(p)}" for p in d["key_points_fa"]]
    if d.get("why_it_matters_fa"):
        out += ["", f"💡 <b>چرا مهمه:</b> {h(d['why_it_matters_fa'])}"]
    if d.get("caution_fa"):
        out += ["", f"⚠️ {h(d['caution_fa'])}"]

    ideas = d.get("content_ideas") or []
    for i, idea in enumerate(ideas, 1):
        block = [f"🎬 <b>ایدهٔ {fa_digits(i)} [{h(idea.get('format', ''))}]: {h(idea.get('title_fa', ''))}</b>"]
        if idea.get("hook_fa"):
            block.append(f"🪝 هوک: «{h(idea['hook_fa'])}»")
        if idea.get("angle_fa"):
            block.append(f"🧭 زاویه: {h(idea['angle_fa'])}")
        for j, beat in enumerate(idea.get("outline_fa") or [], 1):
            block.append(f"   {fa_digits(j)}) {h(beat)}")
        if idea.get("visual_tip_fa"):
            block.append(f"🎨 ویژوال: {h(idea['visual_tip_fa'])}")
        out += ["", "<blockquote expandable>" + "\n".join(block) + "</blockquote>"]

    images, videos, embeds = media_groups(item)
    if images or videos or embeds:
        out += ["", "📥 <b>مدیا برای دانلود:</b>"]
        if images:
            out.append("🖼 " + " · ".join(f'<a href="{ha(m.url)}">عکس {fa_digits(k)}</a>'
                                         for k, m in enumerate(images[:8], 1)))
        if videos:
            out.append("🎞 " + " · ".join(f'<a href="{ha(m.url)}">ویدیو {fa_digits(k)}</a>'
                                         for k, m in enumerate(videos[:4], 1)))
        if embeds:
            out.append("▶️ " + " · ".join(f'<a href="{ha(m.url)}">{_embed_label(m.url)}</a>'
                                         for m in embeds[:4]))

    out += ["", f'🔗 <b>منبع اصلی:</b> <a href="{ha(clean_url(item.url))}">{h(item.source)}</a>']
    extra = _published_label(item)
    if extra:
        out[-1] += f" ({extra})"
    if item.discussion_url:
        label = "بحث در Hacker News" if "ycombinator" in item.discussion_url else "لینک مرتبط"
        out.append(f'💬 <a href="{ha(item.discussion_url)}">{label}</a>')
    if item.also_covered_by:
        out.append(f"📰 رسانه‌های دیگه: {h('، '.join(item.also_covered_by[:6]))}")
    if item.signal:
        out.append(f"📊 {h(fa_digits(item.signal))}")
    if d.get("hashtags"):
        out += ["", h(" ".join(t if t.startswith("#") else f"#{t}" for t in d["hashtags"]))]
    return "\n".join(out)


def tg_others(items: list[NewsItem]) -> str:
    if not items:
        return ""
    lines = ["📌 <b>خبرهای مهم دیگه</b>"]
    for n, item in enumerate(items, 1):
        title = item.title_fa or item.title
        lines += ["", f"<b>{fa_digits(n)}. {h(title)}</b>"]
        if item.one_liner_fa:
            lines.append(h(item.one_liner_fa))
        tail = f'🔗 <a href="{ha(clean_url(item.url))}">{h(item.source)}</a>'
        if item.importance:
            tail += f" · ⭐ {fa_digits(item.importance)}/۱۰"
        lines.append(tail)
    return "\n".join(lines)


def tg_footer(r: DayResult) -> str:
    lines = []
    if r.archive_url:
        lines.append(f'📁 <a href="{ha(r.archive_url)}">گزارش کامل امروز با همهٔ لینک‌ها</a>')
    if r.models:
        lines.append(f"🤖 {h(', '.join(dict.fromkeys(r.models)))}")
    return "\n".join(lines)


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split on blank lines so HTML tags (which never span blocks) stay balanced."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:  # a single giant block: hard split on lines
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        current = block
    if current:
        parts.append(current)
    return parts


# --------------------------------------------------------------------- Markdown
def markdown(r: DayResult) -> str:
    md = [f"# رادار هوش مصنوعی — {jalali_label(r.day)} ({r.day.isoformat()})", ""]
    if r.degraded:
        md += [f"> ⚠️ {r.degraded}", ""]
    if r.daily_brief:
        md += [f"**تصویر کلی امروز:** {r.daily_brief}", ""]
    md += [f"{fa_digits(r.scanned)} خبر از {fa_digits(r.source_count)} منبع بررسی شد.", ""]

    md += ["## سوژه‌های محتوا", ""]
    for n, item in enumerate(r.content_items, 1):
        d = item.deep
        md += [f"### {fa_digits(n)}. {d.get('title_fa') or item.title_fa or item.title}", "",
               f"**عنوان اصلی:** {item.title}  ",
               f"**منبع:** [{item.source}]({clean_url(item.url)})"
               + (f" · {language_name(item.lang)}" if item.lang else "") + "  ",
               f"**اهمیت:** {item.importance}/10 · **ارزش محتوا:** {item.worthiness}/10 · "
               f"**دسته:** {category_name(item.category)}", ""]
        if d.get("summary_fa"):
            md += [d["summary_fa"], ""]
        for p in d.get("key_points_fa") or []:
            md.append(f"- {p}")
        if d.get("why_it_matters_fa"):
            md += ["", f"**چرا مهمه:** {d['why_it_matters_fa']}"]
        if d.get("caution_fa"):
            md += ["", f"> ⚠️ {d['caution_fa']}"]
        for i, idea in enumerate(d.get("content_ideas") or [], 1):
            md += ["", f"**ایدهٔ {fa_digits(i)} [{idea.get('format', '')}]: {idea.get('title_fa', '')}**", ""]
            if idea.get("hook_fa"):
                md.append(f"- هوک: «{idea['hook_fa']}»")
            if idea.get("angle_fa"):
                md.append(f"- زاویه: {idea['angle_fa']}")
            for j, beat in enumerate(idea.get("outline_fa") or [], 1):
                md.append(f"  {j}. {beat}")
            if idea.get("visual_tip_fa"):
                md.append(f"- ویژوال: {idea['visual_tip_fa']}")
        images, videos, embeds = media_groups(item)
        if images or videos or embeds:
            md += ["", "**مدیا:**", ""]
            md += [f"- 🖼 [عکس {k}]({m.url})" for k, m in enumerate(images, 1)]
            md += [f"- 🎞 [ویدیو {k}]({m.url})" for k, m in enumerate(videos, 1)]
            md += [f"- ▶️ [{_embed_label(m.url)}]({m.url})" for m in embeds]
            if images:
                md += ["", f"![]({images[0].url})"]
        if item.discussion_url:
            md += ["", f"[بحث / لینک مرتبط]({item.discussion_url})"]
        if item.also_covered_by:
            md += ["", "رسانه‌های دیگه: " + "، ".join(item.also_covered_by)]
        if d.get("hashtags"):
            md += ["", " ".join(t if t.startswith("#") else f"#{t}" for t in d["hashtags"])]
        md += ["", "---", ""]

    if r.other_items:
        md += ["## خبرهای مهم دیگه", ""]
        for n, item in enumerate(r.other_items, 1):
            md.append(f"{n}. **{item.title_fa or item.title}** — {item.one_liner_fa} "
                      f"([{item.source}]({clean_url(item.url)}))")
        md.append("")
    if r.failed_sources:
        md += [f"_منابع ناموفق: {'، '.join(r.failed_sources)}_", ""]
    if r.models:
        md += [f"_مدل: {', '.join(dict.fromkeys(r.models))}_", ""]
    return "\n".join(md)
