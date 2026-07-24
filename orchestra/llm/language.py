"""Language guard — keeps replies in the user's language.

qwen2.5 is a Chinese-origin model family with a well-known failure mode:
under pressure (long prompts, failed sub-tasks, non-English input) it
drifts into Chinese mid-reply. No prompt wording eliminates this on a 7B
model, so the guard has two layers:

1. PROMPT layer — an explicit, per-request language instruction derived
   from the user's actual message (not a static "user's language" phrase
   the model can ignore).
2. CHECK layer — after generation, scan the reply for CJK characters the
   user never typed. If found, the caller retries once with a corrective
   instruction; if it happens again, the caller falls back to the labeled
   join, which is ugly but always in the right language.

Script detection is deliberately coarse (Arabic block vs CJK blocks vs
default-Latin). It answers one question only: "which language should the
reply be in, and did the model leak CJK?" — not general language ID.
"""
from __future__ import annotations

# Unicode ranges. Arabic: base block + supplement + presentation forms.
_ARABIC = (
    ("\u0600", "\u06FF"), ("\u0750", "\u077F"),
    ("\u08A0", "\u08FF"), ("\uFB50", "\uFDFF"), ("\uFE70", "\uFEFF"),
)
# CJK: unified ideographs (+ext A), hiragana/katakana, hangul.
_CJK = (
    ("\u4E00", "\u9FFF"), ("\u3400", "\u4DBF"),
    ("\u3040", "\u30FF"), ("\uAC00", "\uD7AF"),
)


def _count_in(text: str, ranges: tuple[tuple[str, str], ...]) -> int:
    return sum(1 for ch in text if any(lo <= ch <= hi for lo, hi in ranges))


def detect_language(text: str) -> str:
    """Return 'ar', 'cjk', or 'en' (default for Latin and everything else)."""
    arabic = _count_in(text, _ARABIC)
    cjk = _count_in(text, _CJK)
    letters = sum(1 for ch in text if ch.isalpha())
    if letters == 0:
        return "en"
    if arabic / letters > 0.3:
        return "ar"
    if cjk / letters > 0.3:
        return "cjk"
    return "en"


def has_cjk_leak(reply: str, user_message: str) -> bool:
    """True when the reply contains CJK characters the user never typed.

    A handful of characters (a quoted name, one stray token) is tolerated;
    the drift we're guarding against is sentences, not single glyphs.
    """
    if _count_in(user_message, _CJK) > 0:
        return False                      # user writes CJK — it's their language
    return _count_in(reply, _CJK) >= 5


_NAMES = {"ar": "Arabic", "en": "English", "cjk": "the user's language"}


def language_instruction(user_message: str) -> str:
    """A per-request instruction naming the exact reply language.

    Naming the language explicitly ("Reply in Arabic") measurably beats the
    generic "reply in the user's language" on small models — the generic
    phrase is exactly where qwen drifts.
    """
    lang = detect_language(user_message)
    name = _NAMES[lang]
    return (
        f"Reply ONLY in {name}. Every sentence must be in {name}. "
        "Do NOT use Chinese, Japanese, or Korean under any circumstances "
        "unless the user's own message is written in that script."
    )


CORRECTION = (
    "Your previous reply mixed in Chinese text. Rewrite the ENTIRE reply "
    "in the user's language only. Do not include a single Chinese, "
    "Japanese, or Korean character."
)
