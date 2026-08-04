#!/usr/bin/env python3
"""robots.txt awareness for the toolkit's fetchers (corpus-toolkit#29).

The toolkit fetches from third-party hosts on a cron — `corpus-detect-changes` re-fetches
every source in the manifest on every scheduled run — and until this module existed it
never looked at `robots.txt` anywhere. No allow check, no crawl-delay, no per-host state,
and no field in which to record a host's stated position.

That was fine for every corpus built so far, which is exactly why nobody noticed: the state
and federal endpoints those corpora pull from publish no restrictions. The assumption was
load-bearing and undocumented.

WHAT THE SURVEY FOUND, and why the count understates it. Over the 165 hosts named in the
oregon-counties survey: 42 serve no reachable robots.txt, 8 block a NAMED AI crawler, and 5
name `ClaudeBot` with `Disallow: /`. Those five are `library.municode.com`,
`codelibrary.amlegal.com`, `lake.county.codes`, `www.yamhillcounty.gov` and
`yamhillcounty.gov`, affecting six counties.

Two things sharpen that. **Every commercial code vendor in the survey is in the list** —
Municode, American Legal and General Code all carry the Cloudflare-managed AI block, several
with `Content-Signal: search=yes, ai-train=no, use=reference`, an explicit EU DSM Art. 4
rights reservation. The counties whose law is most machine-readable are precisely the ones
whose hosts refuse this category of use. And these are **generic-crawling-permitted,
AI-crawling-refused** hosts: a blanket `User-agent: * / Disallow: /` would be obvious, while
a rule naming one agent and allowing everyone else is invisible unless something looks.

TWO QUESTIONS, DELIBERATELY KEPT APART:

  `allowed(url)`      — does robots.txt permit OUR user agent? This is the literal
                        compliance question, and the only one with a mechanical answer.
  `ai_position(url)`  — does this host state a position about AI/agent crawling at all,
                        whether or not it names us? A host that `Disallow: /`s ClaudeBot
                        and GPTBot has said something about this category of use that an
                        operator should see before pointing a civic-corpus ingester at it,
                        even though our UA is not literally named and `allowed()` is True.

Conflating them would be wrong in both directions — refusing to fetch what we are permitted
to fetch, or fetching under a technicality past a clearly stated refusal. So this module
reports both and decides neither.

CONTENT-SIGNAL IS SURFACED, NOT INTERPRETED. `Content-Signal: search=yes, ai-train=no` is
not part of the robots.txt grammar and `urllib.robotparser` ignores it. Parsing it into a
boolean would invent a policy the toolkit has no standing to set; it is returned as text for
a human to read.

REPORTING, NOT ENFORCING. Nothing here blocks a fetch. Enforcement is a per-corpus policy
decision and must not arrive as a surprise behaviour change in a toolkit bump — the same
reason `corpus-detect-unsourced` reports rather than gates.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field

# Agents whose presence in a robots.txt means the host has taken a position on AI/agent
# crawling. Used only by `ai_position` — never by `allowed`, which asks about US.
AI_AGENTS = (
    "ClaudeBot", "Claude-Web", "anthropic-ai", "GPTBot", "ChatGPT-User", "OAI-SearchBot",
    "CCBot", "Google-Extended", "PerplexityBot", "Applebot-Extended", "Bytespider",
    "meta-externalagent", "Amazonbot", "Diffbot", "cohere-ai", "AI2Bot", "Timpibot",
)

FETCH_TIMEOUT_S = 15


@dataclass
class RobotsRecord:
    """What one host's robots.txt says. `reachable=False` is not permission — it is the
    absence of an answer, and is reported as `unknown` rather than folded into `allowed`."""
    host: str
    robots_url: str
    reachable: bool
    status: int | None = None
    error: str | None = None
    raw: str = ""
    blocked_ai_agents: list[str] = field(default_factory=list)
    content_signal: str | None = None
    crawl_delay: float | None = None

    @property
    def states_ai_position(self) -> bool:
        return bool(self.blocked_ai_agents or self.content_signal)


_cache: dict[str, tuple[RobotsRecord, urllib.robotparser.RobotFileParser | None]] = {}


def _host_key(url: str) -> tuple[str, str]:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}", p.netloc


def _parse_ai_blocks(text: str) -> list[str]:
    """Agents from AI_AGENTS that this file disallows anywhere.

    Deliberately coarse: any `Disallow:` with a non-empty path under a group naming an AI
    agent counts as a stated position. Precision is `allowed()`'s job; this is a flag for a
    human, and under-reporting it would defeat the point.
    """
    by_lower = {a.lower(): a for a in AI_AGENTS}
    blocked: list[str] = []
    group: list[str] = []      # user-agents of the group currently being read
    in_rules = False           # have we seen a rule line since the last User-agent?

    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()

        if key == "user-agent":
            # A User-agent line after a rule line starts a NEW group; consecutive
            # User-agent lines accumulate into the same one.
            if in_rules:
                group, in_rules = [], False
            group.append(value)
        elif key == "disallow":
            in_rules = True
            # `Disallow:` with an empty value means "allow everything" — the opposite of
            # a block, and counting it would report a permissive host as refusing.
            if value:
                for agent in group:
                    name = by_lower.get(agent.lower())
                    if name and name not in blocked:
                        blocked.append(name)
        elif key in ("allow", "crawl-delay"):
            in_rules = True
    return blocked


def _content_signal(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.lower().startswith("content-signal:"):
            return stripped.partition(":")[2].strip()
    return None


def fetch_robots(url: str, user_agent: str, *, timeout: int = FETCH_TIMEOUT_S):
    """(RobotsRecord, parser|None) for the host of `url`. Cached per host per process."""
    base, host = _host_key(url)
    if base in _cache:
        return _cache[base]

    robots_url = f"{base}/robots.txt"
    req = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # 404 is a real answer meaning "no restrictions"; 401/403 is not.
        rec = RobotsRecord(host, robots_url, reachable=(e.code == 404), status=e.code,
                           error=None if e.code == 404 else f"HTTP {e.code}")
        _cache[base] = (rec, None)
        return _cache[base]
    except Exception as e:                                     # noqa: BLE001
        rec = RobotsRecord(host, robots_url, reachable=False, error=f"{type(e).__name__}: {e}")
        _cache[base] = (rec, None)
        return _cache[base]

    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(raw.splitlines())
    try:
        delay = parser.crawl_delay(user_agent)
    except Exception:                                          # noqa: BLE001
        delay = None
    rec = RobotsRecord(host, robots_url, reachable=True, status=status, raw=raw,
                       blocked_ai_agents=_parse_ai_blocks(raw),
                       content_signal=_content_signal(raw),
                       crawl_delay=float(delay) if delay else None)
    _cache[base] = (rec, parser)
    return _cache[base]


def allowed(url: str, user_agent: str) -> bool | None:
    """Does robots.txt permit `user_agent` to fetch `url`?

    True / False / None, where None means "no answer" — the host served no reachable
    robots.txt. None is NOT True: an unreachable robots.txt is missing information, and
    collapsing it into permission is how a silent assumption gets made on an operator's
    behalf. Callers decide what to do with unknown; this refuses to guess.
    """
    rec, parser = fetch_robots(url, user_agent)
    if parser is None:
        return True if (rec.reachable and rec.status == 404) else None
    return parser.can_fetch(user_agent, url)


def ai_position(url: str, user_agent: str) -> RobotsRecord:
    """The host's stated position on AI/agent crawling, whether or not it names us."""
    return fetch_robots(url, user_agent)[0]


def clear_cache() -> None:
    _cache.clear()
