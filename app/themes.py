"""Design-token engine for the Sentinel Media UI.

Themes live as DATA in ``theme_tokens.json`` (palettes + intensity tokens),
not as hand-written CSS. This module generates the ``:root`` / ``[data-theme]``
/ ``[data-fx]`` custom-property blocks from that data at serve time, hot-
reloading whenever the file changes. Editing the JSON restyles every surface
(APK, Windows desktop, in-Telegram) with no component-code changes — the seed
for a cross-pillar "Sitebuilder".

>>> css = render_theme_css(load_tokens())
>>> '--surface: linear-gradient' in css
True
>>> ':root[data-theme="obsidian"]' in css
True
>>> ':root[data-fx="bold"]' in css
True
"""

from __future__ import annotations

import json
import os
import threading

_PATH = os.path.join(os.path.dirname(__file__), "theme_tokens.json")
_lock = threading.Lock()
_cache: dict = {"mtime": None, "data": None}

# Derived custom properties — constant across palettes (they reference the
# per-palette vars), emitted once in :root.
_DERIVED = [
    ("link", "var(--accent)"),
    ("button", "var(--accent)"),
    ("section", "var(--surface-1)"),
    ("card", "var(--surface-1)"),
    ("surface", "linear-gradient(var(--metal-angle), var(--surface-2) 0%, var(--surface-1) 100%)"),
    ("accent-soft", "rgba(var(--accent-rgb), 0.12)"),
    ("accent-line", "rgba(var(--accent-rgb), 0.55)"),
]


def load_tokens() -> dict:
    """Return parsed tokens, re-reading the file only when its mtime changes."""
    try:
        mt = os.path.getmtime(_PATH)
    except OSError:
        mt = None
    with _lock:
        if _cache["data"] is None or _cache["mtime"] != mt:
            with open(_PATH, encoding="utf-8") as f:
                _cache["data"] = json.load(f)
            _cache["mtime"] = mt
        return _cache["data"]


def save_tokens(data: dict) -> dict:
    """Validate + atomically persist a new token set, then bust the cache.

    Minimal validation only — this is owner-gated and single-user."""
    if not isinstance(data, dict) or "palettes" not in data or "intensities" not in data:
        raise ValueError("tokens must include 'palettes' and 'intensities'")
    if not data["palettes"] or not data["intensities"]:
        raise ValueError("'palettes' and 'intensities' must be non-empty")
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _PATH)
    with _lock:
        _cache["data"] = None
        _cache["mtime"] = None
    return load_tokens()


def defaults(tokens: dict) -> tuple[str, str]:
    d = tokens.get("defaults", {})
    return d.get("theme", "chrome"), d.get("fx", "bold")


def _emit(pairs, indent: str = "  ") -> str:
    return "".join(f"{indent}--{k}: {v};\n" for k, v in pairs)


def render_theme_css(tokens: dict) -> str:
    """Generate the full theme CSS (`:root` + per-theme + per-intensity)."""
    palettes = tokens["palettes"]
    intensities = tokens["intensities"]
    consts = tokens["constants"]
    default_theme = defaults(tokens)[0]
    if default_theme not in palettes:
        default_theme = next(iter(palettes))
    # Pre-boot intensity fallback: prefer "refined", else any.
    base_fx = intensities.get("refined") or next(iter(intensities.values()))

    def palette_pairs(p):
        return [(k, v) for k, v in p.items() if k != "name"]

    blocks = []

    root = [":root {", "  color-scheme: dark;", "",
            "  /* intensity (pre-boot fallback; [data-fx] overrides) */"]
    root.append(_emit(base_fx.items()).rstrip("\n"))
    root.append(f"  --metal-angle: {consts['metal-angle']};")
    root.append("")
    root.append(f"  /* palette: {palettes[default_theme].get('name', default_theme)} (default) */")
    root.append(_emit(palette_pairs(palettes[default_theme])).rstrip("\n"))
    for k in ("destructive", "success", "warn"):
        if k in consts:
            root.append(f"  --{k}: {consts[k]};")
    root.append("")
    root.append("  /* derived */")
    root.append(_emit(_DERIVED).rstrip("\n"))
    root.append("}")
    blocks.append("\n".join(root))

    for pid, p in palettes.items():
        if pid == default_theme:
            continue
        blocks.append(f':root[data-theme="{pid}"] {{\n{_emit(palette_pairs(p))}}}')

    for iid, intensity in intensities.items():
        blocks.append(f':root[data-fx="{iid}"] {{\n{_emit(intensity.items())}}}')

    return "\n".join(blocks) + "\n"


def swatches_js(tokens: dict) -> str:
    """JSON array consumed by the Appearance picker's swatch previews."""
    arr = [{"id": pid, "name": p.get("name", pid), "bg": p["bg"],
            "surf": p["surface-2"], "accent": p["accent"]}
           for pid, p in tokens["palettes"].items()]
    return json.dumps(arr)
