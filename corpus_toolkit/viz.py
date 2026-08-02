"""Shared chart chrome: the one palette, page shell, and theme machinery every
visualization on the platform uses.

WHY THIS EXISTS. Ten viz templates (nine in executive-regulatory-frameworks, two in
oregon-legislature) each carried a hand-copied palette, and they had already drifted —
`--bg` alone had five values across them. That is the exact failure site.py:13-18 ended
for landing pages, never for charts. This module is the viz-side of the same decision:
one canonical instance, consumed by role, swapped in one place.

THE PALETTE IS VALIDATED, NOT TASTEFUL. The categorical order is a colorblind-safety
mechanism: candidate orderings were enumerated and this one clears every adjacent-pair
gate in both modes (worst adjacent CVD ΔE 9.1 light / 8.4 dark, OKLab x100, target >= 8;
normal-vision 19.6 / 19.3, floor 15 — re-verified with the validator, 2026-08-02).
Consequences that are RULES, not suggestions, for every consumer:

  * Assign slots in order, never cycled; a 9th series folds into "Other" or a facet.
  * Scatter/choropleth/small-multiple forms (all-pairs comparison) cap at THE FIRST
    THREE slots; past three, fold or facet.
  * Three light-mode slots (aqua, yellow, magenta) sit below 3:1 on the light surface:
    the RELIEF RULE applies — ship visible direct labels or a table view. Not optional.
  * One axis, ever. Two measures of different scale = two charts or an indexed base.
  * Text wears ink roles, never a series color; a colored chip beside it carries identity.

Self-contained output is the house idiom: data inlined, no CDN, no external assets —
a page on Pages must render forever without a network. docs/viz.md documents usage.
"""
from __future__ import annotations

import html as _html

# ---------------------------------------------------------------- palette (reference
# instance; see docs/viz.md for provenance and the re-validation command)

CATEGORICAL_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                     "#e87ba4", "#008300", "#4a3aa7", "#e34948")
CATEGORICAL_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500",
                    "#d55181", "#008300", "#9085e9", "#e66767")

# Sequential (magnitude): one hue, light->dark. Ordinal use starts no lighter than
# step 250 on light, no darker than 600 on dark.
SEQUENTIAL_BLUE = ("#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
                   "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281",
                   "#0d366b")

DIVERGING = {"cool": "#2a78d6", "warm": "#e34948",
             "mid_light": "#f0efec", "mid_dark": "#383835"}

# Reserved for state, never for "series 4"; always paired with an icon/label.
STATUS = {"good": "#0ca30c", "warning": "#fab219",
          "serious": "#ec835a", "critical": "#d03b3b"}

CHROME_LIGHT = {"surface": "#fcfcfb", "page": "#f9f9f7", "ink": "#0b0b0b",
                "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
                "axis": "#c3c2b7", "border": "rgba(11,11,11,0.10)"}
CHROME_DARK = {"surface": "#1a1a19", "page": "#0d0d0d", "ink": "#ffffff",
               "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
               "axis": "#383835", "border": "rgba(255,255,255,0.10)"}

DISCLAIMER = ("Non-authoritative. Every figure here derives from mirrored public "
              "records; verify against the authoritative sources linked below before "
              "relying on any of it.")


def _vars(chrome: dict, cats: tuple) -> str:
    lines = [f"  --{k}: {v};" for k, v in chrome.items()]
    lines += [f"  --s{i}: {c};" for i, c in enumerate(cats, 1)]
    return "\n".join(lines)


