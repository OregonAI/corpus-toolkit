# corpus_toolkit.viz — the shared chart chrome

One palette, one page shell, one theme mechanism for every visualization on the
platform. Before this module, ten viz templates carried hand-copied palettes that had
already drifted (`--bg` alone had five values). New charts use this; existing pages
migrate opportunistically when next touched.

## What it provides

- **`viz_css()`** — role variables for both modes (`--surface --page --ink --ink2
  --muted --grid --axis --border --s1..--s8`), toggle-aware dark scoping, and the base
  chart chrome (panels, legend chips, caveat blocks, tooltip, table, footer).
- **`chart_page(...)`** — a fully self-contained HTML page: gold non-authoritative bar,
  eyebrow/title/lede, your chart body, a mandatory caveats block, a sources footer
  (label + URL + optional content hash so every number traces), theme toggle. Refuses
  to render if any `__SLOT__` survives substitution — the same guard as `site.py`.
- Palette constants: `CATEGORICAL_LIGHT/DARK` (8 slots), `SEQUENTIAL_BLUE`,
  `DIVERGING`, `STATUS`, `CHROME_LIGHT/DARK`.

## The rules the palette imposes (they are safety mechanisms, not taste)

1. **Slots in order, never cycled.** A 9th series folds into "Other" or a facet.
2. **All-pairs forms cap at three slots** (scatter, choropleth, small multiples).
3. **Relief rule:** light-mode slots 3/4/5 (aqua, yellow, magenta) are sub-3:1 on the
   light surface — any chart using them ships visible direct labels or a table view.
4. **One axis.** Two measures of different scale = two charts or an indexed base.
5. Text wears ink roles, never series colors; a chip beside it carries identity.
6. Status colors are reserved for state, shipped with icon + label, never as series.
7. Self-contained output: data inlined, no CDN, no fetch at view time.

## Provenance / re-validation

The palette is the dataviz reference instance, re-validated 2026-08-02 (worst adjacent
CVD ΔE 9.1 light / 8.4 dark, normal-vision 19.6 / 19.3 against surfaces
`#fcfcfb`/`#1a1a19`). If any hex ever changes, re-run the validator in both modes
against both surfaces before merging — the ordering is what makes the palette
colorblind-safe, and it was chosen by enumeration, not by eye.
