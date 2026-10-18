"""
One-shot calls to Anthropic's Messages API, shaped exactly like core.gemini.

WHY THIS FILE IS SEPARATE
    `core/gemini.py` owns every one-shot call the assistant makes (a screenshot
    read, a page parsed, a command chosen) and the ladder that keeps them
    answering when one model is out of quota. This file adds a second provider
    behind the same contract, so `ai_provider` in the config switches the side
    calls over without a single call site changing.

WHAT IT DOES NOT TOUCH
    The conversation. That runs on Gemini's Live API — bidirectional audio — and
    there is no Anthropic equivalent wired in. Only the text/vision/JSON side
    calls come here, and only when `ai_provider` is "claude".

    Grounded search never comes here either. `actions/web_search.py:100` reads
    `response.candidates[0].content.parts` and the grounding metadata beside it;
    that is a Gemini object shape and no amount of shimming makes it Anthropic's.

THE CONTRACT
    `call(contents, tier, config, timeout_ms) -> reply | None`
        `reply` has `.text`, like the SDK response and like gemini's `_Reply`.
        `None` means "Claude did not answer" — and every caller already treats
        None as "fall back", so a Claude outage costs a feature nothing: the
        Gemini ladder in gemini.py answers instead. This is why opting in is
        safe to test.
    `text(contents, ...) -> str`
        the reply, or "" when nothing answered.

TRANSPORT
    `requests` with a raw POST — the `anthropic` SDK is not installed and adding
    a dependency for one endpoint is not worth it. One URL, three headers, one
    JSON body, and the response is read as a dict.

ROUTING, MEASURED
    Anthropic keeps a safety classifier that silently answers a small share of
    requests with a different model. The response's `model` field is how that is
    visible; when it differs from what was asked for it is logged and the answer
    is still used, because a routed reply is a reply.

    A `stop_reason` of "refusal" is the opposite: no usable content. That returns
    None so the Gemini ladder takes the request.

COST
    Fable 5 and 5.1 are $10 / $50 per million tokens in and out; Sonnet 5 is
    $3 / $15. Every call logs its own cost, and a per-day cap (default $5) stops
    Claude being used once it is spent — after which the config needs no edit for
    the assistant to go back to Gemini for the rest of the day.
"""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
from datetime import date
from pathlib import Path

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_KEY_FILE = _BASE / "config" / "api_keys.json"

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

FAST = "fast"
SMART = "smart"

# Which model answers which tier. Change it here or in the config
# (`claude_fast_model` / `claude_smart_model`), never at a call site.
_MODELS = {
    SMART: "claude-fable-5-1",  # Updated to Fable 5.1 for better reasoning
    FAST:  "claude-sonnet-5-5",  # Updated to Sonnet 5.5 for better speed/cost
}

# USD per million tokens: (input, output). From the platform pricing docs; used
# only to estimate against the daily cap, so a stale number shifts the cap
# rather than breaking anything.
_PRICES = {
    # Claude 5.x models (latest)
    "claude-opus-5-5":   (4.0, 20.0),  # Best for agentic coding, $4/$20
    "claude-fable-5-1":  (10.0, 50.0), # Best for demanding reasoning, $10/$50
    "claude-fable-5":    (10.0, 50.0), # Original Fable 5
    "claude-sonnet-5-5": (2.0, 10.0),  # Best speed/intelligence balance, $2/$10
    "claude-sonnet-5":   (3.0, 15.0),  # Original Sonnet 5
    "claude-haiku-5-5":  (0.10, 0.50), # Fastest for simple tasks, $0.10/$0.50
}
_PRICE_FALLBACK = (10.0, 50.0)

# Blocking only, and generous: the Messages API rejects a small deadline the way
# Gemini rejects one under 10s, and a vision request needs the room.
DEFAULT_TIMEOUT_S = 30.0
MIN_TIMEOUT_S = 10.0
DEFAULT_MAX_TOKENS = 4096
DEFAULT_DAILY_BUDGET_USD = 5.0

# Status codes that are worth a rest rather than an immediate retry. 400/404 sit
# the model out for the session: a bad model name is not going to fix itself
# between one call and the next, and paying for it on every call is a round trip
# thrown in front of everything the assistant does.
_COOLDOWN = {
    429: 5 * 60,
    500: 5 * 60,
    503: 5 * 60,
    529: 30 * 60,
    404: 6 * 60 * 60,
    400: 6 * 60 * 60,
}

# Reentrant: api_key() and daily_budget_usd() read the config cache while they
# hold it, and a plain Lock there is a self-deadlock on the first call.
_lock = threading.RLock()
_cooldown: dict[str, float] = {}
_spend = {"day": "", "usd": 0.0, "warned": False}

_key_cache: str | None = None
_cfg_cache: dict = {}
_cfg_stamp: float = -1.0


