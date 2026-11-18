# ai-news-radar — notes for Claude Code

Daily Persian AI/tech news digest for a Persian-language Instagram creator. Runs on GitHub Actions
(cron, `.github/workflows/daily.yml`), uses the free Gemini API, delivers to Telegram, archives to
`reports/YYYY/DATE.md|json`, and keeps `state/state.json` (seen URLs, recently reported stories).

Pipeline (`radar/pipeline.py`): collectors (RSS / Hacker News Algolia / HF daily papers) → dedupe →
triage agent (1 Gemini call per 150 items: importance, worthiness, story_key, Persian title) → pick
(one item per story_key, official sources preferred) → enrich finalists (article text + media,
respects robots.txt) → deep agent (1 call: Persian summary, key points, content ideas) → Telegram.

Rules:
- Keep Gemini usage to ~2 requests/day (free tier quota is small). Batch, never per-item calls.
- Every source and article fetch must fail soft; one bad site never stops the run.
- If the LLM is down, still send a raw list and do NOT mark items seen.
- User-facing text is Persian; prompts are English. Telegram text is HTML, always escape model output.
- Tests run offline: `python -m pytest -q`.
