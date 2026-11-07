"""The page's colours: what people read stays readable, and the copies of the accent agree.

The stylesheet holds the colours, but the mark and the terminal can't read its
variables, so they carry their own copies, which a change of scheme can miss.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "rootfs/usr/local/lib/opsbox/manager/static"
CSS = (STATIC / "manager.css").read_text()
LIGHT_PART, DARK_PART = CSS.split("@media (prefers-color-scheme: dark)", 1)
TOKEN = re.compile(r"--([a-z-]+):\s*([^;]+);")

LIGHT = dict(TOKEN.findall(LIGHT_PART))
DARK = {**LIGHT, **dict(TOKEN.findall(DARK_PART.split("\n}\n", 1)[0]))}


def luminance(colour: str) -> float:
    """WCAG relative luminance of #rgb or #rrggbb."""
    digits = colour.lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    channels = [int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two colours."""
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["light", "dark"])
@pytest.mark.parametrize("ink", ["text", "muted", "accent", "down"])
@pytest.mark.parametrize("ground", ["bg", "panel-solid", "sunken"])
def test_words_on_the_page_are_readable(theme: dict[str, str], ink: str, ground: str) -> None:
    """Down is here for the words on a danger button."""
    assert contrast(theme[ink], theme[ground]) >= 4.5


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["light", "dark"])
@pytest.mark.parametrize("symbol", ["up", "warn"])
@pytest.mark.parametrize("ground", ["bg", "panel-solid", "sunken"])
def test_status_symbols_stand_out(theme: dict[str, str], symbol: str, ground: str) -> None:
    """They're graphics, at 3:1: a status's word is in the text colour."""
    assert contrast(theme[symbol], theme[ground]) >= 3


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["light", "dark"])
def test_the_main_button_is_readable_across_its_gradient(theme: dict[str, str]) -> None:
    stops = re.findall(r"#[0-9a-f]{3,6}", theme["accent-grad"])
    assert len(stops) >= 2
    for stop in stops:
        assert contrast(theme["on-accent"], stop) >= 4.5


def test_the_mark_uses_the_accent_in_both_themes() -> None:
    """The favicon can't read the stylesheet's variables."""
    icon = (STATIC / "icon.svg").read_text()
    light, dark = icon.split("@media (prefers-color-scheme: dark)", 1)
    assert f".top {{ stroke: {LIGHT['accent']}; }}" in light
    assert f".top {{ stroke: {DARK['accent']}; }}" in dark


def test_the_terminal_uses_the_page_s_colours() -> None:
    """xterm takes its theme as values, not variables, and the terminal stays dark in both themes."""
    script = (STATIC / "manager.js").read_text()
    assert f'cursor: "{DARK["accent"]}"' in script
    backgrounds = set(re.findall(r'background: "(#[0-9a-f]{6})"', script))
    assert len(backgrounds) == 1
    ground = backgrounds.pop()
    assert f"background: {ground};" in CSS
    assert f"background-color: {ground};" in CSS
