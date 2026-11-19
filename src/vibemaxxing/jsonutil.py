"""JSON from outside, parsed so that no input can crash the caller.

`json.loads` on a deeply nested document raises `RecursionError` on Python before 3.14 (and the
decoder's recursion is not ours to bound), which is not a `ValueError`. Everything the bridge reads
from a vendor, from the model's session or from disk goes through `loads`, which refuses a document
nested deeper than ``MAX_DEPTH`` before the decoder sees it, and turns every decoder failure into a
`ValueError`.
"""

from __future__ import annotations

import json
from typing import Final

MAX_DEPTH: Final = 64


def depth(text: str, limit: int = MAX_DEPTH) -> int:
    """The deepest nesting of ``[`` and ``{`` outside strings; stops counting past ``limit``."""
    deepest = level = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            level += 1
            if level > deepest:
                deepest = level
                if deepest > limit:
                    return deepest
        elif char in "]}":
            level -= 1
    return deepest


def loads(text: str | bytes) -> object:
    """``json.loads`` with a depth bound; ValueError for anything it cannot take."""
    raw = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text
    if depth(raw) > MAX_DEPTH:
        raise ValueError("JSON nested too deeply")
    try:
        return json.loads(raw)
    except RecursionError:
        raise ValueError("JSON nested too deeply") from None
