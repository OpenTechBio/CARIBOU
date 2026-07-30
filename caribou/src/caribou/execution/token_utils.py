"""
Lightweight token estimation for context-size reporting.

CARIBOU talks to several LLM backends (OpenAI, Anthropic, DeepSeek,
OpenRouter, Ollama), each with its own tokenizer, so there's no single
exact tokenizer that applies across the board. We use a simple
character-based heuristic (~4 characters per token, the commonly cited
average for English text) to give a useful order-of-magnitude estimate
for the UI. This is NOT an exact count and should not be used to enforce
hard context limits.
"""
from __future__ import annotations

from typing import Dict, Iterable

_CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    """Approximate the token count of a string of text."""
    if not text:
        return 0
    return max(1, round(len(text) / _CHARS_PER_TOKEN))


def estimate_message_tokens(message: Dict[str, object]) -> int:
    """Approximate the token count of a single {role, content, ...} message."""
    content = message.get("content", "")
    if not isinstance(content, str):
        content = str(content)
    return estimate_tokens(content)


def estimate_messages_tokens(messages: Iterable[Dict[str, object]]) -> int:
    """Approximate the total token count across a list of messages."""
    return sum(estimate_message_tokens(m) for m in messages)
