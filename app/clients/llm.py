"""LLM access. Concentrate primary, OpenRouter when a key for it exists.

Concentrate is an Anthropic-compatible gateway: same Messages API, same model names,
base https://api.concentrate.ai. Verified 2026-09-07 -- claude-haiku-4-5-20251001 and
claude-sonnet-5 both answer 200 through it. This mirrors how buzzlead-signal-engine
routes (src/lib/anthropic.ts): the gateway takes over when its key is present, so
switching providers is a variable rather than a code change.

The prompt files name models in OpenRouter's namespaced form ("anthropic/claude-haiku-4.5",
"openai/gpt-oss-120b") because that is what the Clay workflow used. Those ids are kept
verbatim in prompts/ and translated here, so the prompt files stay a faithful record of
the Clay chain regardless of which provider actually serves the call.

ONE DELIBERATE DEVIATION: the research step's openai/gpt-oss-120b is not available on
Concentrate (400 invalid model) and there is no OpenRouter key. It falls back to
claude-sonnet-5. That is the same fallback buzzlead-signal-engine documents for the same
step ("Absent key -> research falls back to Sonnet through the Anthropic client"). The
research SYSTEM PROMPT is still used verbatim; only the model serving it changes. Restore
the exact chain by setting OPENROUTER_API_KEY.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ..config import settings
from .http import VendorError, VendorOutage, request

# Real spend, priced from each response's own token usage. $/1M tokens, Anthropic list prices; the cache
# multipliers are 1.25x to write and 0.1x to read. Used to report what a run actually cost.
PRICES_PER_MTOK = {"claude-sonnet-4-6": (3.00, 15.00), "claude-haiku-4-5": (1.00, 5.00),
                   "claude-haiku-4-5-20251001": (1.00, 5.00), "claude-haiku-4.5": (1.00, 5.00)}
_USAGE_LOG: list[dict] = []


def _record_usage(model: str, provider: str, usage: dict) -> None:
    if usage:
        _USAGE_LOG.append({"model": resolve_model(model) if provider == "concentrate" else model, "provider": provider,
                           "in": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                           "out": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
                           "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                           "cache_write": int(usage.get("cache_creation_input_tokens") or 0)})


def usage_mark() -> int:
    return len(_USAGE_LOG)


def usage_cost_since(mark: int = 0) -> dict:
    total, unpriced = 0.0, set()
    by_model: dict[str, float] = {}
    for u in _USAGE_LOG[mark:]:
        px = PRICES_PER_MTOK.get(u["model"].rsplit("/", 1)[-1])
        if not px:
            unpriced.add(u["model"]); continue
        reg = max(u["in"] - u["cache_read"] - u["cache_write"], 0)
        c = (reg * px[0] + u["cache_write"] * px[0] * 1.25 + u["cache_read"] * px[0] * 0.1 + u["out"] * px[1]) / 1e6
        total += c; by_model[u["model"]] = by_model.get(u["model"], 0.0) + c
    return {"usd": round(total, 4), "calls": len(_USAGE_LOG) - mark, "by_model": {k: round(v, 4) for k, v in by_model.items()},
            "unpriced_models": sorted(unpriced)}


CONCENTRATE_URL = "https://api.concentrate.ai/v1/messages"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# OpenRouter-style id -> Anthropic/Concentrate model name.
MODEL_MAP = {
    "anthropic/claude-haiku-4.5": "claude-haiku-4-5-20251001",
    "anthropic/claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "anthropic/claude-sonnet-5": "claude-sonnet-5",
    "anthropic/claude-sonnet-4.5": "claude-sonnet-4-5",
    "anthropic/claude-opus-5.5": "claude-opus-5-5",
}

# Models with no Concentrate equivalent, and what serves them instead.
# Measured 2026-09-10 on the same two companies, same prompt: Sonnet produced a 5.2k
# report with 6 concrete facts, Haiku an 11.4k report with 15 (dates, dollar figures,
# named outlets). Haiku is not the weaker researcher here, it is the wordier one -- and
# it is several times cheaper on the largest LLM line in the pipeline. It is capped at
# RESEARCH_MAX_TOKENS so the extra length does not simply reappear as input cost in the
# two email prompts that consume the report.
RESEARCH_FALLBACK = os.getenv("RESEARCH_MODEL", "claude-haiku-4-5-20251001")
NO_GATEWAY_EQUIVALENT = {"openai/gpt-oss-120b"}

_warned: set[str] = set()



import threading
import time

# At most this many requests in flight to the gateway at once, across every stage.
#
# 4 was a panic value set on 2026-09-13 when a 402 looked like a burst throttle. It
# turned out to be an empty balance, and the real protections are the retry ladder and
# the per-provider circuit breaker below -- but the 4 stayed, and it became the hard
# ceiling on the whole pipeline: 36 copy workers (3 verticals x 12) funnelling through
# four slots. Measured 2026-09-14, a contact needs ~17s of LLM across 3.5 calls, so
# four in flight caps copy at roughly 850 contacts an hour no matter how many workers
# are waiting. 24 matches the worker count with headroom.
_LLM_GATE = threading.BoundedSemaphore(int(__import__("os").getenv("LLM_MAX_CONCURRENCY", "24")))
_UNAVAILABLE_RETRIES = 3
_UNAVAILABLE_BACKOFF = (20, 40, 60)     # seconds; ~2 minutes before giving up

# A gateway can say "out of credit" with almost any status code. Anthropic returns
# HTTP 400 with "Your credit balance is too low"; Concentrate and OpenRouter use 402.
# Measured 2026-09-13: because only 401/402/403 were treated as an outage, 141
# enforcement chunks were logged as ordinary per-chunk failures and 2,820 companies
# were dropped unjudged -- exactly the silent-loss failure LLMUnavailable exists to
# prevent. Match on the MESSAGE too, never on the status alone.
_OUT_OF_CREDIT = ("credit balance", "insufficient_credits", "insufficient funds",
                  "quota", "billing", "payment required", "out of credit")


def _is_outage(status: int | None, body: str) -> bool:
    if status in (401, 402, 403):
        return True
    return any(t in (body or "").lower() for t in _OUT_OF_CREDIT)


class LLMUnavailable(VendorOutage):
    """The gateway cannot serve ANY request: out of credit (402) or key rejected (401).

    Distinct from a per-call failure on purpose. A caller that treats this like a bad
    reply -- drop the chunk, mark the contact needs_review, carry on -- destroys a whole
    run one item at a time and files every loss under a misleading reason. On
    2026-09-11 Concentrate ran out of funds mid-run; enforcement began dropping 20
    companies per chunk and 20 Proptech contacts were binned as "research call
    failed". Callers must let this propagate so the runner can halt and re-queue.
    """

def _use_openrouter() -> bool:
    """Only when a key that is actually an OpenRouter key is configured.

    Checked by prefix on purpose: OPENROUTER_API_KEY has held a Concentrate key
    (sk-cn-v1-) on this machine, which OpenRouter rejects with a 401 that reads
    "Missing Authentication header" and looks nothing like a wrong-key error.
    """
    key = settings.openrouter_api_key
    return bool(key and key.startswith("sk-or-"))


def resolve_model(model: str) -> str:
    """Concentrate model id for a Clay/OpenRouter-style id. OpenRouter passes through."""
    if model in NO_GATEWAY_EQUIVALENT:
        if model not in _warned:
            print(f"[llm] {model} has no Concentrate equivalent; using {RESEARCH_FALLBACK}. "
                  f"Set OPENROUTER_API_KEY to restore the exact Clay chain.", flush=True)
            _warned.add(model)
        return RESEARCH_FALLBACK
    return MODEL_MAP.get(model, model.split("/", 1)[-1])


def _concentrate(model: str, system: str, user: str, temperature: float,
                 max_tokens: int | None) -> str:
    if not settings.concentrate_api_key:
        raise RuntimeError("CONCENTRATE_AI_API_KEY is not set")
    body: dict[str, Any] = {
        "model": resolve_model(model),
        "max_tokens": max_tokens or 4096,
        "temperature": temperature,
        # Prompt caching (Troy, 2026-09-27): the system prompt is identical on every call
        # of a run, so mark it cacheable; a cached read is a tenth of the input price.
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        # Short, rule-following writing calls. A model that thinks by default spent its
        # whole budget thinking and returned empty emails (2026-09-27).
        "thinking": {"type": "disabled"},
    }
    # Concentrate answers a BURST with 402 and the text "may be because your
    # organization is sending too many concurrent requests" -- the same code it uses
    # for an empty balance. Measured 2026-09-11: the copy stage's 12 workers opened
    # together, the first call got 402, the run halted as out-of-credit, and a single
    # call succeeded minutes later. So: cap concurrency with a semaphore, and on 402
    # back off and retry a few times before concluding the account is actually dry.
    # A model that thinks by default can spend the whole max_tokens budget thinking and
    # hand back a thinking block with no text (claude-sonnet-5, 2026-09-27: a 600-token
    # Email 1 budget came back empty and crashed the copy run). Retry once with room to
    # think AND answer; if it is still empty, fall through to the next provider.
    for budget_try in range(2):
        last: Exception | None = None
        for attempt in range(_UNAVAILABLE_RETRIES + 1):
            try:
                with _LLM_GATE:
                    data = request(
                        "concentrate", "POST", CONCENTRATE_URL,
                        headers={
                            "x-api-key": settings.concentrate_api_key,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json",
                        },
                        json_body=body,
                    )
                break
            except VendorError as exc:
                if exc.status == 401:
                    raise LLMUnavailable(f"concentrate 401: {str(exc)[:200]}") from exc
                # Newer models refuse the parameter outright ("`temperature` is deprecated
                # for this model", claude-sonnet-5 via Bedrock, 2026-09-27). Send the same
                # request without it rather than fail every email that model writes.
                if (exc.status == 400 and "temperature" in str(exc).lower()
                        and "temperature" in body):
                    body.pop("temperature")
                    continue
                if exc.status == 400 and "thinking" in str(exc).lower() and "thinking" in body:
                    body.pop("thinking")
                    continue
                if not _is_outage(exc.status, str(exc)):
                    raise
                if attempt < _UNAVAILABLE_RETRIES:
                    last = exc
                    time.sleep(_UNAVAILABLE_BACKOFF[attempt])
                    continue
                raise LLMUnavailable(f"concentrate {exc.status} after {attempt + 1} attempts "
                                     f"over {sum(_UNAVAILABLE_BACKOFF)}s: {str(exc)[:160]}") from exc
        else:  # pragma: no cover
            raise LLMUnavailable(str(last))
        blocks = (data or {}).get("content") or []
        text = "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict) and b.get("type", "text") == "text")
        if text.strip():
            _record_usage(model, "concentrate", (data or {}).get("usage") or {})
            return text
        if (data or {}).get("stop_reason") == "refusal":
            # The model declined THIS prompt. That is one contact's problem, not an outage:
            # on 2026-09-27 one declined email was reported as "every provider is out" and
            # stopped a 400-contact copy run (and the whole campaign fill) at 20:13.
            raise VendorError("concentrate", None, "model declined to write this one (refusal)")
        body["max_tokens"] = max(int(body.get("max_tokens") or 0) * 4, 4000)
    raise LLMUnavailable(f"concentrate returned no text twice: {str(data)[:200]}")


def _anthropic(model: str, system: str, user: str, temperature: float,
               max_tokens: int | None) -> str:
    """Anthropic's own Messages API. Same wire shape as Concentrate, own balance."""
    if not settings.anthropic_api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
    # Cache the system prompt. Enforcement sends the SAME ~900-token system block on
    # every one of 300-400 calls per vertical; a cached read is a tenth of the price.
    # Only on this path -- Concentrate is Anthropic-compatible but not verified to
    # accept cache_control, and copy runs there.
    body: dict[str, Any] = {
        "model": resolve_model(model),
        "max_tokens": max_tokens or 4096,
        "temperature": temperature,
        "system": [{"type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        # Short, rule-following calls. claude-sonnet-5 thinks by default and spent a 300-token fit-check budget on hidden
        # thinking, returning a thinking block with no text (Lumion, 2026-10-09: 6 of 20 fit checks failed that way).
        # The Concentrate path already disables it; this path did not.
        "thinking": {"type": "disabled"},
    }
    data = None
    for _attempt in range(3):
        try:
            with _LLM_GATE:
                data = request(
                    "anthropic", "POST", ANTHROPIC_URL,
                    headers={
                        "x-api-key": settings.anthropic_api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json_body=body,
                )
            break
        except VendorError as exc:
            if _is_outage(exc.status, str(exc)):
                raise LLMUnavailable(f"anthropic {exc.status}: {str(exc)[:200]}") from exc
            msg = str(exc).lower()
            # Newer models refuse a parameter outright ("`temperature` is deprecated for this model"). Send the same
            # request without it, as the Concentrate path does, instead of failing every call that model makes.
            if exc.status == 400 and "temperature" in msg and "temperature" in body:
                body.pop("temperature")
                continue
            if exc.status == 400 and "thinking" in msg and "thinking" in body:
                body.pop("thinking")
                continue
            raise
    blocks = (data or {}).get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    if not text.strip():
        raise VendorError("anthropic", None, f"empty completion: {str(data)[:300]}")
    _record_usage(model, "anthropic", (data or {}).get("usage") or {})
    return text


def _gemini(system: str, user: str, temperature: float,
            max_tokens: int | None) -> str:
    """Google Gemini. Pre-copy fallback only -- see CHAINS for the measured agreement.

    thinkingBudget 0: 2.5-flash otherwise spends its output budget reasoning and
    returns an empty body. Measured 2026-09-13, that lost 280 of 300 verdicts.
    """
    if not settings.gemini_api_key:
        raise LLMUnavailable("GEMINI_API_KEY is not set")
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user}]}],
        "generationConfig": {
            "maxOutputTokens": max(max_tokens or 2000, 4000),
            "temperature": temperature,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        with _LLM_GATE:
            data = request("gemini", "POST",
                           GEMINI_URL.format(model=GEMINI_MODEL),
                           headers={"content-type": "application/json"},
                           params={"key": settings.gemini_api_key}, json_body=body)
    except VendorError as exc:
        if _is_outage(exc.status, str(exc)):
            raise LLMUnavailable(f"gemini {exc.status}: {str(exc)[:200]}") from exc
        raise
    try:
        parts = (data or {})["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    except Exception:
        raise VendorError("gemini", None, f"unexpected reply: {str(data)[:300]}")
    if not text.strip():
        raise VendorError("gemini", None, f"empty completion: {str(data)[:300]}")
    return text


def _openrouter(model: str, system: str, user: str, temperature: float,
                max_tokens: int | None) -> str:
    body: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        # Cacheable system prompt; OpenRouter passes cache_control through to Anthropic.
        "messages": [{"role": "system", "content": [
                         {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]},
                     {"role": "user", "content": user}],
    }
    if model.startswith("openai/gpt-oss"):
        # A reasoning model: its hidden thinking is billed and counted against max_tokens, so a 1,400-token cap can leave the visible brief
        # empty. Low effort and a floor keep the brief whole (not yet measured live: OpenRouter was out of credit on 2026-10-11).
        body["reasoning"] = {"effort": "low"}
        max_tokens = max(max_tokens or 0, 4000)
    if max_tokens:
        body["max_tokens"] = max_tokens
    try:
        data = request("openrouter", "POST", OPENROUTER_URL, json_body=body, headers={
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "X-Title": "IDPR Vertical Expander",
        })
    except VendorError as exc:
        if _is_outage(exc.status, str(exc)):
            raise LLMUnavailable(
                f"OpenRouter returned {exc.status}. Halting rather than emitting empty "
                "copy for the whole batch. Top up, or unset OPENROUTER_API_KEY to fall "
                "back to Concentrate."
            ) from exc
        raise
    choices = (data or {}).get("choices") or []
    if not choices:
        raise VendorError("openrouter", None, f"no choices: {str(data)[:300]}")
    _record_usage(model, "openrouter", (data or {}).get("usage") or {})
    return (choices[0].get("message") or {}).get("content") or ""


PROVIDERS = {"concentrate": None, "openrouter": None}   # filled below


# Troy, 2026-09-13: "anthropic direct it is, let copy be concentrate." Two chains.
# Pre-copy work (proposal, enforcement, widening) is ~20 cents a vertical on haiku and
# runs on Anthropic's own key so the copy account cannot starve it. Copy is 95% of
# spend and stays on Concentrate; Anthropic is deliberately NOT its fallback, so an
# outage pauses copy for the 2-hour check instead of quietly moving ~30,000 calls a
# night onto a different bill.
CHAINS = {
    # Gemini is LAST on purpose. Measured 2026-09-13 against 300 medical-device
    # companies haiku had already judged, gemini-2.5-flash agrees on keep-vs-drop 83%
    # of the time and keeps ~12% more (gpt-4o-mini managed only 74%), so it is a usable
    # stand-in but not the calibrated judge. Putting it at the end means it is reached
    # only when the others are dry, and the chain reverts on its own once they are
    # topped up. It is not in the copy chain: copy stays on Concentrate.
    "precopy": ["anthropic", "openrouter", "concentrate", "gemini"],
    # OpenRouter FIRST for copy, from 2026-09-14, once both had credit. Not a cost
    # choice alone: the Clay research prompt names openai/gpt-oss-120b, which
    # Concentrate does not carry, so that route silently falls back to haiku. Measured
    # the same day on OpenRouter's own price list -- gpt-oss-120b $0.037/M in vs
    # haiku's $1.00/M -- research costs $0.0017 there against $0.019 on the fallback:
    # $0.025 a contact vs $0.043. Going OpenRouter-first restores the exact approved
    # Clay chain AND halves the bill; Concentrate remains the fallback.
    # Split by prompt, on Troy's instruction 2026-09-14: "concentrate should be used
    # for the email 1 and 2 prompt". Research runs OpenRouter-first because the Clay
    # prompt names openai/gpt-oss-120b, which Concentrate does not carry and silently
    # substitutes with haiku at 27x the input price; the two email prompts are haiku
    # either way, so they go to Concentrate and the spend is split across both
    # accounts instead of draining one. Each keeps the other as fallback.
    # BuzzLead's own LLM gateway is FIRST on both, as in ProductEVO (8eb7c65): it carries claude-haiku-4-5 and gpt-oss-120b on BuzzLead's
    # accounts, so neither the shared OpenRouter balance nor the shared Concentrate balance is touched while it answers. The old order stays
    # behind it as the fallback. LLM_BUZZLEAD=false turns it off. Emmanuel, 2026-10-15.
    # fit screen: gpt-oss only. Concentrate would swap in Haiku, which rejects half of the real fits (measured 2026-10-15), so it is not a fallback.
    "screen": ["buzzlead", "openrouter"],
    "research": ["buzzlead", "openrouter", "concentrate"],
    "copy": ["buzzlead", "concentrate", "openrouter"],
}


def buzzlead_slug(model: str) -> str:
    """BuzzLead's bare model slug for one of our ids: 'anthropic/claude-haiku-4.5' -> 'claude-haiku-4-5'."""
    bare = model.split("/", 1)[-1]
    return bare.replace(".", "-") if bare.startswith("claude") else bare


def _buzzlead(model: str, system: str, user: str, temperature: float,
              max_tokens: int | None) -> str:
    """BuzzLead's own LLM gateway (POST /llm/chat/completions): its provider waterfall serves the call on BuzzLead's accounts, billed to
    the key. Any refusal or server error is an outage here, so the chain moves on to the next provider."""
    base = (settings.buzzlead_base or "https://research-615339619342.us-east4.run.app").rstrip("/")
    body: dict[str, Any] = {"model": buzzlead_slug(model), "temperature": temperature,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if max_tokens:
        body["max_tokens"] = max(max_tokens, 4000) if "gpt-oss" in model else max_tokens   # hidden reasoning counts against the cap
    try:
        data = request("buzzlead_llm", "POST", f"{base}/llm/chat/completions", json_body=body, headers={
            "Authorization": f"Bearer {settings.buzzlead_api_key}", "Content-Type": "application/json"})
    except VendorError as exc:
        if exc.status is None or exc.status in (401, 402, 403, 429) or exc.status >= 500:
            raise LLMUnavailable(f"BuzzLead LLM returned {exc.status}: {str(exc)[:120]}") from exc
        raise
    choices = (data or {}).get("choices") or []
    text = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
    if not text:
        raise LLMUnavailable(f"BuzzLead LLM returned no text: {str(data)[:200]}")
    _record_usage(model, "buzzlead", (data or {}).get("usage") or {})
    return text


def _has(provider: str) -> bool:
    if provider == "buzzlead":
        return bool(settings.buzzlead_api_key) and os.getenv("LLM_BUZZLEAD", "true").strip().lower() != "false"
    if provider == "openrouter":
        return _use_openrouter()
    if provider == "anthropic":
        return bool(settings.anthropic_api_key)
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    return bool(settings.concentrate_api_key)


# Per-provider circuit breaker. A gateway that just said "out of credit" will say it
# again on the next call, and the next; walking the whole chain every time cost two
# minutes a call on 2026-09-13 -- Anthropic 400, OpenRouter 402, then Concentrate's
# 20/40/60s backoff -- before the one live provider was reached. Biotech's gate did 79
# verdicts in six minutes. A refused provider is skipped for DOWN_FOR seconds.
DOWN_FOR = int(os.getenv("LLM_DOWN_FOR_S", "900"))
_down_until: dict[str, float] = {}
_down_lock = threading.Lock()


def _mark_down(provider: str) -> None:
    with _down_lock:
        _down_until[provider] = time.time() + DOWN_FOR


def _is_down(provider: str) -> bool:
    with _down_lock:
        return _down_until.get(provider, 0) > time.time()


import itertools as _itertools
_COPY_ROTATE = _itertools.count()


def _order(provider: str | None) -> list[str]:
    """Which gateways to try, in order, for one call.

    Troy, 2026-09-13: "line 86 in the env contains open router api key, lets use it
    for the enforcement." Each call names a preferred provider (or inherits the
    default); the other configured provider is the fallback when the preferred one
    is out of credit. Both accounts have run dry on the same day, so the pipeline
    should never depend on exactly one of them.
    """
    chain = CHAINS.get(provider or "copy") or [provider]
    if (provider or "copy") == "copy":
        # Emmanuel, 2026-10-16: the BuzzLead gateway answered a Haiku email call in 22 s on average, OpenRouter in 6 s (measured, same prompt, 8 in
        # parallel). LLM_COPY_CHAIN puts the faster route first, and LLM_COPY_ROTATE alternates which provider goes first so the load (and the bill)
        # is split across both accounts, as IDPR does with Concentrate and OpenRouter.
        override = os.getenv("LLM_COPY_CHAIN", "").strip()
        if override:
            chain = [c.strip() for c in override.split(",") if c.strip()]
        if os.getenv("LLM_COPY_ROTATE", "").strip().lower() in ("1", "true", "yes") and len(chain) > 1:
            if next(_COPY_ROTATE) % 2:
                chain = chain[1:2] + chain[:1] + chain[2:]
    live = [p for p in chain if _has(p) and not _is_down(p)]
    # If everything is marked down, try the chain anyway -- a breaker must never turn
    # a temporary outage into a permanent one.
    return live or [p for p in chain if _has(p)]


def _model_label(provider: str, model: str) -> str:
    """The model a provider will actually run for a requested id, as 'provider/model'."""
    if provider == "openrouter":
        return f"openrouter/{model}"
    if provider == "gemini":
        return f"gemini/{GEMINI_MODEL}"
    if provider == "buzzlead":
        return f"buzzlead/{buzzlead_slug(model)}"
    return f"{provider}/{resolve_model(model)}"


def complete_meta(model: str, system: str, user: str, temperature: float,
                  max_tokens: int | None = None, provider: str | None = None
                  ) -> tuple[str, str, str]:
    """complete(), plus WHICH provider and model produced the text.

    Returns (text, provider, "provider/model"). The chain falls back silently by
    design, so without this a run written half on Concentrate and half on OpenRouter
    -- or research written by haiku instead of gpt-oss-120b -- leaves no trace on the
    rows it produced, and no copy change can ever be measured against replies.
    """
    order = _order(provider)
    if not order:
        raise LLMUnavailable("no LLM provider configured")
    last: Exception | None = None
    for name in order:
        try:
            if name == "buzzlead":
                text = _buzzlead(model, system, user, temperature, max_tokens)
            elif name == "openrouter":
                text = _openrouter(model, system, user, temperature, max_tokens)
            elif name == "anthropic":
                text = _anthropic(model, system, user, temperature, max_tokens)
            elif name == "gemini":
                text = _gemini(system, user, temperature, max_tokens)
            else:
                text = _concentrate(model, system, user, temperature, max_tokens)
            return text, name, _model_label(name, model)
        except LLMUnavailable as exc:
            last = exc
            _mark_down(name)
            remaining = order[order.index(name) + 1:]
            if remaining:
                print(f"[llm] {name} unavailable ({str(exc)[:60]}); skipping it for "
                      f"{DOWN_FOR // 60} min, falling back to {remaining[0]}", flush=True)
            continue
    raise LLMUnavailable(f"every configured provider is out: {last}")


def complete(model: str, system: str, user: str, temperature: float,
             max_tokens: int | None = None, provider: str | None = None) -> str:
    """`provider` names a CHAIN: "precopy", "research" or "copy" (default)."""
    return complete_meta(model, system, user, temperature, max_tokens, provider)[0]


def complete_json_retrying(model: str, system: str, user: str, temperature: float,
                           max_tokens: int | None = None,
                           provider: str | None = None, attempts: int = 3):
    """complete_json, retrying a TRUNCATED reply with a bigger budget.

    A cut-off reply is not a bad prompt and not an outage; it is one unlucky
    generation, and it killed a whole ProductEVO run three seconds in on 2026-09-21.
    Each retry doubles the budget, because the commonest cause is simply asking for
    more JSON than the ceiling allows.

    An EMPTY reply is retried too. It was not, and on 2026-09-23 the adjacent split --
    the loop that turns a winner into the next generation of audiences, and which had
    never once succeeded in this system's life -- failed with "no JSON found in model
    reply:" and nothing after the colon. The model returned nothing at all. That is the
    same unlucky generation, and re-asking is the whole remedy; a bigger budget does not
    help an empty reply but costs nothing either.
    """
    budget = max_tokens or 2000
    last: Exception | None = None
    for i in range(attempts):
        try:
            return complete_json(model, system, user, temperature, budget, provider)
        except ValueError as exc:
            last = exc
            msg = str(exc)
            retryable = ("TRUNCATED" in msg or "not valid JSON" in msg
                         or "no JSON found" in msg)
            if not retryable:
                raise
            budget = min(budget * 2, 16000)
            print(f"[llm] reply was not usable JSON (attempt {i + 1}/{attempts}); "
                  f"retrying with max_tokens={budget}", flush=True)
    raise last if last else ValueError("no JSON after retries")


def complete_json(model: str, system: str, user: str, temperature: float,
                  max_tokens: int | None = None, provider: str | None = None) -> Any:
    """Same, but parse a JSON object out of the reply.

    Models wrap JSON in prose or ```json fences often enough that a bare json.loads
    fails on otherwise-good output, so pull the outermost object out first.
    """
    raw = complete(model, system, user, temperature, max_tokens, provider=provider)
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        span = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
        if not span:
            raise ValueError(f"no JSON found in model reply: {raw[:300]}")
        try:
            return json.loads(span.group(0))
        except json.JSONDecodeError as exc:
            # Almost always a max_tokens cut rather than a malformed model reply: the
            # object simply stops mid-key. Say so, because "Expecting ',' delimiter"
            # sends you looking for a prompt bug that is not there.
            truncated = not text.rstrip().endswith(("}", "]"))
            hint = (" -- reply appears TRUNCATED, raise max_tokens"
                    if truncated else "")
            raise ValueError(
                f"model reply is not valid JSON{hint}. {exc}. "
                f"Reply length {len(raw)} chars, ends: {raw[-120:]!r}"
            ) from exc
