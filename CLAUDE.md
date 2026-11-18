# ai-news-radar — notes for Claude Code

Daily Persian AI/tech news digest for a Persian-language Instagram creator. Runs on GitHub Actions
3x/day (cron, `.github/workflows/daily.yml`: morning/midday/evening Melbourne), uses the free Gemini API, delivers to Telegram, archives to
`reports/YYYY/DATE-HHMM.md|json` (one per run), and keeps `state/state.json` (seen URLs, recently reported stories).

Pipeline (`radar/pipeline.py`): collectors (RSS / Hacker News Algolia / HF daily papers) → dedupe →
triage agent (1 Gemini call per 150 items: importance, worthiness, story_key, Persian title) → pick
(one item per story_key, official sources preferred) → enrich finalists (article text + media,
respects robots.txt) → deep agent (1 call: Persian summary, key points, content ideas) → Telegram.

Breaking alerts (`radar/alerts.py`, `.github/workflows/alerts.yml`, `python main.py --alerts`):
every 20 min 14-24 UTC, hourly otherwise. Sources in `config.yaml > alerts` (official RSS/pages,
HN viral/rising, new Hugging Face repos of big labs). A new source is learned silently on first
read. No LLM call without fresh candidates; one batched Flash-Lite call when there are; fallback
to keyword heuristics if the LLM is down. State is `state/alerts.json` in the Actions cache (never
committed); the digest restores it read-only to mark already-alerted stories. Triage also scores
`viral`; `item.hot` (viral >= 8 or importance >= 9) drives the 🔥 highlights.
Actions budget (private repo, 2000 min/month): ~44 alert runs/day must stay under 1 minute each.

Desktop visualiser (`desktop/`, built by `.github/workflows/desktop.yml` on windows-latest with
PyInstaller, published as a GitHub Release): pywebview window over `desktop/ui/index.html`. The
Python `Api` runs the real collectors per source, `radar.dedup`, and `desktop/scoring.py` (local
keyword/engagement scoring, no API key). With a read-only GitHub token (stored in
%APPDATA%/AINewsRadar/settings.json) it loads the latest `reports/*.json`. Browser preview:
`desktop/ui/index.html?demo` uses mock data. Windows minutes count 2x: build only on demand.

Rules:
- Keep Gemini usage to ~2 requests per run, ~6/day (free tier quota is small). Batch, never per-item calls.
- Every source and article fetch must fail soft; one bad site never stops the run.
- If the LLM is down, still send a raw list and do NOT mark items seen.
- User-facing text is Persian; prompts are English. Telegram text is HTML, always escape model output.
- Tests run offline: `python -m pytest -q`.
