"""Terminal-safe rendering of stored text (card names etc.) for CLI output."""
from __future__ import annotations

import re

# Backslash (the escape character itself), C0 (incl. TAB/CR/LF/ESC/BEL), DEL, C1, and the
# Unicode line/paragraph separators U+2028 / U+2029.
_SPECIAL = re.compile("[\\\\\x00-\x1f\x7f-\x9f  ]")


def _escape(m: re.Match[str]) -> str:
    c = ord(m.group())
    if c == 0x5C:
        return "\\\\"
    return f"\\x{c:02x}" if c < 0x100 else f"\\u{c:04x}"


def term_safe(value: object) -> str:
    """str(value) with every control character shown as a visible ``\\xNN`` / ``\\uNNNN`` escape.

    Each literal backslash is first encoded as ``\\\\``, so the mapping is reversible: a real ESC
    prints ``\\x1b`` while the literal text ``\\x1b`` prints ``\\\\x1b``. Stops stored text from emitting
    escape sequences (screen clear, OSC 52 clipboard) or forging TAB-separated columns / extra rows.
    Other printable text is untouched.
    """
    return _SPECIAL.sub(_escape, str(value))
