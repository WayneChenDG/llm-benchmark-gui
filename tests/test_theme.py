"""主题层一致性：两套主题键集相同、nav 令牌齐备且对比度达标、切换为原地更新。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_benchmark_app import ui_theme


def _contrast(fg: str, bg: str) -> float:
    def lum(hex_str: str) -> float:
        r, g, b = (int(hex_str[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
        chans = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                 for c in (r, g, b)]
        return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def test_theme_keys_are_navy_and_light():
    assert set(ui_theme.theme_keys()) == {"navy", "light"}
    assert ui_theme.DEFAULT_THEME in ui_theme.THEMES


def test_palettes_have_identical_key_sets():
    key_sets = [set(palette) for palette in ui_theme.THEMES.values()]
    assert all(ks == key_sets[0] for ks in key_sets), "两套主题令牌键必须完全一致"


def test_nav_tokens_meet_text_contrast():
    for name, palette in ui_theme.THEMES.items():
        for fg, bg in (("nav_fg", "nav_bg"), ("nav_fg_muted", "nav_bg"),
                       ("nav_fg", "nav_bg_active"), ("nav_fg_muted", "nav_bg_active")):
            ratio = _contrast(palette[fg], palette[bg])
            assert ratio >= 4.5, f"{name}: {fg} on {bg} = {ratio:.2f} < 4.5"


def test_set_theme_updates_tokens_in_place():
    original = ui_theme.ACTIVE_THEME
    tokens_id = id(ui_theme.TOKENS)
    try:
        ui_theme.set_theme("light")
        assert ui_theme.ACTIVE_THEME == "light"
        assert ui_theme.TOKENS["nav_bg"] == ui_theme.THEMES["light"]["nav_bg"]
        assert id(ui_theme.TOKENS) == tokens_id, "TOKENS 必须原地更新（引用不失效）"
    finally:
        ui_theme.set_theme(original)


def test_set_theme_rejects_unknown_name():
    original = ui_theme.ACTIVE_THEME
    try:
        try:
            ui_theme.set_theme("no-such-theme")
        except KeyError:
            pass
        else:
            raise AssertionError("未知主题名应抛 KeyError")
        assert ui_theme.ACTIVE_THEME == original
    finally:
        ui_theme.set_theme(original)
