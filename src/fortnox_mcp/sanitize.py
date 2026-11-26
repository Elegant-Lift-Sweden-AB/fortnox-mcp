"""Make product titles safe for the Fortnox article register.

Fortnox silently deletes characters its ``Description`` field does not accept,
so a title that is pushed unsanitised comes back with its meaning quietly
changed ("(±1%)" became "(1%)", a non-breaking hyphen simply vanished). Every title the
server writes to Fortnox goes through :func:`fortnox_safe_title` first, and
:func:`dropped_chars` reports what a title would lose.

The allowed set is empirical: derived from a live article register
(GET /3/articles) and from rows Fortnox rejected or silently mangled on import:

  Description : letters (including å ä ö and friends), digits, space, and
                -  /  ,  ( )  .  +  _  %  =  :  '  &  \\  en-dash  ®  ²  Ω
"""
import re

# Hard cap applied to a sanitised title (Fortnox's own limit has not been measured).
DESCRIPTION_MAX = 200

# Characters Fortnox deletes silently, and what to write instead.
DESC_MAP = {
    '[': '(',            # "ej tillatna tecken" on import
    ']': ')',
    '‑': '-',       # non-breaking hyphen: Fortnox deletes it
    ' ': ' ',       # narrow no-break space: Fortnox deletes it
    ' ': ' ',       # nbsp (defensive)
    '×': 'x',       # multiplication sign
    '±': '+-',      # Fortnox ate it: "(±1%)" became "(1%)"
    '\t': ' ',
}

# Non-alphanumeric characters that survive untouched.
DESC_ALLOWED_EXTRA = set(" -/,().+_%=:'&\\–®²Ω")

# Characters removed on purpose rather than replaced: the deletion is the rule,
# so it is not reported as an unexpected loss.
DESC_DROP = frozenset('"~')

_WHITESPACE_RUN = re.compile(r'\s+')


def _apply_rules(s: str) -> str:
    """Apply the replacement and deliberate-deletion rules, nothing else."""
    for bad, good in DESC_MAP.items():
        s = s.replace(bad, good)
    s = s.replace('V~', 'V AC')          # 500V~ -> 500V AC
    for ch in DESC_DROP:
        s = s.replace(ch, '')
    return s


def _is_allowed(c: str) -> bool:
    return c.isalnum() or c in DESC_ALLOWED_EXTRA


def fortnox_safe_title(s: str, maxlen: int = DESCRIPTION_MAX) -> str:
    """Return ``s`` rewritten so Fortnox stores it unchanged.

    Replacements first, then everything outside the allowed charset is removed,
    whitespace runs collapse to a single space, and the result is capped at
    ``maxlen``. The cap is stripped again: a trailing space would differ from
    what Fortnox stores (it trims them), so a later comparison would see a
    change that isn't there.
    """
    if not s:
        return ''
    s = _apply_rules(s)
    s = ''.join(c for c in s if _is_allowed(c))
    s = _WHITESPACE_RUN.sub(' ', s).strip()
    if maxlen is not None:
        s = s[:maxlen].strip()
    return s


def dropped_chars(s: str) -> list:
    """Return the distinct characters :func:`fortnox_safe_title` deletes outright.

    Replaced characters and the deliberate deletions are excluded — they are
    rules with a known result. What is left is the list of losses that can
    change a title's meaning, in order of first appearance.
    """
    if not s:
        return []
    out = []
    for c in _apply_rules(s):
        if _is_allowed(c) or c in out:
            continue
        out.append(c)
    return out
