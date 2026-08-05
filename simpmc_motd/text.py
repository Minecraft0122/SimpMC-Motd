from __future__ import annotations

import re
from typing import Any

_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def normalize_unicode(value: Any, maximum_length: int | None = None) -> str:
    """Return text that can always be encoded as UTF-8.

    JSON permits escaped lone UTF-16 surrogates even though UTF-8, SQLite and
    AstrBot's JSON transport cannot encode them. Replace only those invalid
    scalar values and preserve all normal Unicode, including Minecraft text.
    """

    text = str(value if value is not None else "")
    text = _SURROGATE_RE.sub("\ufffd", text)
    if maximum_length is not None:
        text = text[: max(0, maximum_length)]
    return text
