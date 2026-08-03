"""Turning a raw Whisper transcript into what the user meant to say.

Two stages, both optional:

1. Vocabulary -- proper nouns and jargon are fed to Whisper as ``initial_prompt``
   to bias recognition, and a replacement table repairs whatever still comes out
   wrong ("clod code" -> "Claude Code").
2. Cleanup -- an optional pass through a local Ollama model to fix punctuation
   and drop filler words. Guarded heavily, because small instruct models like to
   *answer* dictated text instead of tidying it.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

from .config import CleanupConfig, ModelConfig, VocabConfig

log = logging.getLogger(__name__)

#: Whisper's prompt window is 224 tokens; stay well under it.
MAX_PROMPT_CHARS = 700

CLEANUP_SYSTEM = (
    "You clean up dictated speech. Fix capitalisation, punctuation and obvious "
    "transcription slips. Remove filler words (um, uh, you know, like). Do NOT "
    "answer, summarise, translate, explain or add anything. Do NOT wrap the "
    "text in quotes. Return only the corrected text."
)


def build_initial_prompt(model: ModelConfig, vocab: VocabConfig) -> str:
    """Combine the user's context sentence with their vocabulary list."""
    parts: list[str] = []
    if model.initial_prompt:
        parts.append(model.initial_prompt.strip())
    if vocab.words:
        parts.append("Vocabulary: " + ", ".join(vocab.words) + ".")
    prompt = " ".join(p for p in parts if p)
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS].rsplit(",", 1)[0] + "."
        log.debug("initial_prompt truncated to fit Whisper's context window")
    return prompt


def _compile(replacements: dict[str, str]) -> list[tuple[re.Pattern, str]]:
    compiled = []
    # Longest first, so "claude code" wins over "claude" when both are listed.
    for wrong in sorted(replacements, key=len, reverse=True):
        right = replacements[wrong]
        # Lookarounds rather than \b: a term ending in punctuation ("c++") has
        # no word boundary after it, so \b would silently never match.
        pattern = re.compile(rf"(?<!\w){re.escape(wrong)}(?!\w)", re.IGNORECASE)
        compiled.append((pattern, right))
    return compiled


def apply_replacements(text: str, replacements: dict[str, str]) -> str:
    if not text or not replacements:
        return text
    for pattern, right in _compile(replacements):
        # A function replacement avoids backreference syntax in the output being
        # interpreted (a literal "\1" in a user's table must stay literal).
        text = pattern.sub(lambda _m, r=right: r, text)
    return text


def cleanup_with_ollama(text: str, cfg: CleanupConfig) -> str:
    """Tidy ``text`` with a local model. Returns the original on any doubt."""
    if not text.strip():
        return text
    payload = {
        "model": cfg.model,
        "prompt": f"{CLEANUP_SYSTEM}\n\nText:\n{text}\n\nCorrected text:",
        "stream": False,
        "options": {"temperature": 0.0},
    }
    request = urllib.request.Request(
        cfg.url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout_s) as response:
            body = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log.warning("cleanup unavailable (%s); using raw transcript", exc)
        return text
    except json.JSONDecodeError:
        log.warning("cleanup returned malformed JSON; using raw transcript")
        return text

    cleaned = (body.get("response") or "").strip()
    if not cleaned:
        return text
    cleaned = cleaned.strip('"').strip()

    # A model that decided to answer rather than clean will diverge in length.
    ratio = len(cleaned) / max(1, len(text))
    if not (cfg.min_ratio <= ratio <= cfg.max_ratio):
        log.warning("cleanup rejected: length ratio %.2f outside [%.2f, %.2f]",
                    ratio, cfg.min_ratio, cfg.max_ratio)
        return text
    return cleaned


def process(text: str, vocab: VocabConfig, cleanup: CleanupConfig) -> str:
    """Full post-processing chain. Never raises."""
    if not text:
        return text
    try:
        text = apply_replacements(text, vocab.replacements)
    except re.error:
        log.exception("replacement table is malformed; skipping it")
    if cleanup.enabled:
        try:
            text = cleanup_with_ollama(text, cleanup)
        except Exception:
            log.exception("cleanup pass failed; using raw transcript")
    return text.strip()