def viz_css() -> str:
    """The role variables (both modes, toggle-aware) plus the base chart chrome.

    Dark is declared under BOTH scopes: the media query covers the OS setting, the
    data-theme scope covers the toggle, and each must beat the other in its own
    direction (the :not() guard lets a light stamp beat OS-dark)."""
    return f"""
:root {{
  color-scheme: light;
{_vars(CHROME_LIGHT, CATEGORICAL_LIGHT)}
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    color-scheme: dark;
{_vars(CHROME_DARK, CATEGORICAL_DARK)}
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
{_vars(CHROME_DARK, CATEGORICAL_DARK)}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--page); color: var(--ink);
       font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }}
.disclaimer {{ background: #8a6d1a; color: #fff; padding: 6px 16px; font-size: 12.5px; }}
main {{ max-width: 980px; margin: 0 auto; padding: 24px 16px 48px; }}
.eyebrow {{ color: var(--muted); font-size: 12px; letter-spacing: .08em;
            text-transform: uppercase; margin: 8px 0 2px; }}
h1 {{ font-size: 26px; line-height: 1.25; margin: 0 0 6px; }}
.lede {{ color: var(--ink2); max-width: 70ch; margin: 0 0 20px; }}
.panel {{ background: var(--surface); border: 1px solid var(--border);
          border-radius: 10px; padding: 16px; margin: 16px 0; overflow-x: auto; }}
.caveats {{ background: var(--surface); border-left: 3px solid var(--axis);
            border-radius: 0 8px 8px 0; padding: 10px 14px; margin: 16px 0;
            color: var(--ink2); font-size: 13.5px; }}
.caveats h2 {{ font-size: 13px; margin: 0 0 6px; color: var(--ink); }}
.legend {{ display: flex; flex-wrap: wrap; gap: 12px 18px; margin: 8px 0 4px;
           font-size: 13px; color: var(--ink2); }}
.legend .chip {{ display: inline-block; width: 10px; height: 10px; border-radius: 3px;
                 margin-right: 6px; vertical-align: -1px; }}
svg text {{ font: 12px system-ui, -apple-system, "Segoe UI", sans-serif;
            fill: var(--muted); }}
svg .val {{ fill: var(--ink); font-variant-numeric: tabular-nums; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13.5px; }}
th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--grid); }}
th {{ color: var(--muted); font-weight: 600; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.tip {{ position: fixed; pointer-events: none; background: var(--surface);
        border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px;
        font-size: 12.5px; box-shadow: 0 4px 14px rgba(0,0,0,.18); display: none;
        z-index: 10; max-width: 320px; }}
footer {{ color: var(--muted); font-size: 12px; margin-top: 28px;
          border-top: 1px solid var(--grid); padding-top: 12px; }}
footer code {{ font-size: 11px; }}
a {{ color: var(--s1); }}
.themebtn {{ float: right; background: none; border: 1px solid var(--border);
             border-radius: 8px; color: var(--ink2); padding: 4px 10px;
             cursor: pointer; font-size: 12.5px; }}
"""


THEME_JS = """
(function(){
  var K='oregonai-theme', r=document.documentElement,
      s=localStorage.getItem(K);
  if(s) r.setAttribute('data-theme', s);
  document.querySelector('.themebtn').addEventListener('click', function(){
    var cur = r.getAttribute('data-theme') ||
      (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    var next = cur === 'dark' ? 'light' : 'dark';
    r.setAttribute('data-theme', next); localStorage.setItem(K, next);
  });
})();
"""


_SHELL = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>__CSS__</style>
</head><body>
<div class="disclaimer">__DISCLAIMER__</div>
<main>
<button class="themebtn" type="button">theme</button>
<p class="eyebrow">__EYEBROW__</p>
<h1>__TITLE__</h1>
<p class="lede">__LEDE__</p>
<!--BODY-->
<div class="caveats"><h2>Read this before quoting the chart</h2>__CAVEATS__</div>
<footer>Generated __GENERATED__ by an automated build.__SOURCES__</footer>
</main>
<div class="tip" id="tip"></div>
<script>__THEME_JS__</script>
<!--SCRIPT-->
</body></html>
"""


def chart_page(*, title: str, eyebrow: str, lede_html: str, body_html: str,
               caveats_html: str, sources: list[dict] | None = None,
               generated: str = "", script: str = "",
               disclaimer: str = DISCLAIMER) -> str:
    """A self-contained story/chart page in the house idiom.

    `sources`: [{"label", "url", "sha256"?}] — rendered in the footer so every number
    on the page traces to a cited artifact. Raises if a slot survives substitution,
    the same guard as site.py:127-140: publishing literal __TITLE__ is worse than
    failing the build."""
    src_html = ""
    if sources:
        rows = []
        for s in sources:
            h = f" <code>{_html.escape(s['sha256'][:12])}</code>" if s.get("sha256") else ""
            rows.append(f'<a href="{_html.escape(s["url"])}">'
                        f'{_html.escape(s["label"])}</a>{h}')
        src_html = " Sources: " + " · ".join(rows) + "."
    out = (_SHELL
           .replace("__CSS__", viz_css())
           .replace("__THEME_JS__", THEME_JS)
           .replace("__DISCLAIMER__", _html.escape(disclaimer))
           .replace("__TITLE__", _html.escape(title))
           .replace("__EYEBROW__", _html.escape(eyebrow))
           .replace("__LEDE__", lede_html)
           .replace("__CAVEATS__", caveats_html)
           .replace("__GENERATED__", _html.escape(generated) or "at build time")
           .replace("__SOURCES__", src_html)
           .replace("<!--BODY-->", body_html)
           .replace("<!--SCRIPT-->", f"<script>{script}</script>" if script else ""))
    import re
    # Slots always contain a letter; bare runs of underscores are legitimate content —
    # Oregon's budget-bill templates literally read "appropriated to ______", and the
    # first production story about them tripped this guard (a good catch of the wrong
    # thing). Dunder-styled prose like __INIT__ would still trip it; slots are the
    # convention, so that stays the safe side to fail on.
    leftover = re.findall(r"__[A-Z][A-Z0-9_]*__", out)
    if leftover:
        raise ValueError(f"chart_page: unsubstituted slot(s) {sorted(set(leftover))} — "
                         f"refusing to render a page with placeholder chrome")
    return out
