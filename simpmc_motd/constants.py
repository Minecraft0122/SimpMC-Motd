from __future__ import annotations

from datetime import timedelta, timezone

PLUGIN_ID = "simpmc_motd"
PLUGIN_NAME = "SimpMC-Motd"
PLUGIN_VERSION = "2.0.0"
RENDER_CACHE_VERSION = "7"
DISPLAY_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

MAX_STATUS_PACKET_BYTES = 2 * 1024 * 1024
MAX_FAVICON_BYTES = 1024 * 1024
MAX_PLAYER_COUNT = 2_147_483_647
DEFAULT_RENDER_CACHE_ENTRIES = 256

MINECRAFT_COLOR_CODES = {
    "0": "#000000",
    "1": "#0000aa",
    "2": "#00aa00",
    "3": "#00aaaa",
    "4": "#aa0000",
    "5": "#aa00aa",
    "6": "#ffaa00",
    "7": "#aaaaaa",
    "8": "#555555",
    "9": "#5555ff",
    "a": "#55ff55",
    "b": "#55ffff",
    "c": "#ff5555",
    "d": "#ff55ff",
    "e": "#ffff55",
    "f": "#ffffff",
}

MINECRAFT_NAMED_COLORS = {
    "black": "#000000",
    "dark_blue": "#0000aa",
    "dark_green": "#00aa00",
    "dark_aqua": "#00aaaa",
    "dark_red": "#aa0000",
    "dark_purple": "#aa00aa",
    "gold": "#ffaa00",
    "gray": "#aaaaaa",
    "dark_gray": "#555555",
    "blue": "#5555ff",
    "green": "#55ff55",
    "aqua": "#55ffff",
    "red": "#ff5555",
    "light_purple": "#ff55ff",
    "yellow": "#ffff55",
    "white": "#ffffff",
}
