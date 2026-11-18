"""Prompts for the two editor agents. Instructions are in English (models follow them
more reliably); every user-facing field is requested in natural Persian."""
from __future__ import annotations

import json

CATEGORIES = [
    "model_release", "product_launch", "research", "open_source", "business_funding",
    "policy_regulation", "hardware_chips", "tools_for_creators", "safety_ethics",
    "big_tech", "other_tech",
]


def editor_system(profile: dict) -> str:
    return f"""You are the senior news editor and content strategist for a Persian-language
social media page about AI and technology.

About the page:
- Channel: {profile.get('channel', 'Instagram page')}
- Audience: {profile.get('audience', 'Persian-speaking people interested in AI and tech')}
- Content formats the creator makes: {', '.join(profile.get('formats', ['ریلز', 'کاروسل', 'استوری', 'پست']))}
- Extra notes from the creator: {profile.get('notes', '-')}

Rules you always follow:
- Be faithful to the sources. Never invent facts, numbers, quotes, dates or prices that are not
  in the provided text. If something is a rumour, leak or unconfirmed, say so.
- Persian output must be natural, fluent, modern Persian written for social media, not a
  literal machine translation. On first mention keep important names in English in
  parentheses, e.g. «اوپن‌ای‌آی (OpenAI)», «جمینای (Gemini)».
- Sources may be in any language; read them all and always answer in Persian.
- Reply with JSON only, matching the requested schema exactly."""


def triage_prompt(items: list[dict], previously_reported: list[str]) -> str:
    previous = "\n".join(f"- {t}" for t in previously_reported) or "- (none)"
    return f"""Today's raw news items are below (JSON lines). Score every item.

importance (1-10): how significant this is for the AI / tech world.
  10 = major frontier model launch, huge acquisition, landmark regulation.
  7-8 = notable launch or result people in tech will talk about this week.
  4-6 = useful but niche. 1-3 = minor, promotional, listicle, deal/discount, SEO filler.
worthiness (1-10): how good this is as the basis for a piece of content for THIS page.
  High when it is surprising, visual or demo-able, practical (a tool people can try),
  involves big names, sparks debate, or affects ordinary users' lives and jobs, and can be
  explained clearly in under 60 seconds. Low for dry corporate or very technical items
  with no hook.
viral (1-10): how likely this is to blow up and be talked about everywhere in the next day
  (big names, surprising, emotional, controversial, demo-able; strong engagement signals such as
  many Hacker News points or many outlets covering it push this up).
category: one of {json.dumps(CATEGORIES)}
story_key: a short English kebab-case slug for the underlying event. Items about the same
  event MUST share the same story_key.
topic_key: a short English kebab-case slug for the broader ongoing saga or main subject the
  event belongs to (e.g. "nvidia-leadership-crisis", "gpt-6-launch"). Different events in the
  same saga (follow-ups, reactions, investigations) share one topic_key.
already_reported: true if the item is the same story as one of the previously reported
  stories below AND brings no significant new development.

Previously reported in the last few days:
{previous}

Only return entries for items with importance >= 4. For items with importance >= 6 also give
title_fa (a punchy Persian headline) and one_liner_fa (one Persian sentence summarising the news).

Schema:
{{"items": [{{"id": "...", "importance": 0, "worthiness": 0, "viral": 0, "category": "...",
  "story_key": "...", "topic_key": "...", "already_reported": false, "title_fa": "...", "one_liner_fa": "..."}}]}}

Items:
{chr(10).join(json.dumps(i, ensure_ascii=False) for i in items)}"""


def deep_prompt(items: list[dict], ideas_per_item: int = 2) -> str:
    return f"""These are today's top stories selected for content. For each one, read the article
text and produce a Persian brief the creator can work from directly.

For every item:
- title_fa: a strong Persian headline.
- summary_fa: 3-5 sentences, an accurate Persian translation-summary of what happened.
- key_points_fa: 4-7 short Persian bullet points with the concrete facts (numbers, names,
  dates, availability, price, how to access) from the text.
- why_it_matters_fa: 1-2 sentences on why the audience should care.
- content_ideas: {ideas_per_item} ideas, each with:
    format (one of the page's formats), title_fa, hook_fa (the exact first line / first
    3 seconds), angle_fa (the take or storyline), outline_fa (3-5 beats in order),
    visual_tip_fa (which visuals to use; mention the article's images/videos listed in
    "media" when they help).
- hashtags: 5-8 hashtags, mixing Persian and English.
- caution_fa: empty string, or a short warning if the story is a rumour, unverified,
  sponsored, or easy to misrepresent.

Also write daily_brief_fa: 2-3 Persian sentences about the big picture of the news in this
batch (the page gets several updates a day; this batch covers the hours since the last one).

Schema:
{{"daily_brief_fa": "...", "items": [{{"id": "...", "title_fa": "...", "summary_fa": "...",
  "key_points_fa": ["..."], "why_it_matters_fa": "...",
  "content_ideas": [{{"format": "...", "title_fa": "...", "hook_fa": "...", "angle_fa": "...",
    "outline_fa": ["..."], "visual_tip_fa": "..."}}],
  "hashtags": ["..."], "caution_fa": ""}}]}}

Stories:
{chr(10).join(json.dumps(i, ensure_ascii=False) for i in items)}"""


def alert_prompt(items: list[dict], previous: list[str]) -> str:
    previous_text = "\n".join(f"- {t}" for t in previous) or "- (none)"
    return f"""You watch AI and tech news in real time for a Persian creator who wants to be the
FIRST to cover big news. Decide for each candidate below whether it deserves an instant alert NOW.

alert_score (1-10):
  9-10: a new frontier model or major model version from a top lab (OpenAI, Google/DeepMind,
        Anthropic, Meta, xAI, DeepSeek, Alibaba/Qwen, Mistral, Moonshot ...), a launch millions of
        people will use, a historic industry event (mega acquisition, CEO change at a top AI
        company, landmark ruling or ban), or a story that is clearly going viral.
  7-8: important and timely, but not "drop everything".
  1-6: routine news, minor updates, opinion pieces, small funding rounds.
For Hugging Face uploads only a genuinely new model family or major version scores high;
fine-tunes, quantised copies and small updates score low.
already_covered: true if the same story is already in the list below with no major new development.

Already alerted or reported:
{previous_text}

For items with alert_score >= 7 also give:
  title_fa (punchy Persian headline), summary_fa (2-3 Persian sentences, only facts from the
  candidate), why_fa (1 Persian sentence: why it is big), is_model_release (true/false),
  quick_idea: {{"format": one of the page's formats, "hook_fa": the first line of a fast video,
  "angle_fa": the take in one sentence}}.

Schema:
{{"items": [{{"id": "...", "alert_score": 0, "already_covered": false, "is_model_release": false,
  "title_fa": "...", "summary_fa": "...", "why_fa": "...",
  "quick_idea": {{"format": "...", "hook_fa": "...", "angle_fa": "..."}}}}]}}

Candidates:
{chr(10).join(json.dumps(i, ensure_ascii=False) for i in items)}"""
