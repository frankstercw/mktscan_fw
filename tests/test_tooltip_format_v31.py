from pathlib import Path


def test_dashboard_tooltip_helper_has_three_sections():
    text = (Path(__file__).parents[1] / "dashboard" / "app.py").read_text()
    assert 'f"SOURCE\\n"' in text
    assert 'f"DEFINITION\\n"' in text
    assert 'f"HOW TO INTERPRET\\n"' in text


def test_direct_toggle_help_uses_canonical_tooltip():
    text = (Path(__file__).parents[1] / "dashboard" / "app.py").read_text()
    assert 'help=tooltip_text(' in text
    # Aside from the dataframe normalizer, no legacy direct help should remain.
    assert 'help=(' not in text
