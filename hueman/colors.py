"""Colour conversions between CIE xy (what Hue uses) and sRGB (what Qt shows)."""

from __future__ import annotations


def _gamma(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _inv_gamma(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def xy_to_rgb(x: float, y: float, brightness: float = 100.0) -> tuple[int, int, int]:
    """Convert CIE xy plus brightness (0-100) to an 8-bit sRGB triple."""
    y = max(y, 1e-6)
    Y = 1.0
    X = (Y / y) * x
    Z = (Y / y) * (1 - x - y)
    r = X * 3.2404542 - Y * 1.5371385 - Z * 0.4985314
    g = -X * 0.9692660 + Y * 1.8760108 + Z * 0.0415560
    b = X * 0.0556434 - Y * 0.2040259 + Z * 1.0572252
    r, g, b = (max(0.0, v) for v in (r, g, b))
    m = max(r, g, b, 1e-6)
    r, g, b = (_gamma(v / m) for v in (r, g, b))
    scale = 0.25 + 0.75 * max(0.0, min(brightness, 100.0)) / 100.0
    return tuple(int(round(max(0.0, min(1.0, v)) * 255 * scale)) for v in (r, g, b))  # type: ignore[return-value]


def rgb_to_xy(r: int, g: int, b: int) -> tuple[float, float]:
    rl, gl, bl = (_inv_gamma(v / 255.0) for v in (r, g, b))
    X = rl * 0.4124564 + gl * 0.3575761 + bl * 0.1804375
    Y = rl * 0.2126729 + gl * 0.7151522 + bl * 0.0721750
    Z = rl * 0.0193339 + gl * 0.1191920 + bl * 0.9503041
    s = X + Y + Z
    if s <= 0:
        return 0.3127, 0.3290  # D65 white
    return round(X / s, 4), round(Y / s, 4)


def mirek_to_rgb(mirek: int, brightness: float = 100.0) -> tuple[int, int, int]:
    """Approximate the colour of a white light at the given colour temperature."""
    kelvin = 1_000_000 / max(mirek, 1)
    t = kelvin / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * _ln(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * _ln(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
        b = 255.0
    scale = 0.25 + 0.75 * max(0.0, min(brightness, 100.0)) / 100.0
    return tuple(int(max(0, min(255, v)) * scale) for v in (r, g, b))  # type: ignore[return-value]


def _ln(v: float) -> float:
    import math

    return math.log(max(v, 1e-6))


def mirek_to_kelvin(mirek: int) -> int:
    return int(round(1_000_000 / max(mirek, 1)))


def kelvin_to_mirek(kelvin: int) -> int:
    return int(round(1_000_000 / max(kelvin, 1)))


def light_rgb(light: dict) -> tuple[int, int, int] | None:
    """Best-effort current display colour of a light/grouped_light resource, None when off."""
    if not (light.get("on") or {}).get("on"):
        return None
    bri = (light.get("dimming") or {}).get("brightness", 100.0)
    ct = light.get("color_temperature") or {}
    if ct.get("mirek_valid") and ct.get("mirek"):
        return mirek_to_rgb(ct["mirek"], bri)
    xy = (light.get("color") or {}).get("xy")
    if xy:
        return xy_to_rgb(xy["x"], xy["y"], bri)
    return mirek_to_rgb(370, bri)


def action_rgb(action: dict) -> tuple[int, int, int] | None:
    """Display colour for a scene action."""
    if not (action.get("on") or {}).get("on", True):
        return None
    bri = (action.get("dimming") or {}).get("brightness", 100.0)
    xy = (action.get("color") or {}).get("xy")
    if xy:
        return xy_to_rgb(xy["x"], xy["y"], bri)
    mirek = (action.get("color_temperature") or {}).get("mirek")
    return mirek_to_rgb(mirek or 370, bri)
