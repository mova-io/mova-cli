"""Voice-shaped output — make text-agent answers sound right read aloud.

A text agent happily returns Markdown: ``**bold**``, ``- bullet lists``,
``# headings``, ``[links](url)``, fenced ``code``. Fed straight to TTS that
sounds *terrible* — the synth reads "star star", "hash", "open bracket". The
fix is a small, conservative normalizer applied to the text just before
synthesis (the pipeline's ``text_filter`` hook): strip the markup, keep the
words, expand a couple of unspeakable symbols.

It is intentionally lossy-toward-speech and dependency-free. It does **not** try
to be a Markdown parser — it removes the constructs that hurt when spoken and
leaves the prose. Numbers/dates are left for the TTS engine (which voices them
better than a hand-rolled expander would).
"""

from __future__ import annotations

import re

_FENCED_CODE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BOLD_ITALIC = re.compile(r"(\*\*\*|\*\*|\*|___|__|_)(.+?)\1", re.S)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_LIST_MARKER = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+", re.M)
_HR = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$", re.M)
_WHITESPACE = re.compile(r"\s+")

# A tiny, safe set of symbol → spoken-word expansions.
_SYMBOLS = (
    (re.compile(r"\s*&\s*"), " and "),
    (re.compile(r"(?<=\d)\s*%"), " percent"),
    (re.compile(r"\$\s*(\d)"), r"\1 dollars "),  # $5 → 5 dollars
)


def speakify(text: str, *, max_chars: int | None = None) -> str:
    """Normalize Markdown-ish text into a clean string for TTS.

    Pure and idempotent-ish: running it twice yields the same result. Returns an
    empty string for empty/whitespace input. ``max_chars`` caps the spoken length
    (shorter answers = less TTS cost + lower latency): it trims to the last
    sentence boundary that fits, falling back to a hard cut if the first sentence
    already exceeds the cap.
    """
    if not text or not text.strip():
        return ""
    out = _FENCED_CODE.sub(" ", text)  # don't read code blocks aloud
    out = _IMAGE.sub(r"\1", out)
    out = _LINK.sub(r"\1", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _HR.sub(" ", out)
    out = _HEADING.sub("", out)
    out = _BLOCKQUOTE.sub("", out)
    out = _LIST_MARKER.sub("", out)
    out = _BOLD_ITALIC.sub(r"\2", out)
    for pattern, repl in _SYMBOLS:
        out = pattern.sub(repl, out)
    out = _WHITESPACE.sub(" ", out).strip()
    if max_chars is not None and len(out) > max_chars:
        out = _truncate(out, max_chars)
    return out


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def _truncate(text: str, max_chars: int) -> str:
    """Trim to the last whole sentence within ``max_chars`` (else a hard cut)."""
    kept: list[str] = []
    length = 0
    for sentence in _SENTENCE_SPLIT.split(text):
        add = (1 if kept else 0) + len(sentence)
        if length + add > max_chars:
            break
        kept.append(sentence)
        length += add
    if kept:
        return " ".join(kept)
    return text[:max_chars].rstrip()
