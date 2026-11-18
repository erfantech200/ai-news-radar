"""Tiny LLM client: Gemini (free tier) with a model fallback chain, plus an optional
OpenAI-compatible fallback (OpenRouter, Groq, Cerebras ... whatever has a free key)."""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                  "gemini-flash-latest"]


class LLMError(RuntimeError):
    pass


def parse_json(text: str) -> dict:
    """Parse model output into a dict, tolerating code fences and chatter around it."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("model did not return JSON") from None
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned broken JSON: {exc}") from None
    if isinstance(value, list):
        value = {"items": value}
    if not isinstance(value, dict):
        raise LLMError("model returned JSON that is not an object")
    return value


def _retry_delay(resp: requests.Response) -> float | None:
    """Seconds Google asks us to wait, if it says so."""
    try:
        for detail in resp.json().get("error", {}).get("details", []):
            delay = detail.get("retryDelay")
            if delay:
                return float(str(delay).rstrip("s"))
    except (ValueError, AttributeError):
        pass
    return None


class LLM:
    def __init__(self, gemini_key: str | None = None, models: list[str] | None = None,
                 timeout: float = 300):
        self.gemini_key = gemini_key if gemini_key is not None else os.getenv("GEMINI_API_KEY", "")
        env_models = [m.strip() for m in os.getenv("GEMINI_MODELS", "").split(",") if m.strip()]
        self.models = models or env_models or DEFAULT_MODELS
        self.timeout = timeout
        self.fallback_url = os.getenv("FALLBACK_LLM_BASE_URL", "").rstrip("/")
        self.fallback_key = os.getenv("FALLBACK_LLM_API_KEY", "")
        self.fallback_model = os.getenv("FALLBACK_LLM_MODEL", "")
        self.used_models: list[str] = []
        self._dead: set[str] = set()   # models that are unavailable for this key today

    # ------------------------------------------------------------------ Gemini
    def _gemini_once(self, model: str, system: str, prompt: str, max_tokens: int,
                     temperature: float) -> requests.Response:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        return requests.post(GEMINI_URL.format(model=model), json=body, timeout=self.timeout,
                             headers={"x-goog-api-key": self.gemini_key})

    @staticmethod
    def _gemini_text(data: dict) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            reason = data.get("promptFeedback", {}).get("blockReason", "no candidates")
            raise LLMError(f"Gemini returned nothing ({reason})")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise LLMError(f"empty answer (finishReason={candidates[0].get('finishReason')})")
        return text

    def _try_gemini(self, model: str, system: str, prompt: str, max_tokens: int,
                    temperature: float) -> dict:
        for attempt in range(3):
            try:
                resp = self._gemini_once(model, system, prompt, max_tokens, temperature)
            except requests.RequestException as exc:
                log.warning("%s network error: %s", model, exc)
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code == 200:
                data = resp.json()
                try:
                    return parse_json(self._gemini_text(data))
                except LLMError as exc:
                    finish = (data.get("candidates") or [{}])[0].get("finishReason")
                    log.warning("%s bad output (%s, finish=%s)", model, exc, finish)
                    if finish == "MAX_TOKENS":
                        raise
                    continue
            if resp.status_code == 429:
                delay = _retry_delay(resp)
                body = resp.text[:300]
                if "PerDay" in body or "per day" in body.lower() or delay is None or delay > 60:
                    raise LLMError(f"{model}: daily quota used up")
                log.info("%s rate limited, waiting %.0fs", model, delay)
                time.sleep(delay + 1)
                continue
            if resp.status_code in (500, 502, 503, 504):
                time.sleep(8 * (attempt + 1))
                continue
            # 400/403/404: model missing or not on the free tier for this key.
            raise LLMError(f"{model}: HTTP {resp.status_code} {resp.text[:200]}")
        raise LLMError(f"{model}: gave up after retries")

    # ------------------------------------------------- OpenAI-compatible backup
    def _try_fallback(self, system: str, prompt: str, max_tokens: int, temperature: float) -> dict:
        body = {
            "model": self.fallback_model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.fallback_key}"}
        url = f"{self.fallback_url}/chat/completions"
        resp = requests.post(url, json=body, headers=headers, timeout=self.timeout)
        if resp.status_code == 400:  # some providers reject response_format
            body.pop("response_format")
            resp = requests.post(url, json=body, headers=headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise LLMError(f"fallback: HTTP {resp.status_code} {resp.text[:200]}")
        return parse_json(resp.json()["choices"][0]["message"]["content"])

    # ------------------------------------------------------------------ public
    def generate_json(self, system: str, prompt: str, max_tokens: int = 16384,
                      temperature: float = 0.4) -> dict:
        errors = []
        if self.gemini_key:
            for model in self.models:
                if model in self._dead:
                    continue
                try:
                    result = self._try_gemini(model, system, prompt, max_tokens, temperature)
                    self.used_models.append(model)
                    return result
                except LLMError as exc:
                    log.warning("model failed: %s", exc)
                    errors.append(str(exc))
                    if "HTTP 4" in str(exc) or "quota" in str(exc):
                        self._dead.add(model)
        if self.fallback_url and self.fallback_key and self.fallback_model:
            try:
                result = self._try_fallback(system, prompt, max_tokens, temperature)
                self.used_models.append(self.fallback_model)
                return result
            except (LLMError, requests.RequestException, KeyError) as exc:
                errors.append(str(exc))
        if not errors:
            raise LLMError("no LLM configured: set GEMINI_API_KEY")
        raise LLMError("all models failed: " + " | ".join(errors))
