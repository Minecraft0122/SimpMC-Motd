from __future__ import annotations

import base64
import binascii
import html
import re
import struct
from typing import Any

from ..constants import (
    MAX_FAVICON_BYTES,
    MINECRAFT_COLOR_CODES,
    MINECRAFT_NAMED_COLORS,
)
from ..text import normalize_unicode

COLOR_CODE_RE = re.compile(r"§.")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_COMPONENT_NODES = 256
MAX_COMPONENT_CHARS = 8192
MAX_COMPONENT_DEPTH = 16


def sanitize_component(value: Any) -> Any:
    """Keep only bounded fields used by the status card.

    Minecraft status descriptions are untrusted recursive JSON.  Sanitizing once
    prevents recursion bombs and avoids persisting hover/click events or player
    data that the renderer never consumes.
    """

    remaining_nodes = MAX_COMPONENT_NODES
    remaining_chars = MAX_COMPONENT_CHARS

    def visit(item: Any, depth: int) -> Any:
        nonlocal remaining_nodes, remaining_chars
        if depth > MAX_COMPONENT_DEPTH or remaining_nodes <= 0:
            return ""
        remaining_nodes -= 1
        if item is None:
            return ""
        if isinstance(item, str):
            text = normalize_unicode(item, remaining_chars)
            remaining_chars -= len(text)
            return text
        if isinstance(item, list):
            output = []
            for child in item:
                if remaining_nodes <= 0 or remaining_chars <= 0:
                    break
                output.append(visit(child, depth + 1))
            return output
        if isinstance(item, dict):
            output: dict[str, Any] = {}
            for key in ("text", "translate"):
                if key in item and remaining_chars > 0:
                    output[key] = visit(str(item.get(key) or ""), depth + 1)
            color = item.get("color")
            if isinstance(color, str):
                output["color"] = normalize_unicode(color, 32)
            for key in ("bold", "italic", "underlined", "strikethrough"):
                if isinstance(item.get(key), bool):
                    output[key] = item[key]
            for key in ("with", "extra"):
                children = item.get(key)
                if isinstance(children, list) and remaining_nodes > 0:
                    output[key] = visit(children, depth + 1)
            return output
        return visit(str(item), depth + 1)

    return visit(value, 0)


def _component_to_plain(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_component_to_plain(item) for item in value)
    if isinstance(value, dict):
        pieces: list[str] = []
        text = value.get("text")
        if text is not None:
            pieces.append(str(text))
        if "translate" in value and not pieces:
            pieces.append(str(value.get("translate") or ""))
        for item in value.get("with", []) or []:
            pieces.append(_component_to_plain(item))
        for item in value.get("extra", []) or []:
            pieces.append(_component_to_plain(item))
        return "".join(pieces)
    return str(value)


def component_to_plain(value: Any) -> str:
    return _component_to_plain(sanitize_component(value))


def clean_motd(value: Any) -> str:
    text = COLOR_CODE_RE.sub("", component_to_plain(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip() or "Minecraft Server"


def safe_minecraft_color(value: Any) -> str:
    color = normalize_unicode(value).strip().lower()
    if color in MINECRAFT_NAMED_COLORS:
        return MINECRAFT_NAMED_COLORS[color]
    if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return color
    return ""


def style_from_state(state: dict[str, Any]) -> str:
    styles: list[str] = []
    color = safe_minecraft_color(state.get("color"))
    if color:
        styles.append(f"color:{color}")
    if state.get("bold"):
        styles.append("font-weight:700")
    if state.get("italic"):
        styles.append("font-style:italic")
    decorations = []
    if state.get("underlined"):
        decorations.append("underline")
    if state.get("strikethrough"):
        decorations.append("line-through")
    if decorations:
        styles.append(f"text-decoration:{' '.join(decorations)}")
    return ";".join(styles)


def wrap_styled_text(text: str, state: dict[str, Any]) -> str:
    text = normalize_unicode(text)
    if not text:
        return ""
    escaped = html.escape(text, quote=True)
    style = style_from_state(state)
    if not style:
        return escaped
    return f'<span style="{style}">{escaped}</span>'


def legacy_text_to_html(
    text: str,
    inherited_state: dict[str, Any] | None = None,
) -> str:
    text = normalize_unicode(text)
    state: dict[str, Any] = dict(inherited_state or {})
    chunks: list[str] = []
    buffer: list[str] = []
    index = 0

    def flush() -> None:
        if buffer:
            chunks.append(wrap_styled_text("".join(buffer), state))
            buffer.clear()

    while index < len(text):
        char = text[index]
        if char == "§" and index + 1 < len(text):
            code = text[index + 1].lower()
            flush()
            if code in MINECRAFT_COLOR_CODES:
                state = {"color": MINECRAFT_COLOR_CODES[code]}
            elif code == "l":
                state["bold"] = True
            elif code == "o":
                state["italic"] = True
            elif code == "n":
                state["underlined"] = True
            elif code == "m":
                state["strikethrough"] = True
            elif code == "r":
                state = dict(inherited_state or {})
            index += 2
            continue
        buffer.append(char)
        index += 1
    flush()
    return "".join(chunks)


def _component_to_html(
    value: Any,
    inherited_state: dict[str, Any] | None = None,
) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return legacy_text_to_html(value, inherited_state)
    if isinstance(value, list):
        return "".join(_component_to_html(item, inherited_state) for item in value)
    if isinstance(value, dict):
        state: dict[str, Any] = dict(inherited_state or {})
        if "color" in value:
            color = safe_minecraft_color(value.get("color"))
            if color:
                state["color"] = color
        for key in ("bold", "italic", "underlined", "strikethrough"):
            if key in value and isinstance(value[key], bool):
                state[key] = value[key]

        pieces: list[str] = []
        if "text" in value:
            pieces.append(legacy_text_to_html(str(value.get("text") or ""), state))
        elif "translate" in value:
            pieces.append(wrap_styled_text(str(value.get("translate") or ""), state))
        for item in value.get("with", []) or []:
            pieces.append(_component_to_html(item, state))
        for item in value.get("extra", []) or []:
            pieces.append(_component_to_html(item, state))
        return "".join(pieces)
    return legacy_text_to_html(str(value), inherited_state)


def component_to_html(
    value: Any,
    inherited_state: dict[str, Any] | None = None,
) -> str:
    return _component_to_html(sanitize_component(value), inherited_state)


def motd_to_html(value: Any) -> str:
    rendered = component_to_html(value)
    return rendered.strip() or html.escape("Minecraft Server", quote=True)


def safe_favicon(value: Any) -> str | None:
    prefix = "data:image/png;base64,"
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    encoded = value[len(prefix) :]
    if len(encoded) > ((MAX_FAVICON_BYTES + 2) // 3) * 4 + 4:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    if (
        len(decoded) < 24
        or len(decoded) > MAX_FAVICON_BYTES
        or not decoded.startswith(PNG_SIGNATURE)
        or decoded[12:16] != b"IHDR"
    ):
        return None
    width, height = struct.unpack(">II", decoded[16:24])
    if (width, height) != (64, 64):
        return None
    return value
