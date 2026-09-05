"""
Ollama Client — Local LLM integration for explanation enrichment.

Uses the Ollama REST API (http://localhost:11434) to enrich raw sheet
explanations into polished, TTS-friendly narration scripts.

Gracefully falls back to the raw explanation if Ollama is unavailable.
"""
import json
import logging
import urllib.request
import urllib.error
from typing import Optional

import config

logger = logging.getLogger("TikTokDaemon")


# ---------------------------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------------------------

def enrich_explanation(answer: str, raw_explanation: str) -> str:
    """
    Uses Ollama to rewrite the raw explanation into a short, engaging
    narration script suitable for TTS (max 2–3 sentences, ~10 seconds).

    Returns the enriched string, or `raw_explanation` if Ollama is down.
    """
    if not raw_explanation or not raw_explanation.strip():
        return f"The answer is {answer}."

    prompt = _build_prompt(answer, raw_explanation)

    try:
        response_text = _call_ollama(prompt)
        if response_text:
            enriched = response_text.strip()
            # Ensure it doesn't ramble — cap at roughly 3 sentences
            sentences = _split_sentences(enriched)
            enriched = " ".join(sentences[:3]).strip()
            if enriched:
                logger.info(f"Ollama enriched explanation ({len(enriched)} chars).")
                return enriched
    except Exception as e:
        logger.warning(f"Ollama enrichment skipped ({e}). Using raw explanation.")

    return raw_explanation.strip()


# ---------------------------------------------------------------------------
# PROMPT BUILDER
# ---------------------------------------------------------------------------

def _build_prompt(answer: str, raw_explanation: str) -> str:
    return (
        f"You are a charismatic riddle show narrator.\n"
        f"Rewrite the following riddle explanation as a short, engaging, "
        f"TV-style narration (2-3 sentences maximum, suitable for reading aloud on TikTok).\n"
        f"Do NOT add any greeting, intro, or sign-off. Just output the narration.\n\n"
        f"Answer: {answer}\n"
        f"Raw explanation: {raw_explanation}\n\n"
        f"Narration:"
    )


# ---------------------------------------------------------------------------
# OLLAMA REST API CALL
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str) -> Optional[str]:
    """
    Sends a generation request to the local Ollama REST API.
    Returns the generated text string, or None on failure.
    """
    url = f"{config.OLLAMA_HOST.rstrip('/')}/api/generate"
    payload = json.dumps({
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_predict": config.OLLAMA_MAX_TOKENS,
            "temperature": 0.7,
            "top_p": 0.9,
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return data.get("response", "").strip()
    except urllib.error.URLError as e:
        raise ConnectionError(f"Cannot reach Ollama at {config.OLLAMA_HOST}: {e.reason}") from e


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _split_sentences(text: str):
    """Very simple sentence splitter — splits on '. ', '! ', '? '."""
    import re
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p for p in parts if p]


def is_ollama_available() -> bool:
    """Quick health check — returns True if Ollama API is reachable."""
    try:
        url = f"{config.OLLAMA_HOST.rstrip('/')}/api/tags"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3):
            return True
    except Exception:
        return False