class ClaudeReply:
    """The Messages API response, narrowed to what every call site already reads.

    `.text` is the whole visible answer, joined across content blocks — thinking
    blocks are dropped, since a caller that wanted prose would not have asked for
    one word.
    """

    __slots__ = ("text", "model", "requested_model", "stop_reason",
                 "input_tokens", "output_tokens")

    def __init__(self, text: str, model: str, requested_model: str,
                 stop_reason: str, usage: dict):
        self.text = text
        self.model = model
        self.requested_model = requested_model
        self.stop_reason = stop_reason
        self.input_tokens = int(usage.get("input_tokens") or 0)
        self.output_tokens = int(usage.get("output_tokens") or 0)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _config() -> dict:
    """The whole config dict, re-read when api_keys.json changes on disk.

    Keyed on mtime rather than cached once: the provider is meant to be switched
    from the settings UI while the assistant runs, and a value frozen at import
    would make that require a restart for no benefit.
    """
    global _cfg_cache, _cfg_stamp
    try:
        stamp = _KEY_FILE.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        if stamp == _cfg_stamp and _cfg_cache:
            return _cfg_cache
    try:
        data = json.loads(_KEY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
    except Exception:
        return {}
    with _lock:
        _cfg_cache, _cfg_stamp = data, stamp
    return data


def api_key(refresh: bool = False) -> str:
    """The Anthropic key, or "" if there is none. Cached; never raises.

    Only a non-empty key is cached, so a key saved after startup is picked up
    without restarting — the same reason gemini.api_key works this way.
    """
    global _key_cache
    with _lock:
        if _key_cache is not None and not refresh:
            return _key_cache
        key = str(_config().get("anthropic_api_key") or "")
        if key:
            _key_cache = key
        return key


def provider() -> str:
    return str(_config().get("ai_provider") or "gemini").strip().lower()


def enabled(tier: str) -> bool:
    """Is this tier's job Claude's right now?

    Three conditions, all cheap: the provider is set to claude, the tier is one
    of the two Claude answers (SEARCH and the Live rungs stay Gemini's), and a
    key exists. Called from gemini.call(), so it must not make a request.
    """
    if provider() != "claude":
        return False
    if tier not in _MODELS:
        return False
    return bool(api_key())


def model_for(tier: str) -> str:
    cfg_key = "claude_smart_model" if tier == SMART else "claude_fast_model"
    return str(_config().get(cfg_key) or "").strip() or _MODELS.get(tier, _MODELS[SMART])


def max_tokens() -> int:
    try:
        return max(1, int(_config().get("claude_max_tokens") or DEFAULT_MAX_TOKENS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOKENS


def daily_budget_usd() -> float:
    try:
        cap = float(_config().get("claude_daily_budget_usd") or DEFAULT_DAILY_BUDGET_USD)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_BUDGET_USD
    return cap if cap > 0 else float("inf")


# ---------------------------------------------------------------------------
# Cooldown and budget
# ---------------------------------------------------------------------------

def _cool(model: str, seconds: float) -> None:
    with _lock:
        _cooldown[model] = time.monotonic() + seconds


def _cooling(model: str) -> bool:
    with _lock:
        until = _cooldown.get(model, 0.0)
        if until and time.monotonic() < until:
            return True
        _cooldown.pop(model, None)
        return False


def _budget_left() -> bool:
    """Has today's Claude spend still got room? False means go back to Gemini."""
    cap = daily_budget_usd()
    with _lock:
        today = date.today().isoformat()
        if _spend["day"] != today:
            _spend.update(day=today, usd=0.0, warned=False)
        spent = _spend["usd"]
        if spent < cap:
            return True
        if not _spend["warned"]:
            _spend["warned"] = True
            print(f"[Claude] daily budget of ${cap:.2f} spent "
                  f"(${spent:.2f}) — side calls go back to Gemini until tomorrow.")
    return False


def _charge(used_model: str, in_tok: int, out_tok: int) -> float:
    """Log one call's cost and add it to today's tally. Returns the cost."""
    pin, pout = _PRICES.get(used_model, _PRICE_FALLBACK)
    cost = in_tok / 1e6 * pin + out_tok / 1e6 * pout
    with _lock:
        today = date.today().isoformat()
        if _spend["day"] != today:
            _spend.update(day=today, usd=0.0, warned=False)
        _spend["usd"] += cost
        total = _spend["usd"]
    print(f"[Claude] {used_model}: {in_tok} in / {out_tok} out tokens "
          f"~ ${cost:.4f} (today ${total:.2f} of ${daily_budget_usd():.2f})")
    return cost


# ---------------------------------------------------------------------------
# Contents translation
# ---------------------------------------------------------------------------

def _source(block) -> dict | None:
    """A Gemini inline blob -> an Anthropic base64 source block."""
    data = getattr(block, "data", None)
    if data is None and isinstance(block, dict):
        data = block.get("data")
    if not isinstance(data, (bytes, bytearray)):
        return None
    mime = getattr(block, "mime_type", None) or (
        block.get("mime_type") if isinstance(block, dict) else None) or "image/png"
    src = {"type": "base64",
           "media_type": str(mime),
           "data": base64.b64encode(bytes(data)).decode("ascii")}
    # Images are the shape this app actually passes (screenshots, frames).
    # Anything else is a document, which is what Anthropic calls a PDF.
    return {"type": "image" if str(mime).startswith("image/") else "document",
            "source": src}


def _blocks(contents) -> list[dict]:
    """REST `contents` -> Anthropic content blocks.

    The four shapes the call sites use: a bare string, a list of strings, SDK
    Part objects carrying an image, and the dict form gemini's Live path accepts.
    A list of strings becomes ONE block: splitting "the instruction" from "the
    text it acts on" across blocks invites the model to answer the last one.
    """
    items = contents if isinstance(contents, (list, tuple)) else [contents]
    texts: list[str] = []
    media: list[dict] = []

    for item in items:
        if isinstance(item, str):
            texts.append(item)
            continue
        if isinstance(item, dict):
            if "text" in item:
                texts.append(str(item["text"]))
                continue
            blob = item.get("inline_data") or item.get("image")
            src = _source(blob) if blob is not None else None
            if src:
                media.append(src)
            continue
        blob = getattr(item, "inline_data", None)
        if blob is not None:
            src = _source(blob)
            if src:
                media.append(src)
            continue
        txt = getattr(item, "text", None)
        if txt:
            texts.append(str(txt))

    joined = "\n\n".join(t for t in texts if t.strip())
    blocks = ([{"type": "text", "text": joined}] if joined else [])
    # Anthropic wants text before the image it describes.
    return blocks + media


def _system(config) -> str:
    """Pull a system instruction out of whatever config shape a caller passed.

    `types.GenerateContentConfig(...)` in the actions, a plain dict elsewhere, and
    sometimes a list of parts. The rest of the config — response schemas, tool
    declarations — is Gemini's and is not translated: a call that needs those is
    not routed here (see `enabled`).
    """
    if config is None:
        return ""
    si = config.get("system_instruction") if isinstance(config, dict) \
        else getattr(config, "system_instruction", None)
    if not si:
        return ""
    if isinstance(si, str):
        return si
    parts = si if isinstance(si, (list, tuple)) else [si]
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        else:
            t = getattr(p, "text", None) or (p.get("text") if isinstance(p, dict) else None)
            if t:
                out.append(str(t))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def _post(body: dict, key: str, timeout_s: float):
    import requests
    return requests.post(
        API_URL,
        headers={
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json=body,
        timeout=timeout_s,
    )


def call(contents, tier: str = SMART, config=None,
         timeout_ms: int = int(DEFAULT_TIMEOUT_S * 1000), key: str = ""):
    """One generation through the Messages API. ClaudeReply, or None.

    None is the answer to every failure, and callers already treat it as
    "fall back", so nothing here needs to raise.
    """
    key = key or api_key()
    if not key:
        print("[Claude] no Anthropic API key is configured")
        return None

    requested = model_for(tier)
    if _cooling(requested):
        return None
    if not _budget_left():
        return None

    body = {
        "model": requested,
        "max_tokens": max_tokens(),
        "messages": [{"role": "user", "content": _blocks(contents)}],
    }
    if not body["messages"][0]["content"]:
        return None
    system = _system(config)
    if system:
        body["system"] = system

    timeout_s = max(MIN_TIMEOUT_S, timeout_ms / 1000.0)
    try:
        resp = _post(body, key, timeout_s)
    except Exception as e:
        print(f"[Claude] {requested}: {type(e).__name__}: {str(e)[:140]}")
        return None

    if resp.status_code != 200:
        msg = ""
        try:
            msg = str(resp.json().get("error", {}).get("message") or resp.text[:140])
        except Exception:
            msg = resp.text[:140]
        rest = _COOLDOWN.get(resp.status_code)
        if resp.status_code in (401, 403):
            # The key is wrong, not the model. Resting it for the session is the
            # difference between one failed call and one per side call.
            rest = rest or 6 * 60 * 60
        if rest:
            _cool(requested, rest)
        print(f"[Claude] {requested}: HTTP {resp.status_code}: {msg[:140]}"
              + (f" — resting it for {int(rest) // 60} minutes" if rest else ""))
        return None

    try:
        data = resp.json()
    except Exception as e:
        print(f"[Claude] {requested}: reply was not JSON: {e}")
        return None

    used = str(data.get("model") or requested)
    if used != requested:
        # Anthropic's safety classifier reroutes a small share of requests to a
        # different model. The answer is still an answer, so it is used and said
        # out loud — invisible rerouting is how a model swap looks like a bug.
        print(f"[Claude] asked for {requested}, {used} answered "
              f"(classifier routing) — using its reply.")

    stop = str(data.get("stop_reason") or "")
    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text").strip()
    usage = data.get("usage") or {}
    _charge(used, int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0))

    if not text:
        print(f"[Claude] {used}: stop_reason={stop or 'unknown'} with no text — "
              f"letting Gemini answer")
        return None
    return ClaudeReply(text, used, requested, stop, usage)


def text(contents, tier: str = SMART, config=None,
         timeout_ms: int = int(DEFAULT_TIMEOUT_S * 1000), key: str = "",
         default: str = "") -> str:
    """`call`, reduced to the reply text. `default` when Claude did not answer."""
    reply = call(contents, tier=tier, config=config, timeout_ms=timeout_ms, key=key)
    if reply is None:
        return default
    return reply.text or default
