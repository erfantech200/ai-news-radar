"""Fast local scoring for the desktop visualiser (no API key needed).

The real bot scores with Gemini; this keyword + engagement model is what the desktop app uses for
live headlines, so the show runs on any PC. Real Gemini scores appear when the bot is connected."""
from __future__ import annotations

import re

from radar.persian import category_name

CATEGORY_RULES = [
    ("model_release", r"\b(introduc\w*|launch\w*|releas\w*|unveil\w*|debut\w*|new)\b.*\b(model|gpt-?\d|gemini|claude|llama|"
                      r"grok|deepseek|qwen|mistral|kimi|glm|gemma|phi-?\d|sora|veo|imagen|weights)\b|"
                      r"\b(gpt-?\d[\w.]*|gemini \d|claude \w+ \d|llama \d|qwen\d|deepseek[- ]?v\d)\b"),
    ("open_source", r"open[- ]?source|open[- ]weights?|github|hugging ?face|new hugging face model"),
    ("business_funding", r"\b(raises?|funding|valuation|ipo|acquir\w*|acquisition|billion|million|revenue|earnings|s-1)\b"),
    ("policy_regulation", r"\b(law|regulat\w*|ban\w*|court|lawsuit|sues?|senate|congress|policy|ftc|sec|eu|antitrust)\b"),
    ("hardware_chips", r"\b(nvidia|chips?|gpus?|tpus?|amd|tsmc|semiconductor\w*|blackwell|rubin|datacenter|data center)\b"),
    ("safety_ethics", r"\b(safety|alignment|deepfakes?|misuse|bias|privacy|security|jailbreak\w*|harm\w*)\b"),
    ("research", r"\b(paper|research\w*|study|benchmark\w*|arxiv|scientists?|lab)\b"),
    ("tools_for_creators", r"\b(app|tool|feature|update|now available|free|video|image|voice|agent\w*|plugin|extension)\b"),
    ("big_tech", r"\b(google|apple|microsoft|meta|amazon|openai|anthropic|samsung|tesla)\b"),
]
ENTITIES = {
    "OpenAI": r"openai|chatgpt|gpt-?\d|sora|codex",
    "Google": r"google|gemini|deepmind|veo|gemma|android",
    "Anthropic": r"anthropic|claude",
    "Meta": r"\bmeta\b|llama|instagram|whatsapp",
    "Nvidia": r"nvidia|jensen",
    "Microsoft": r"microsoft|copilot|windows",
    "Apple": r"\bapple\b|iphone|siri|macos",
    "DeepSeek": r"deepseek",
    "Qwen": r"qwen|alibaba",
    "Mistral": r"mistral",
    "xAI": r"\bxai\b|grok|musk",
    "Waymo": r"waymo|robotaxi",
    "HuggingFace": r"hugging ?face",
}
BIG = re.compile("|".join(ENTITIES.values()), re.I)
DRAMA = re.compile(r"\b(ban\w*|lawsuit|sues?|fired|ousted|resign\w*|leak\w*|beats?|shock\w*|outage|"
                   r"billion|record|free|hack\w*|breach|exclusive|first|surpass\w*|kills?|replac\w*)\b", re.I)
AI_RE = re.compile(r"\b(ai|a\.i\.|llm|model|agent\w*|neural|gpt|gemini|claude|machine learning|chatbot)\b", re.I)
HASHTAG_FA = {
    "model_release": "#مدل_جدید", "open_source": "#متن_باز", "business_funding": "#کسب_و_کار",
    "policy_regulation": "#قانون_گذاری", "hardware_chips": "#سخت_افزار", "safety_ethics": "#ایمنی_هوش_مصنوعی",
    "research": "#پژوهش", "tools_for_creators": "#ابزار_کاربردی", "big_tech": "#غول_های_فناوری",
    "other_tech": "#تکنولوژی",
}
_RULES = [(code, re.compile(rx, re.I)) for code, rx in CATEGORY_RULES]
_ENT = {name: re.compile(rx, re.I) for name, rx in ENTITIES.items()}


def _numbers(signal: str) -> tuple[int, int]:
    nums = [int(n) for n in re.findall(r"\d+", signal or "")]
    return (nums[0] if nums else 0, nums[1] if len(nums) > 1 else 0)


def clamp(v: float) -> int:
    return int(max(1, min(10, round(v))))


def score(item: dict) -> dict:
    title = item.get("title", "")
    text = f"{title} {item.get('summary', '')[:200]}"
    category = next((code for code, rx in _RULES if rx.search(text)), "other_tech")
    entities = [name for name, rx in _ENT.items() if rx.search(text)]
    points, comments = _numbers(item.get("signal", ""))
    is_hn = "Hacker News" in (item.get("signal") or "")
    is_hf = "Hugging Face" in (item.get("signal") or "")
    coverage = len(item.get("also_covered_by") or [])
    drama = bool(DRAMA.search(title))

    importance = 4.0
    importance += 2 if item.get("official") else 0
    importance += 2 if category == "model_release" else 0
    importance += 1 if entities else 0
    importance += min(3, points // 200) if is_hn else 0
    importance += min(2, points // 60) if is_hf else 0
    importance += min(2, coverage)

    viral = 3.0
    viral += min(4, points // 150) if is_hn else 0
    viral += min(2, comments // 150) if is_hn else 0
    viral += 1 if entities else 0
    viral += 2 if drama else 0
    viral += 1 if category in ("model_release", "tools_for_creators") else 0
    viral += min(1, coverage)

    worthiness = (importance + viral) / 2 + (1 if category in ("tools_for_creators", "model_release") else 0)
    imp, vir, worth = clamp(importance), clamp(viral), clamp(worthiness)

    tags = []
    if AI_RE.search(text) or category in ("model_release", "open_source"):
        tags.append("#هوش_مصنوعی")
    tags.append(HASHTAG_FA[category])
    tags += [f"#{e}" for e in entities[:2]]
    return {**item, "category": category, "category_fa": category_name(category), "importance": imp,
            "viral": vir, "worthiness": worth, "hot": vir >= 8 or imp >= 9, "tags": tags[:4]}
