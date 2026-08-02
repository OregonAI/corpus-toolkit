"""The chart chrome's promises: one palette, guarded rendering, both modes present."""
import pytest

from corpus_toolkit import viz


def test_a_page_with_an_unfilled_slot_refuses_to_render():
    with pytest.raises(ValueError):
        viz.chart_page(title="T", eyebrow="E", lede_html="L",
                       body_html="<p>__OOPS__</p>", caveats_html="C")


def test_the_page_is_self_contained_and_carries_both_modes_and_the_disclaimer():
    page = viz.chart_page(title="Audit mix", eyebrow="oregon-audits", lede_html="lede",
                          body_html="<svg></svg>", caveats_html="<p>partial year</p>",
                          sources=[{"label": "reports", "url": "https://x", "sha256": "ab" * 32}],
                          generated="2026-08-02")
    assert "http" not in page.split("</style>")[0].split("<style>")[1]  # no external CSS
    assert "prefers-color-scheme: dark" in page and 'data-theme="dark"' in page
    assert "Non-authoritative" in page and "partial year" in page
    assert "abababababab" in page  # source hash surfaces


def test_the_categorical_order_is_fixed_and_eight_slots_in_both_modes():
    # The ORDER is the colorblind-safety mechanism; a reorder must be a deliberate,
    # re-validated change, so the exact sequence is pinned here.
    assert viz.CATEGORICAL_LIGHT == ("#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                                     "#e87ba4", "#008300", "#4a3aa7", "#e34948")
    assert len(viz.CATEGORICAL_DARK) == 8
    css = viz.viz_css()
    for i in range(1, 9):
        assert f"--s{i}:" in css
