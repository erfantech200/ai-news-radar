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
category: one of {json.dumps(CATEGORIES)}
story_key: a short English kebab-case slug for the underlying event. Items about the same
  event MUST share the same story_key.
already_reported: true if the item is the same story as one of the previously reported
  stories below AND brings no significant new development.

Previously reported in the last few days:
{previous}

Only return entries for items with importance >= 4. For items with importance >= 6 also give
title_fa (a punchy Persian headline) and one_liner_fa (one Persian sentence summarising the news).

Schema:
{{"items": [{{"id": "...", "importance": 0, "worthiness": 0, "category": "...",
  "story_key": "...", "already_reported": false, "title_fa": "...", "one_liner_fa": "..."}}]}}

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

Also write daily_brief_fa: 2-3 Persian sentences about the big picture of today's news.

Schema:
{{"daily_brief_fa": "...", "items": [{{"id": "...", "title_fa": "...", "summary_fa": "...",
  "key_points_fa": ["..."], "why_it_matters_fa": "...",
  "content_ideas": [{{"format": "...", "title_fa": "...", "hook_fa": "...", "angle_fa": "...",
    "outline_fa": ["..."], "visual_tip_fa": "..."}}],
  "hashtags": ["..."], "caution_fa": ""}}]}}

Stories:
{chr(10).join(json.dumps(i, ensure_ascii=False) for i in items)}"""
