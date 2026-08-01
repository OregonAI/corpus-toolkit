"""The shared landing-page shell every corpus renders into.

    from corpus_toolkit.site import Page, Tile, build

    build(Page(
        config=config_mod.load("_meta/corpus.yml"),
        repo="oregon-audits",
        title="Oregon Audits — Secretary of State audit reports",
        ...
        tiles=[Tile("Audit reports", "242", "2020 to present")],
    ))

WHY THIS LIVES IN THE TOOLKIT. Three corpora had grown their own `src/build_site.py` — 220,
247 and 265 lines — and every one carried its own copy of the same theme-aware CSS, the same
tile markup, the same theme-toggle script, and the same corpus-index.json emission. Five more
corpora needed a page. Eight copies of one stylesheet do not stay identical, and the drift is
invisible: nobody diffs two repositories' `<style>` blocks, so the pages simply stop looking
like one platform.

WHAT A CORPUS STILL OWNS, because these are the parts that should differ: its headline and
lede, its tiles, its prose sections, and any extra files it publishes. The shell owns
chrome and contracts, not content.

THE TWO CONTRACTS THIS FILE KEEPS, both of which fail in someone else's repository:

  1. `corpus-index.json` IS EMITTED AT THE SITE ROOT. It maps id -> [title, doc_type, path],
     it is how a sibling corpus resolves a citation INTO this one, and the org landing page
     reads it for live document counts. A corpus that deploys Pages itself CANNOT also call
     the reusable publish-index workflow — two workflows deploying one Pages site fight over
     the `pages` concurrency group — so the page builder has to own it. Dropping it breaks
     resolution somewhere else, silently, and the corpus keeps serving perfectly.

  2. IT IS BUILT AT DEPLOY TIME AND NEVER COMMITTED. A committed index is a generated file
     that can fall behind its own corpus, and that failure also surfaces elsewhere — as a
     resolver returning stale titles and paths.

`.nojekyll` is written for a third reason of the same kind: without it GitHub Pages runs
Jekyll, which silently drops any file or directory beginning with an underscore.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

PLATFORM_URL = "https://oregonai.github.io/"


@dataclass(frozen=True)
class Tile:
    """One statistic. `value` is pre-formatted — the caller knows whether it is a count, a
    percentage or a word like "referenced", and guessing here would mangle one of them."""
    label: str
    value: str
    sub: str = ""


@dataclass(frozen=True)
class Section:
    """A prose block. `body_html` is emitted verbatim, so it is the caller's job to escape
    anything derived from data rather than written by hand."""
    heading: str
    body_html: str


@dataclass
class Page:
    config: object                      # corpus_toolkit.config.CorpusConfig
    repo: str                           # repository name, e.g. "oregon-audits"
    title: str                          # <title> and the browser tab
    description: str                    # <meta name="description">
    eyebrow: str                        # small caps line above the headline
    headline: str                       # <h1> — a sentence, not the corpus name
    lede_html: str                      # one paragraph under the headline
    disclaimer: str                     # the gold non-authoritative bar
    tiles: list[Tile] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    footer_note: str = ""               # first footer line; the platform line is automatic
    extra_files: list[Path] = field(default_factory=list)   # copied into site/ verbatim
    repo_root: Path | None = None
    site_dir: Path | None = None
    mcp_url: str | None = None          # defaults to the platform's path convention

    def __post_init__(self):
        if self.repo_root is None:
            self.repo_root = Path(getattr(self.config, "root", "."))
        if self.site_dir is None:
            self.site_dir = self.repo_root / "site"
        if self.mcp_url is None:
            self.mcp_url = f"https://oregonai.morficflux.com/{self.repo}/mcp"


def esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _tiles_html(tiles: list[Tile]) -> str:
    return "\n".join(
        f'<div class="tile"><div class="num">{esc(t.value)}</div>'
        f'<div class="lbl">{esc(t.label)}</div>'
        + (f'<div class="sub">{esc(t.sub)}</div>' if t.sub else "")
        + "</div>"
        for t in tiles)


def _sections_html(sections: list[Section]) -> str:
    return "\n".join(
        f'  <section>\n    <h2>{esc(s.heading)}</h2>\n{s.body_html}\n  </section>'
        for s in sections)


def build_corpus_index(page: Page) -> str:
    """Write corpus-index.json into the site directory. See this module's docstring — this
    is a cross-repository contract, not a convenience."""
    from corpus_toolkit.index import build_index

    index = build_index(page.config)
    out = page.site_dir / "corpus-index.json"
    out.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    return f"{index['n_documents']:,} documents, {out.stat().st_size / 1024:.0f} KiB"


def render(page: Page) -> str:
    """Render the page, and REFUSE to return one with an unsubstituted placeholder.

    A leftover `__MCP__` renders as the literal text `__MCP__` on a page that otherwise
    looks finished — no error, no blank space, just a published page quietly telling an
    agent the wrong endpoint. Every failure mode of a string-substitution template is this
    one, so it is checked rather than trusted."""
    html = (_substitute(page))
    leftover = sorted(set(re.findall(r"__[A-Z_]{2,}__|<!--[A-Z]+-->", html)))
    if leftover:
        raise ValueError(
            f"landing page has unsubstituted placeholder(s): {', '.join(leftover)}. "
            f"Every one is a slot the shell defines and this render did not fill.")
    return html


def _substitute(page: Page) -> str:
    return (TEMPLATE
            .replace("<!--TILES-->", _tiles_html(page.tiles))
            .replace("<!--SECTIONS-->", _sections_html(page.sections))
            .replace("__TITLE__", esc(page.title))
            .replace("__DESCRIPTION__", esc(page.description))
            .replace("__DISCLAIMER__", esc(page.disclaimer))
            .replace("__EYEBROW__", esc(page.eyebrow))
            .replace("__HEADLINE__", esc(page.headline))
            .replace("__LEDE__", page.lede_html)
            .replace("__FOOTER_NOTE__", page.footer_note or
                     "Unofficial and non-authoritative; not affiliated with the State of "
                     "Oregon.")
            .replace("__REPO_URL__", f"https://github.com/OregonAI/{page.repo}")
            .replace("__MCP__", esc(page.mcp_url))
            .replace("__PLATFORM__", PLATFORM_URL)
            .replace("__TODAY__", date.today().isoformat()))


def build(page: Page) -> dict:
    """Write the whole site directory. Returns what was written, for the caller to print."""
    page.site_dir.mkdir(parents=True, exist_ok=True)
    (page.site_dir / "index.html").write_text(render(page), encoding="utf-8")

    # Without this, Pages runs Jekyll and silently drops anything starting with `_`.
    (page.site_dir / ".nojekyll").write_text("", encoding="utf-8")

    copied = []
    llms = page.repo_root / "llms.txt"
    for src in ([llms] if llms.exists() else []) + list(page.extra_files):
        if not Path(src).exists():
            # Named rather than skipped in silence: an absent visualisation renders as a
            # dead link on a page that otherwise looks finished.
            copied.append(f"MISSING {Path(src).name}")
            continue
        shutil.copyfile(src, page.site_dir / Path(src).name)
        copied.append(Path(src).name)

    return {"index": build_corpus_index(page), "copied": copied,
            "site": str(page.site_dir)}


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<meta name="description" content="__DESCRIPTION__">
<style>
  :root{--bg:#f6f7f9;--panel:#fcfcfb;--ink:#0b0b0b;--muted:#52514e;--line:#e4e8ee;
    --accent:#1f6feb;--accent-ink:#0b4bc0;--gold:#8a6d1f;
    --shadow:0 1px 2px rgba(20,25,40,.06),0 8px 30px rgba(20,25,40,.07)}
  @media (prefers-color-scheme:dark){:root{--bg:#0e1116;--panel:#1a1a19;--ink:#fff;--muted:#c3c2b7;--line:#232a33;
    --accent:#5a9bff;--accent-ink:#8fbaff;--gold:#d9b45a;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 34px rgba(0,0,0,.45)}}
  :root[data-theme="light"]{--bg:#f6f7f9;--panel:#fcfcfb;--ink:#0b0b0b;--muted:#52514e;--line:#e4e8ee;--accent:#1f6feb;--accent-ink:#0b4bc0;--gold:#8a6d1f}
  :root[data-theme="dark"]{--bg:#0e1116;--panel:#1a1a19;--ink:#fff;--muted:#c3c2b7;--line:#232a33;--accent:#5a9bff;--accent-ink:#8fbaff;--gold:#d9b45a}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
  a{color:var(--accent-ink);text-decoration:none} a:hover{text-decoration:underline}
  .wrap{max-width:960px;margin:0 auto;padding:0 22px}
  .disc{background:var(--gold);color:#1a1400;font-size:13px;text-align:center;padding:7px 14px;font-weight:600}
  header{padding:60px 0 26px;border-bottom:1px solid var(--line)}
  .eyebrow{text-transform:uppercase;letter-spacing:.14em;font-size:12px;color:var(--muted);font-weight:700;margin-bottom:14px}
  h1{font-size:clamp(28px,5vw,44px);line-height:1.08;margin:0 0 16px;letter-spacing:-.02em;font-weight:800;text-wrap:balance}
  .lede{font-size:19px;color:var(--muted);max-width:64ch;margin:0}
  .cta{display:flex;flex-wrap:wrap;gap:12px;margin-top:26px}
  .btn{display:inline-flex;align-items:center;gap:8px;padding:11px 18px;border-radius:10px;font-weight:650;font-size:15px;
    border:1px solid var(--line);background:var(--panel);color:var(--ink);box-shadow:var(--shadow)}
  .btn.primary{background:var(--accent);color:#fff;border-color:transparent}
  .btn:hover{text-decoration:none;transform:translateY(-1px)}
  section{padding:44px 0}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
  .tile{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;box-shadow:var(--shadow)}
  .tile .num{font-size:32px;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
  .tile .lbl{font-weight:650;margin-top:2px}
  .tile .sub{color:var(--muted);font-size:13.5px;margin-top:5px;line-height:1.45}
  h2{font-size:14px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:0 0 18px;font-weight:700}
  code{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:13px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  ul.plain{margin:0;padding-left:20px;color:var(--muted);font-size:14.5px}
  ul.plain li{margin:8px 0}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
  .card{display:block;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:var(--shadow);color:var(--ink)}
  .card:hover{text-decoration:none;transform:translateY(-1px)}
  .card b{display:block;margin-bottom:4px}
  .card span{color:var(--muted);font-size:13.5px;line-height:1.45}
  footer{border-top:1px solid var(--line);padding:30px 0 60px;color:var(--muted);font-size:13.5px}
  footer p{margin:6px 0}
  #theme{position:fixed;top:14px;right:14px;width:36px;height:36px;border-radius:10px;border:1px solid var(--line);
    background:var(--panel);color:var(--ink);cursor:pointer;box-shadow:var(--shadow);font-size:16px;z-index:5}
</style>
</head>
<body>
<button id="theme" title="Toggle light/dark" aria-label="Toggle theme">◑</button>
<div class="disc">__DISCLAIMER__</div>
<div class="wrap">
  <header>
    <div class="eyebrow">__EYEBROW__</div>
    <h1>__HEADLINE__</h1>
    <p class="lede">__LEDE__</p>
    <div class="cta">
      <a class="btn primary" href="__REPO_URL__">Browse the corpus →</a>
      <a class="btn" href="llms.txt">llms.txt</a>
      <a class="btn" href="__PLATFORM__">The platform</a>
    </div>
  </header>

  <section><div class="grid"><!--TILES--></div></section>

<!--SECTIONS-->

  <footer>
    <p>Built __TODAY__ from the corpus. __FOOTER_NOTE__</p>
    <p>Part of the <a href="__PLATFORM__">OregonAI Civic Corpus Platform</a>.
       MCP endpoint: <code>__MCP__</code></p>
  </footer>
</div>
<script>
(function(){
  var b=document.getElementById('theme'),r=document.documentElement;
  try{var s=localStorage.getItem('theme'); if(s) r.setAttribute('data-theme',s);}catch(e){}
  b.addEventListener('click',function(){
    var cur=r.getAttribute('data-theme')||
      (matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
    var next=cur==='dark'?'light':'dark';
    r.setAttribute('data-theme',next);
    try{localStorage.setItem('theme',next);}catch(e){}
  });
})();
</script>
</body>
</html>
"""
