"""Text normalization shared by terminal and desktop voice modes."""

from __future__ import annotations

import re
import unicodedata


def normalize_spoken_command(text: str) -> str:
    """Repair conservative imperative forms commonly emitted by STT."""
    verbs = {
        "opened": "open",
        "closed": "close",
        "minimized": "minimize",
        "maximized": "maximize",
        "paused": "pause",
        "played": "play",
    }
    return re.sub(
        r"^\s*(opened|closed|minimized|maximized|paused|played)\s+(.+?)\s*[.!]?\s*$",
        lambda match: f"{verbs[match.group(1).casefold()]} {match.group(2)}",
        text,
        flags=re.I,
    )


def speech_safe_text(text: str) -> str:
    """Turn Markdown-formatted model output into natural text for TTS.

    The chat view keeps the original response; this is only for the voice path.
    """
    punctuation = str.maketrans(
        {
            "\u2018": "'",
            "\u2019": "'",
            "\u201c": '"',
            "\u201d": '"',
            "\u2013": "-",
            "\u2014": "-",
            "\u2026": "...",
        }
    )
    normalized = text.translate(punctuation)
    normalized = re.sub(r"```(?:[A-Za-z0-9_+-]+)?\s*", "", normalized)
    normalized = re.sub(r"`([^`]+)`", r"\1", normalized)
    normalized = re.sub(r"!?(?:\[([^\]]+)\]\([^)]+\))", r"\1", normalized)
    normalized = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", normalized)
    normalized = re.sub(r"(?m)^\s*(?:[-+*]|\d+[.)])\s+", "", normalized)
    normalized = normalized.replace("**", "").replace("__", "").replace("*", "")
    without_symbols = "".join(
        character for character in normalized if unicodedata.category(character) not in {"So", "Sk"}
    )
    compact = " ".join(without_symbols.split())
    return re.sub(r"\s+([,.!?;:])", r"\1", compact)
