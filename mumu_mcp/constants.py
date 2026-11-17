"""Android/ADB constants that the tools validate against."""

from __future__ import annotations

KEYCODES: dict[str, int] = {
    "home": 3,
    "back": 4,
    "call": 5,
    "endcall": 6,
    "menu": 82,
    "up": 19,
    "down": 20,
    "left": 21,
    "right": 22,
    "center": 23,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "camera": 27,
    "clear": 28,
    "tab": 61,
    "space": 62,
    "enter": 66,
    "del": 67,
    "backspace": 67,
    "forward_del": 112,
    "mute": 164,
    "media_play": 126,
    "media_stop": 86,
    "media_next": 87,
    "media_previous": 88,
    "search": 84,
    "settings": 176,
    "info": 165,
    "wakeup": 224,
    "sleep": 223,
    "app_switch": 187,
    "recent": 187,
    "voice": 231,
}

# MuMu toolbar functions exposed by `control tool func --name`.
TOOLBAR_FUNCS = (
    "rotate",
    "go_home",
    "go_back",
    "top_most",
    "fullscreen",
    "shake",
    "screenshot",
    "volume_up",
    "volume_down",
    "volume_mute",
)

# MuMu settings keys that Claude/Cursor agents ask for, with the accepted range.
RESOLUTION_LIMITS = {"width": (380, 4096), "height": (380, 4096), "dpi": (10, 960)}
CPU_RANGE = (1, 32)
RAM_RANGE = (0.75, 16.0)

# Common presets, because "1440x2560:640" is not something a model should guess.
RESOLUTION_PRESETS: dict[str, tuple[int, int, int]] = {
    "phone": (1440, 2560, 640),
    "phone-hd": (1080, 1920, 480),
    "phone-hd+": (1080, 2400, 440),
    "tablet": (1600, 2560, 320),
    "tablet-hd": (1200, 1920, 240),
    "widescreen": (1920, 1080, 280),
    "widescreen-2k": (2560, 1440, 360),
    "widescreen-4k": (3440, 1440, 480),
    "square": (1080, 1080, 320),
}

BOOT_PROP = "sys.boot_completed"
