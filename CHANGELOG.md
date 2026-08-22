# Changelog

Release notes for `corpus-toolkit`, the shared platform every OregonAI corpus pins.

This file exists because `docs/reference-architecture.md` mandates a CHANGELOG in the repo
anatomy every corpus must follow, and `repo.py` hardcodes `CHANGELOG.md` into
`NON_CONTENT_NAMES` on the assumption it exists — while the toolkit itself had none
(corpus-toolkit#41). The de-facto changelog was `git log`, which is good prose but not
something a downstream can read when deciding whether to move a pin across 27 tags.

**Entries before v1.19.0 are back-filled from tag messages and merge commits.** They are
accurate about what shipped and deliberately terse; `MIGRATION.md` carries the upgrade
notes and the reasoning, and remains the file to read before moving a pin.

The audience is a corpus deciding whether a bump is safe, so each entry leads with whether
it can break you.

## Unreleased

### Internal — the suite now asserts WHERE `corpus_toolkit` was imported from (corpus-toolkit#152)

**Nothing here changes anything a corpus can observe**: test-only, no runtime behaviour, no
schema, no MCP contract, no version.

`#146` settled that the *metadata* answering `importlib.metadata` belongs to this checkout.
Nothing asserted that the *code* does, and `import` and `importlib.metadata` search
`sys.path` independently — so a site-packages copy shadowing this checkout produced a
`USER_AGENT` built somewhere else while metadata and `pyproject.toml` agreed perfectly, and
every assertion in the file stayed green. Measured before the guard existed: a tree holding
these tests with the package resolved from a different checkout reported `9 passed, 2
skipped`.

`test_the_distribution_is_the_one_in_this_checkout` had become a duplicate of the first
assertion in `test_user_agent_names_the_version_this_source_declares` — two tests, one fact,
identical failure messages — because the identity question it was named for now happens
before the comparison. It is renamed to `test_the_code_under_test_is_the_one_in_this_checkout`
and given the assertion its name always promised.

**Deliberately not behind `in_tree_distribution`'s skip.** Import provenance does not depend
on install metadata, and gating it there would make it inert in exactly the environment where
a shadowing import is most likely — a worktree, where that guard skips. A worktree imports
its own source, so the ungated check passes there; that was measured in both a worktree and
the main checkout before it was written.

### Internal — `test_user_agent.py` no longer fails in a git worktree (corpus-toolkit#146)

**Nothing here changes anything a corpus can observe**, and no bump is involved: test-only,
no runtime behaviour, no schema, no MCP contract, no version.

Recorded because the failure mode wastes an agent's afternoon and then survives into a PR
description. Two of these tests failed in **every fresh `git worktree`** and passed in the
main checkout. `importlib.metadata` resolves against `sys.path`, and `*.egg-info/` is
gitignored, so a worktree has no metadata of its own and resolution falls through to a stale
user-site install — leaving the assertion comparing this tree's code against an unrelated
install's metadata. Both tests already skipped when *nothing* answered; the condition asked
**"is corpus-toolkit installed?"** where the question is **"is the installed distribution
THIS tree's?"**. The skip reason now names the resolved path of the metadata that answered
and the tree it came from.

**A stale install of this checkout still fails**, which is what these tests are for; CI
installs editable from the checkout root, so nothing is skipped there.

### Fixed — a curated `issuing_body_profiles` file that cannot be read no longer raises out of `issuing_body_profile` (corpus-toolkit#143)

**This can only turn a failed tool call into an answer.** `issuing_body_profile` reads two
files. The issuing-body registry goes through `read_issuing_body_registry`
(corpus-toolkit#136, above), so a registry a corpus cannot read comes back as one reported
condition. The **curated profiles file two lines later was parsed inline** and raised
whatever the file raised: `ParserError` from a mistyped line, `PermissionError` or
`UnicodeDecodeError` from `read_text()`, and `AttributeError` from `.get("profiles", {})`
on a document that parses to a list or a string. The `is_file()` in front of it guarded
only *absence* — the one failure mode that never raised.

The curated block is by construction the **optional** half of this tool's answer: a corpus
declaring no profiles file serves `curated: {}` quite happily. So the whole registry
identity, holdings and attribution answer was being lost to a file whose absence would have
cost nothing, and an agent got a failed tool call instead of a finding.

**It is now read once, through the config, the same four ways the registry is** — gone,
unopenable, unparseable, or shaped like something other than a profiles file
(`corpus_toolkit.config.read_issuing_body_profiles` / `ProfilesRead`, and
`CorpusConfig.issuing_body_profiles_read`). The "could not be read: `<type>`: `<detail>`"
wording is now shared by both readers rather than spelled twice.

**The answer states the limit and serves what the registry knows.** The response is the
normal success response — `curated: {}`, and the registry entry, `in_repo` and
`attribution` all served, because they come from files that read fine — plus a new
**`curated_warning`** naming `plugins.issuing_body_profiles`, the file and the reason, and
saying that `curated` is empty because the overlay could not be read and **not** because
this corpus curates nothing for that body. Reporting "could not check" as "is not there" is
the collapse this platform files bugs about, and an unreadable overlay must never degrade
into "that body is not registered".

**The fault sentence is declared once, in `config.py`, as
`CorpusConfig.issuing_body_profiles_fault`, and is not the registry's.** Different key,
different file, different fix; an operator sent to the registry by it would find nothing
wrong there.

**One behaviour change worth naming: a declared profiles file that is not there is now a
fault rather than a silence.** It used to fall through `is_file()` and serve `curated: {}`,
which reported a path nobody created as a fact about the body. This matches what the
registry reader has always done with a declared file that is gone. A corpus that declares
**no** `plugins.issuing_body_profiles` key is unaffected and carries no warning — that is a
choice, not a fault. Of the eight live corpora only `executive-regulatory-frameworks`
declares the key, and its file is present and reads.

`curated_warning` is additive and absent for every corpus whose overlay reads, so this
stays contract v1.

### Fixed — an address literal is not a name, so `http://127.0.0.1:8000` passed the front-door gate (corpus-toolkit#138)

**This can turn a corpus red that was green.** `corpus-validate-frontmatter` refused a
`corpus.authoritative_source` under a name RFC 2606 reserves, and an address literal is not
a name — so `http://localhost:8000/` failed and `http://127.0.0.1:8000/` and
`http://[::1]/official` passed. Two spellings of the same dead pointer, treated
differently, and the address spelling is the worse one: it *resolves*, on the agent's own
machine, to whatever is listening there.

**Every IP address literal is now refused, loopback or not**, under its own rule and its own
message. `0.0.0.0` and `::` are the wildcard a server binds to rather than an address a
client connects to; `192.168.*`, `10.*`, `172.16-31.*`, `169.254.*` and `fc00::/7` resolve
on the *reader's* network, which is worse than dead because they can answer, wrongly, and
differently for every reader; and a routable literal like `8.8.8.8` names no publisher,
matches no TLS certificate name, and stops being this corpus's front door the day the host
is renumbered. `127.1` and `127.000.000.001` are refused as well — every resolver in the
path reads them as 127.0.0.1, and `ipaddress.ip_address` parses neither (strict dotted
quads, and no leading zeros since CVE-2021-29921), so the check consults `socket.inet_aton`
after it.

**corpus-template still validates at exit 0**, and its exemption is not widened: while
`corpus.id` is unfilled *and* the repo holds no documents, a **missing** front door and the
**placeholder the template ships** are warnings. Those are the two states an unedited
template is legitimately in. An address literal is not one of them — the template does not
ship one — so it is an error there too.

**The RFC 2606 rule is untouched, and the two stay two.** `localhost` still fails as a
reserved *name*, not as a loopback address: the citations answer different questions and a
message that blurs them is wrong about the value it is quoted at. `corpus_overview` picked
the new rule up with no change of its own — that is what moving the predicate in #140 first
bought. No live corpus is affected; all nine declare a public https host.

### Fixed — `corpus_overview` names a front door that cannot answer (corpus-toolkit#140)

**Nothing to change; one new sentence may appear in one tool's response.** `corpus_overview`
attached its `config_warning` on exactly one condition — `corpus.authoritative_source`
missing. A corpus serving `https://REPLACE-ME.invalid/where-the-official-text-lives`, the
value `corpus-template` ships, is truthy, so no warning fired and every object-shaped
response carried that URL as if it were an answer while the one tool an agent is told to
call first said nothing was wrong. An omission had a voice; the same defect with a URL in
front of it had silence.

`corpus_overview` now warns whenever there is **anything** wrong with the declared front
door, not when it is missing. The declared value **still ships** in the envelope: `null` is
the documented "this corpus declares none", and emitting it here would collapse a corpus
configured wrongly into an unconfigured one and delete the one fact an operator needs —
which placeholder is in the file.

**Still not a repo gate at runtime** (corpus-toolkit#141). `config.load()` keeps
`authoritative_source: str | None` and a server still starts on any value, because a pin
bump must not take down a corpus that was legal when it deployed. What changed is that the
runtime may now *say* something. A warning is not a refusal.

The rule itself moved: `validate/frontmatter._reserved_name` and the wording of every
finding about this field are now `config.front_door_fault`, which the validator and the MCP
framework both read. This is the `issuing_body_registry_fault` arrangement
(corpus-toolkit#136) applied to the field next door — the two readers of one field can no
longer answer differently, which is the whole of #140. Zero live corpora are affected; all
nine declare a real https front door.

### Added — a drift run may file one group drift finding (corpus-toolkit#132, ADR 0010)

**On a capped run this costs you tickets: one slot per finding.** `MAX_ISSUES_PER_RUN` is
still 25 and a capped run still exits 1, but a run may now also file **one** issue per group
in which **every compared source changed**, titled `Group drifted: <group>` — and each of
those comes out of the same 25. A run that files two findings reports two fewer changed
sources, taken off the tail of the largest group. Under the cap nothing changes: every
source that drifted still gets its own `Source changed:` ticket, and no finding ever
suppresses one.

It reports **correlation, not cause**: that those sources changed *together*, and nothing
about why. The tool observes bytes. Of the three whole-group events on record one was a
footer version bump, one was a set of URLs that stopped serving, and one — oregon-counties'
3,447 of 3,447 (corpus-toolkit#68) — was no change at all, an inert run whose baselines were
all empty. So the finding **accompanies** the individual tickets and never suppresses them:
a genuinely independent change inside a bulk-drifting group keeps its own ticket.

What it buys, reconstructed against the shape of ERF run 31022774644 — `oar` 484/484, a DEQ
group 52/52, five genuine changes across three agency groups, 813 sources in scope, no
unseeded baselines:

```
               previous release      this release
oar 484/484     0 tickets            1 finding, 0 tickets
deq  52/52     20 tickets            1 finding, 18 tickets
wrd, dhs, odot  5 genuine tickets    5 genuine tickets
                                     ── 25 issues either way; exit 1 either way
```

**It does not fix the duplicate tickets.** `deq` still describes one broken-URL fault
eighteen times, and it paid two of its twenty for the two findings. What changes is that
`oar` — 89% of the drift, one template-level cause, and no ticket at all under the cap —
stops being silent.

Run separately, an `oregon-counties`-shaped manifest (every baseline empty, so every source
reads as changed) files **no** finding at all: nothing was compared, so there is no drift to
report.

The trigger is an observation, not a judgement:

* **100% of the sources that were COMPARED**, and more than one of them. ">80%" was
  rejected: the sources that did not change are evidence against the pattern, and one
  source cannot corroborate itself.
* **An uncompared source is not a changed source.** Unseeded baselines and failed fetches
  are excluded from both sides of the rule, so a group that "changed" because nothing was
  ever compared to it — the oregon-counties shape — files **nothing**.

A finding **consumes a slot from `MAX_ISSUES_PER_RUN`** because a cap some issues are exempt
from is not a cap: a corpus with 27 bulk-drifting groups would otherwise file 27 issues past
a limit of 25. Findings are filed **before** the individual tickets — corpus-toolkit#69
spends the ticket budget smallest-drifting-group-first and so reaches the largest group last,
where a finding queued behind the tickets would never file — and the remaining budget then
spends smallest-first exactly as #69 decided. Among themselves the findings go largest group
first; the ADR does not settle that order, and it is only observable when the findings alone
exhaust the budget, in which case no per-source ticket files at all. Both counts are printed
on stdout, and any finding the budget did not reach is **named** on stderr.

The title carries **no counts**: `_open_issue` prevents re-filing by searching for its own
title, and `Group drifted: oar (484 of 484)` would file a second issue for the same
unresolved condition the day the count read 480. The counts, a sample of the ids and the
run link are in the body.

### Changed — `--check-relationships` now checks the corpus configuration too (corpus-toolkit#139)

**This can turn a `check-links` run red, and that is the point.**
`corpus-validate-frontmatter --check-relationships` returned before `_check_config` was
ever reached, so the path `check-links.yml` runs gated **no** corpus-level fact: not the
front door, not whether the issuing-body registry can be read, not registry rows with no
slug, not a declared `plugins.issuing_body_name_fields` no entry carries, not
`plugins.extra_schema_checks`. It ran the relationship graph and the joins and reported
`OK`.

**Nothing turns red today**, because `validate-frontmatter.yml` runs the full command on
every PR for every corpus, so these findings already ran somewhere. That is a property of
one workflow file rather than of this tool: the moment a corpus's CI is trimmed to the
link check, or someone reaches for this flag as the cheap local validate, a green run had
checked nothing about the corpus's configuration — and since #141 a missing front door is
a hard error, so what was skipped is now load-bearing.

**The choice, recorded at the flag** (`main()` in `validate/frontmatter.py`): the flag
narrows **which documents** are checked, not **whether the corpus is configured**. None of
the config findings is per-document and none gets cheaper by looking at fewer files, and
the join gate went the same way for the same reason (#3 — "leaving it out of that path
would mean the gate exists in a command no corpus's CI actually invokes"). The alternative
considered and rejected was printing "no config was checked": honest, and it leaves a
trimmed CI with no gate at all.

**The summary line changed** on this path, and says what it checked: `OK: relationship
graph consistent across N content file(s), and corpus configuration checked.` A `--changed`
run with no changed content files checks the config too — a corpus-level fact does not
depend on which files a PR touched, and the full command already checked it on that same
no-op run.

**Unchanged: the full command.** Same checks, same order, same output. Both entry points
now call one `_check_corpus_config`, so they cannot drift apart again.


### Fixed — a registry this corpus cannot read is an answer, not a traceback (corpus-toolkit#136)

**No corpus needs to do anything, and one class of tool crash stops happening.**
`CorpusConfig.issuing_body_slugs` answered `None` — the documented "could not check" that
`documents_by_agency`'s `slug_in_registry: null` and `search_corpus`'s
`registry_checked: false` both rest on — only when the declared registry path was **not a
file**. A registry that WAS a file and did not parse raised `yaml.ParserError` out of
whatever asked, including a live MCP tool call. One mistyped YAML line therefore took out
every body-shaped tool on a server, and the index build with them, instead of degrading
them.

**Missing, unopenable, unparseable and wrongly shaped are now one condition** — *this
corpus declares a registry it cannot read* — reported the way the missing file already
was, with the reason carried alongside so every caller can name the file and say what went
wrong. `search_corpus`'s `issuing_body_filter.note`, `documents_by_agency`'s
`attribution.note` and `issuing_body_profile`'s `error` all say it; the last of those used
to raise. A registry **row** with no `slug` no longer raises `KeyError` out of
`issuing_body_profile` either — it is skipped, and the bodies that have a slug are served.

**Three findings stay three findings.** "Could not read the registry", "the registry is
empty" and "that body is not in the registry" have different causes and different fixes,
and the first is never served as either of the others (CONTEXT.md; response convention 5).

**The operator is told once, at startup.** A per-call note reaches the agent, not the
person who can fix the file, so a server whose declared registry cannot be read prints a
WARNING on stderr naming the file and the reason. It still starts: a broken registry costs
one class of question, and refusing to start would cost every other one too.

**A row that is not an entry at all is now counted.** A bare string or a number under
`entries:` was filtered out before anything counted it, so the registry read clean, the
validator reported nothing, and every document naming that body was reported as
unregistered — a check that passed without checking. It is now reported alongside a row
that carries no `slug`: both are rows nothing can be attributed to, and both fail
`corpus-validate-frontmatter`. **No live corpus has one** (measured across the four
registries the nine corpora declare).

**One reader, in `config`.** `RegistryRead` moved from `validate/frontmatter.py` to
`corpus_toolkit.config`, which both `mcp` and `validate` already import, and it is the one
reader the runtime, the load-time sentinel check and the validator all go through. The
validator grew this shape first (#129) while the runtime parsed the same file separately
and raised where the validator reported — one fact declared twice, which is the shape of
five separate defects in this project. The wording of "this registry is broken" is
likewise declared once, as `CorpusConfig.issuing_body_registry_fault`, so the validator's
finding, the three tool responses and the startup warning cannot drift into naming
different halves of it.

**Nothing importable was removed.** `validate.frontmatter.RegistryRead` is now a binding
to the class in `config`, `validate.frontmatter._read_registry(config)` delegates, and
`config._parse_registry_slugs(path, key)` is kept as a name over the one reader — it
returns the slugs as it always did, and raises a `ValueError` naming the file and the
problem where it used to raise whatever the file raised. Nothing in the toolkit calls it;
a corpus repo might, and absence of a local caller is not evidence (AGENTS.md).


### Changed — a corpus without a front door no longer validates (corpus-toolkit#11)

**This can turn your CI red on the next pin bump, and that is the point.**
`corpus-validate-frontmatter` now FAILS a corpus that declares no
`corpus.authoritative_source`, where it used to warn. Every object-shaped MCP response
carries that field and every response tells an agent this copy is non-authoritative and to
verify at source; without the field the agent is never told where. #6 added the key and
asked for exactly this promotion "so new corpora cannot ship without one"; it waited
because the corpora had not adopted it yet.

**Nothing turns red today.** All nine live corpora declare one — measured, not assumed:
`executive-regulatory-frameworks`, `federal-reference`, `oregon-audits`, `oregon-budget`,
`oregon-collective-bargaining`, `oregon-counties`, `oregon-kpm`, `oregon-legislature`,
`oregon-records-retention`. (#11's own title said "all four corpora must declare one —
today zero do", which was wrong in both halves; the three that were still missing landed
before this change so that no corpus meets it as a surprise.)

**A placeholder is refused too, and that is the half that would otherwise have leaked.**
`corpus-template` ships
`authoritative_source: "https://REPLACE-ME.invalid/where-the-official-text-lives"` — it is
URL-shaped on purpose, because a bare `{{...}}` is not a URL and has been an error since
v1.10.0. An omission-only check passes it, so a corpus that forked the template and never
edited the line would have shipped green while every one of its responses pointed an agent
at a host that cannot exist. The rule is the **reserved names of RFC 2606** — the `.test`,
`.example`, `.invalid` and `.localhost` TLDs, plus `example.com`/`.net`/`.org` — rather
than a string match on `REPLACE-ME`, because a corpus that edits the path and leaves the
host is shipping the same dead pointer. The check reads the URL's HOST, so a real front
door with `example` in its path is untouched. A host still carrying the template's
`REPLACE-ME` marker is refused too — the reverse edge, where the reserved name is edited
away and the marker is not — and so are two values that used to slip past the URL check
that precedes all of this: `https:///schedules`, which names no host, and
`https://[oops`, on which `urllib.parse.urlsplit` RAISES, ending the whole run in a
traceback that named neither the file nor the key.

**The template still validates itself**, or every corpus would start life from a template
that fails. While `corpus.id` is still the unfilled `{{CORPUS_ID}}` **and** the repo holds
no documents, both findings are reported as warnings, with a third warning naming that
state — so it is never silent. Both halves are load-bearing: filling in the id is step 1 of
the replication guide, adding a document is what makes a repo a corpus, and either one
turns the warnings back into errors. The exemption is corpus-wide, not `--changed`-scoped,
so a PR that touches no content file cannot borrow it.

**Unchanged:** the loader still types the field `str | None` and a server still starts
without one, still emitting the documented `authoritative_source: null` plus
`corpus_overview`'s `config_warning`. This is a repo gate, never a runtime one — a corpus
that was legal when it deployed must not be taken down by a pin bump. A value that is not a
URL at all remains the error it has been since v1.10.0.

**Whether this warrants a major bump is a release decision, not this entry's.** AGENTS.md
says breaking changes bump the major version, and a corpus that has not adopted the key
goes from green to red on a pin bump. The precedent in this file runs the other way — the
v1.10.0 non-URL error and #3's dangling-`document_id` promotion both shipped as minors, and
no major bump exists here — so the call belongs to whoever cuts the tag.

**If this fails your corpus:** add one line under `corpus:` in `_meta/corpus.yml` naming
the one page a reader opens to reach your official text. One URL is enough for a corpus
spanning several publishers — `get_document` answers per document from that document's own
`source_url`.

Also: the release gate's own scratch corpus was carrying the template's `.invalid`
placeholder, unnoticed, since the template stopped using a `{{AUTHORITATIVE_SOURCE_URL}}`
placeholder. `contract_smoke.py` now fills in a real front door the way a human
instantiating the template would, and fails loudly if the template stops carrying exactly
one `authoritative_source:` line to fill.


### Fixed — `search_corpus(issuing_body=...)` takes a registry slug too, and says which it matched

**One response-shape change, stated first because the rest of this entry is additive.** A
body-filtered search that matches NOTHING used to return `[]` and now returns a one-element
list holding a `no_hits` record (below) — so a client that counts `len()` on a search it
filtered by issuing body sees 1 where it saw 0. That is the whole of the behaviour change:
a frontmatter search that MATCHES is unchanged hit for hit, an unfiltered search is
byte-identical in both directions, and no other tool moves. **Whether that warrants a major
bump is a release decision, not this entry's**: the shape changed on one path of one tool,
the contract's Versioning rule reads "additive tools/fields stay v1; changed semantics bump
v2", and the call belongs to whoever cuts the tag.

The filter was an exact match on the free-text `issuing_body`
**frontmatter** field and nothing else. Every other tool that takes a body takes a
**registry slug** — `issuing_body_profile(slug)` resolves one, `documents_by_agency(slug)`
requires one — so a caller holding a slug passed it here and got `[]`, which is
indistinguishable from "this corpus holds nothing for that body". The contract did not say
which of the two the parameter wanted, so neither reading was wrong on its face.

It is worse for an agent than for a person: an agent that has just resolved a slug has every
reason to reuse it, no signal that this one parameter wants a different kind of string, and
an empty result that reads as a finding.

**Both are accepted, and resolution is by IDENTITY, never by hit count.** A value naming an
entry in the corpus's issuing-body registry filters on the resolved slug
(`docs.issuing_body_slug`, the column `documents_by_agency` already answers from); anything
else filters on the frontmatter field exactly as before. Deciding by "did it return
anything" would make the same call mean different things on different days, and would
collapse the distinction this fixes: a slug naming a real body that holds nothing here would
fall through and be reported as a string that matched nothing.

**A body-filtered search with no matches no longer answers `[]`.** `search_corpus` is the
one tool with no envelope to carry a note (response convention 1's exemption), so an empty
list was the whole answer — and an empty list cannot say which of two columns it looked in.
It now returns exactly one record that is not a hit (`no_hits: true`, no `id`/`path`/
`snippet`) carrying the query, the filter block and a note naming every filter applied. It
says what was searched for; it never says the corpus holds nothing for that body. An
unfiltered search that matches nothing still returns `[]`.

**Every hit gains `issuing_body_filter`** — `{value, matched, registry_checked, note?}` —
and only when the parameter is used. `registry_checked` is separate from `matched` because
`matched: "issuing_body"` means two different things: *checked, and it names no body* when a
registry was read, and *never asked* when the corpus declares none or declares one that
could not be read (the note says which — a broken path is a fault, no registry is a choice).
Reporting the second as the first would tell a caller its slug is wrong on every corpus with
no registry to be wrong against.

**A registry NAME is filtered as frontmatter text, not resolved to its slug.** "Not a slug"
is never reported as "not a body": `issuing_body_profile` also takes a name, so a caller can
hold one, and a registry name overlaps a document's free-text `issuing_body` routinely —
resolving names would hijack the reading that already works for the callers who were always
right. The value is also **stripped once** before either match, as `documents_by_agency`
already strips its slug, so a padded value stops missing a row the corpus does hold.

**A value that is both a slug and some document's frontmatter string resolves to the slug**,
and the hit says so. The slug is the identity every other tool takes, so a caller holding one
got it from a slug-shaped tool; there is no parameter to force the other reading, because a
frontmatter descriptor is prose and a slug is lower-hyphen-case.

**Custom backends keep working and are never misreported.** `RetrievalBackend.search` MAY now
accept a keyword-only `issuing_body_slug`; the framework passes it only to a `search` whose
signature NAMES it, so an adapter written before this keeps answering, and its frontmatter
answer is labelled a frontmatter answer with a note naming the backend rather than relabelled
a slug answer. `**kwargs` deliberately does not count: a backend that swallows the keyword
returns an *unfiltered* result, and an unfiltered result labelled "filtered by slug" is a
wrong answer rather than a missing one. `FileBackend` implements it, so a file-backed corpus
does nothing, while an `api` or `hybrid` corpus — which cannot use `FileBackend` at all and
therefore supplies `plugins.retrieval_module` — keeps the behaviour it has today until it
names the parameter. The slug filter holds on the semantic branch as well as the keyword one — a
filter only one ranker honours stops filtering the moment a corpus configures semantic
search.

No index rebuild: `docs.issuing_body_slug` has been in the schema since v1.26.0.
Contract v1 (`docs/mcp-interface-contract.md`, corpus-toolkit#131).

### Added — the validator reports a name field the registry does not carry

**Reported, not fatal: a corpus mid-migration keeps loading and keeps serving.** New
`corpus-validate-frontmatter` warning when a field listed in
`plugins.issuing_body_name_fields` reaches no name in the issuing-body registry it names
columns of.

The declaration is checked at load for SHAPE — empty list, bare string, non-string entry,
no registry to name columns of — and was not checked against the registry itself, so
`oar_nmae` loaded clean, served clean, and made every free-text `issuing_body_profile`
query against that field match nothing. Matching nothing is exactly what a body the corpus
does not hold looks like.

It stays out of the loader deliberately: ERF declared `oar_name` between ERF#166 and
ERF#168, while the column was still being populated, and refusing that load would break a
config that is correct and merely early. So it surfaces where corpus-level config findings
already surface — the same function that reports a missing `corpus.authoritative_source`,
in the command every corpus runs on every PR:

```
warning _meta/corpus.yml: plugins.issuing_body_name_fields: no entry in
_meta/agency-registry.yml carries a name in 'oar_nmae' — checked 189 entries, ...
```

Not `corpus_overview`'s `config_warning`: that channel reaches an AGENT holding an answer,
and a registry column an agent cannot fix would be noise on every conversation.

**Four conditions, kept apart, because three of them otherwise read as "that body is not
here".** A field carried by *some* entries is a partly-populated column and is not
reported. A field whose every cell is null or numeric IS reported, because `name_values` —
now shared by the matcher and the validator — skips cells that are not strings, so a check
for the key alone would pass while every query still matched nothing. A registry that could
not be read is never reported as a registry lacking a field: that is an **error** naming
the read failure and saying the fields went unchecked. And a registry holding **no entries
at all** is reported as an empty registry rather than as a misspelled field — a column
claim about a registry with no rows accuses an author of a typo they did not make.

A corpus that declares nothing gets the same finding worded for the state it is in:
`issuing_body_name_fields defaults to ['name'], and this corpus declares no override`.

**Also fixed: a broken registry no longer ends the validator in a traceback.** A
`plugins.issuing_body_registry` naming a missing file raised `FileNotFoundError` out of the
registry load before any finding was printed, and an entry with no `slug` raised
`KeyError`. Both are now named errors against the registry path — the run still fails, with
a message naming the file and, for a slug-less row, how many rows are affected — and the
per-document slug checks skip rather than report every document's slug as unregistered.
(corpus-toolkit#129)

### Fixed — `register_scheme` accepts a compiled pattern, flags and all

**No action required; every existing (string) call is unchanged. A corpus whose citation
patterns carry flags should now pass the compiled object rather than its `.pattern`.**

`register_scheme(name, pattern, ...)` typed and documented `pattern` as `str`, so a corpus
that keeps its citation patterns compiled — the natural shape, since it matches with them
itself — had exactly one call available: `register_scheme("eo", EO_C.pattern)`.
`re.compile()` over a pattern's source text keeps **none** of the flags the original was
compiled with, and the loss is silent: the scheme registers, the server starts, and
citations stop matching in whatever way the flag governed.

`executive-regulatory-frameworks` registers six schemes and lost `re.I` on five of them
that way (`ORS_C`, `OAR_RULE_C`, `OAR_DIV_C`, `EO_C`, `NUMS_C`); only `OR_CONST_C` matched
case-insensitively, because an inline `(?i)` had been added to it by hand when this was
first hit. So `resolve_citation("executive order 23-04")` came back `unresolved` while
`"EO 23-04"` resolved, and nothing in the response said the difference was case rather
than content.

```python
EO_C = re.compile(r"(?:Executive\s+Order|EO)\s+(?P<num>\d+-\d+)", re.I)

register_scheme("eo", EO_C)            # flags survive by construction
register_scheme("eo", EO_C.pattern)    # case-sensitive, as it always was
```

The parameter is now `str | re.Pattern`: a string is compiled exactly as before (no flags,
inline `(?i)` honoured), and a compiled pattern is used **as itself**. Precisely: passing a
compiled pattern already worked at runtime, because `re.compile()` returns one unchanged —
but nothing said so. The annotation said `str`, the docstring said `str`, and no test held
the behaviour, so a corpus had no reason to write that call and one obvious tidy-up
(`re.compile(pattern.pattern)`) would have dropped every flag again. This release makes it
the contract rather than an accident: declared type, explicit branch, and a guard that runs
through the served resolver. A compiled *bytes*
pattern is refused at registration with a `TypeError` naming the scheme — it can only ever
raise `TypeError` on a citation str, and it would have done so on every resolve inside a
live server. The guard runs through the served resolver, not the local pattern object.
(corpus-toolkit#134)


### Fixed — the drift issue budget is no longer spent in manifest order

**No action required, and the cap does not move.** `MAX_ISSUES_PER_RUN` is still 25 and a
capped run still exits 1. What changed is *which* 25 sources get an issue: they are now
taken smallest-drifting-group first instead of in manifest iteration order.

The budget used to go to whichever group the loop reached first. ERF run 31022774644 is the
case: 544 changed, 25 opened, 519 dropped, and the whole budget went to a 52-source DEQ
group that happened to sort first. Five apparently genuine changes across three other
agencies got no ticket, and neither did `oar` — 484 of the 544 changed sources. Reconstructed
against this release, the same manifest files `wrd 1/1, dhs 2/2, odot 2/2` and spends the
remaining 20 on DEQ: every small genuine finding is reported, and what is dropped is the tail
of the largest groups rather than an arbitrary prefix of the manifest.

Ordering is deterministic — group drift count ascending, then group name, then the
manifest's own order within a group — so a re-run over the same drift files the same set.

A capped run now prints, per group, **issues opened/attempted of sources changed** —
`wrd (1/1 of 1), dhs (2/2 of 2), odot (2/2 of 2), deq (20/20 of 52), oar (0/0 of 484)` — and
separates the two ways a group can end up with no ticket: *not reached by the budget* (this
allocation working) and *every issue creation failed* (#53 happening, which the run-wide
alarm cannot see when a larger group's filings succeeded). Both are named, because from the
breakdown alone "a group that drifted has no issue" looks the same either way.

`changed-sources.tsv` is unaffected: still every changed source, still in manifest order,
still the same four columns — now pinned by a test, which it never had.

**This does nothing for the everything-is-broken shape.** oregon-counties run 31400877762 —
3,391 of 3,447 changed because every manifest entry carried `sha256: ''` — files exactly what
it filed before, because when every group drifts at ~100% every allocation is equally
meaningless. That shape is #68's (seed the baselines); the per-group breakdown from #67 is
what tells the two apart. (corpus-toolkit#69)


### Added — a corpus declares which registry fields carry a name

**No action required. Which bodies a corpus matches does not change unless it declares
something; the candidate payload changes for everyone.** New config key
`plugins.issuing_body_name_fields`, defaulting to `["name"]` — exactly the one field
`issuing_body_profile`'s free-text fallback matched before, so a pin bump never widens a
corpus's matcher on its behalf. What *every* corpus gains is two additive keys on each
candidate (below) and one fixed crash.

`issuing_body_profile` takes a registry slug or free text, and the free-text half matched
`name` alone. That is right only while `name` holds the name readers know.
`executive-regulatory-frameworks` is migrating under its ADR 0003: `name` holds the OAR
chapter title, that title has been copied to `oar_name`, and ERF#168 makes `name` the
**statutory** name. Those differ in practice — "Business Development Department, Oregon
(DBA: Business Oregon)" is one string in the state's financial register, another in the
rules index, and a third in statute. Measured against ERF's committed 189-row registry with
that promotion simulated, matching `name` alone leaves **189 of 189** bodies unfindable by
the name printed on every OAR citation; `name` + `oar_name` + `aliases` leaves **0**
(corpus-toolkit#128).

```yaml
plugins:
  issuing_body_registry: _meta/agency-registry.yml
  issuing_body_name_fields: ["name", "oar_name", "aliases"]
```

A declared field's value may be a **string or a list of strings**, matched element-wise, so
a curated alias list needs no key of its own. Uniqueness stays **per body, not per name** —
a query hitting a body's name, its `oar_name` and two aliases is one hit, not four.
Widening is safe here in a way widening a *join* would not be: this is a disambiguation
surface, so a wider net produces a question, never a silent misattribution.

**Candidates now carry the name that matched**: `{slug, name, matched_field, matched_name}`.
`name` is unchanged; the two new keys are always present (for a corpus declaring nothing
they read `name` and the entry's name), so a caller that renders candidates never branches
on what the corpus declared. A reader who searched by an OAR name is no longer handed a list
of statutory names they may never have seen.

**Also fixed, for every corpus: a malformed registry cell no longer takes the tool down.**
A registry entry whose `name` is null, numeric, or a list made the free-text fallback raise
`AttributeError` on `None.lower()` — every free-text query against that registry, not just
one naming the bad entry. Such a cell is now skipped rather than coerced (`str(None)`
matching the query "none" is a match nobody wrote), and a list is matched element-wise. A
corpus whose registry holds a **list-valued `name`** therefore finds bodies through it that
v1.28.0 crashed on; that is the one respect in which a corpus declaring nothing is not
byte-identical, and the previous behaviour was an exception rather than an answer.

The declaration is checked at load: an empty list, a key with no value, a bare string, a
non-string entry, or name fields declared with no `issuing_body_registry` all fail loudly.
Each of those otherwise degrades into "matches nothing", which is indistinguishable from a
body that is not there — the very symptom this change fixes.

## v1.28.0 — 2026-08-20

### Fixed — two silent wrong-writes in `--record-baseline`

**Can affect any corpus that runs `corpus-detect-changes --record-baseline`.** Both wrote or
skipped the wrong thing and reported success; neither raised, and the two verification
guards that exist to catch exactly this passed both.

**A `sha256:` written above `id:` got a duplicate key inserted (corpus-toolkit#119).** The
rewrite planner associates a `sha256:` with its entry by scanning forward from `id:`, so one
above it was never claimed and a second was inserted below:

```yaml
sources:
  - sha256: ""                        # stale, and now shadowed
    id: "a"
    sha256: "9239087f81db…"           # inserted
```

Both guards accept it: PyYAML resolves duplicate keys last-wins, so the re-parse check reads
back the *inserted* value and matches, and the line diff sees one added line carrying a
wanted value — the shape it is designed to allow. The run reported `1 baseline(s) recorded`,
exited 0, and left a stale key in a file a human reviews. On the next run the manifest parses
to the new value, so the source reads as current and nothing ever surfaces it.

The planner now also scans backwards from `id:` to the entry's start, **at the entry's own
key column** — the same rule the forward scan uses, so an `attachments:` list carrying
per-file digests above the entry's `id:` is not claimed.

**Duplicate ids across two group files sharing a `group:` name were not detected
(corpus-toolkit#120).** `occurrences` was built per file while `fetched`/`in_scope` are keyed
`(group, id)`, so two files declaring the same group collided in the hash map while looking
unique to the duplicate guard. One entry's hash was written into the other, no
`REFUSED to record` line, exit 0 — the wrong-entry write the planner refuses by name,
arriving one level up where the guard did not look.

`occurrences` is now built across all files on the same key. Two files under *different*
groups may still share an id — directory mode defaults `group` to the file stem — and are
untouched. The refusal names **every** file involved: an operator told "duplicate id in this
group file" about a cross-file collision searches the wrong one and finds a single entry that
looks fine.

No manifest on the platform is affected today; every live entry writes `id:` first and no two
group files share a `group:`. Nothing enforced either.
### Fixed — `issuing_body_profile("")` served a profile nobody asked for

**Affects any corpus whose issuing-body registry holds one entry.** The tool takes a slug
**or** a free-text name fragment, and the fallback is a case-insensitive substring match —
where `"" in name` is true for every entry. On a one-entry registry that is exactly one hit,
the uniqueness test passes, and the tool answers with registry identity, curated notes and
holdings for an agency nobody named (corpus-toolkit#122).

**The failure is inverted with corpus size.** On a multi-entry registry every entry matches,
so `len(hits) != 1` and the error path already fires. It was silent precisely where one match
looks like a deliberate answer.

An empty or whitespace-only query is now refused by name — it is a missing argument, not a
wildcard and not a name fragment — matching the shape corpus-toolkit#123 gave
`documents_by_agency`.

The query is also **stripped once and the stripped value used throughout**. `"  slug  "`
previously missed the exact-match branch, fell into the substring fallback, matched nothing,
and was reported as a slug the registry does not contain — about one it does.
### Added — the release gate now covers the template's `CMD` and its requirements extras

**No action required.** A gate-only change plus one new public function,
`corpus_toolkit.mcp.server.build_arg_parser()`.

corpus-toolkit#100 made the gate run the template's Dockerfile `RUN` commands. Two more
pieces of consumed surface in the same file were still uncovered, and both fail the same way:
unit tests green, `entrypoints` green, the gate green, **every corpus broken**
(corpus-toolkit#116).

**The `CMD` is how the container actually starts.** The gate asserted
`corpus-mcp-serve --help`, which argparse answers with exit 0 regardless of which options
exist. Rename an option in the toolkit and: the unit suite stays green (`test_mount_path.py`
builds the app through `_sdk.http_kwargs` and never touches the parser), the `entrypoints`
job stays green (it asserts `hasattr(module, "main")`), `--help` still exits 0, #100's
build-command step still passes — and every corpus container crash-loops on
`unrecognized arguments`. That `CMD` is identical across all seven live corpora.

The gate now extracts the `CMD` argv from the Dockerfile and parses it through the parser
`corpus-mcp-serve` itself runs. That needed the parser out of `main()`, so
**`build_arg_parser()` is now importable**; `main()` calls it, because two parsers would
drift and the gate would be validating an argv `main` no longer accepts.

Shell-form `CMD` is **refused**, not skipped — a `CMD` the gate cannot read is a `CMD`
nothing validates.

**The extras are names a corpus depends on.** `pip install -r requirements.txt` is classified
container-only and skipped, so `corpus-toolkit[mcp,semantic]` was never checked against
`pyproject.toml`. pip only *warns* on an unknown extra: delete or rename `semantic` and the
image builds, the gate is green, and every corpus loses numpy — `semantic.available()`
returns `False` and the corpus serves keyword-only **while reporting healthy**. That is the
`federal-reference` incident the extra's own comment in `pyproject.toml` records.

Both checks verified by mutation: renaming a parser option and undeclaring an extra each fail
the gate at step 5.

The extras check reads `pyproject.toml`, so it needs a TOML parser. `tomllib` is 3.11+ and
this project supports 3.10, so the `test` extra now carries `tomli` there — the same parser
under its pre-stdlib name. With neither available the gate says so by name rather than
failing the extras check for a reason that has nothing to do with extras.


### Added — `documents_by_agency(slug)`: a corpus answers for one agency registry slug

**Additive.** A new tool, registered on any corpus whose retrieval backend implements
`documents_for_slug(slug, limit, offset)` — which `FileBackend` now does, so every
file-backed corpus serves it on the pin bump with no config change.

```
documents_by_agency("department-of-geology-and-mineral-industries")
→ {slug, slug_in_registry, documents: [...], total, returned, limit, offset, attribution}
```

It exists so `corpus-gateway` can assemble `agency_profile(slug)` by **asking** each corpus
instead of duplicating each corpus's agency crosswalk. The crosswalks are per-consumer by
design — *"the table lives in the consumer, correctness belongs to the registry"* — so a
gateway that copied them would re-centralise what was deliberately distributed and go stale
silently whenever one changed. No crosswalk loader was added to the toolkit: the mapping is
applied at ingest by the corpus that owns it (`oregon-kpm` 785/785 documents, `oregon-audits`
223/242, measured 2026-08-19) and the toolkit reads the resolved slug it already indexes.

**Four answers that must not collapse into each other**, because conflating any pair is the
defect this platform files bugs about:

| `documents` | `attribution.complete` | means |
|---|---|---|
| non-empty | `true` | the whole answer |
| non-empty | `false` | a **floor** — documents here are attributed to nobody |
| empty | `true` | this corpus genuinely holds nothing for that slug |
| empty | `null` | nobody measured. **Not** the same as none |

`slug_in_registry` is `null`, never `false`, where the slug was not checked: "not checked
here" is not "no such agency". A registry that is *declared but unreadable* is reported as
the fault it is, not as "this corpus declares no registry".

A refusal carries `error` and omits `attribution` — a refusal is not an answer, and a
completeness claim attached to one invites reading it as one; branch on `error` first.

A declared no-body sentinel is refused by name rather than answered — those documents belong
to no body by the corpus's own assertion, so they are not any agency's holdings, and serving
them under one would rebuild the conflation corpus-toolkit#94 closed. An empty slug is
refused too: it matched the `''` written for every unattributed document, so one response
returned them as an agency's *and* counted them as belonging to none.

`limit` is clamped to 200 and `offset` to ≥ 0, and the response echoes the values actually
served — SQLite reads a negative LIMIT as unbounded, so `limit=-1` returned every match in
one response while the response still said `limit: -1`.

**Not gated on a registry**, unlike `issuing_body_profile`. That tool reports registry
identity and needs one; this reports which documents carry a slug and does not. The
difference is load-bearing — `oregon-kpm` has its registry commented out and `oregon-audits`
declares none, so mirroring that gate would leave the tool unregistered on two of the three
corpora a cross-corpus agency profile has to ask.

`holdings_for` now also reports `unattributed` and `declared_no_body` for a corpus with no
registry. Only `in_registry`/`no_registry_entry` need one to tell apart, and omitting all
four discarded the fact that decides whether a per-slug answer is a floor. `complete` is
`false` or `null` there, never `true`: documents carrying no slug prove a floor without a
registry, but a mistyped slug is invisible without one, so completeness is unknown rather
than yes.

That path is selected on the **corpus's config**, not on which keys a backend happened to
send. Selecting on the report shape alone — the first version — fired for a corpus that
*does* have a registry whose backend reported a partial measurement: the half-measurement
was served as a measurement, the diagnostic naming what was missing disappeared, and the
note asserted a config fact the code had never looked at. `issuing_body_profile` reaches
`_holdings` through the same door, so that did change existing answers. It no longer does.

A backend reporting registry buckets for a corpus that declares no readable registry is a
**disagreement**, reported as unknown and named as such, rather than resolved in the
backend's favour.

Closes corpus-toolkit#46's toolkit slice.


### Added — a JSON source can declare which paths it watches

**Opt-in; nothing changes unless a source adds `watch`.** Verified across the platform: 1,116
sources in 8 manifests, 3 of them `format: json`, none declaring `watch` — so no committed
`sha256` moves.

`content_hash` normalised only the html/xml branch; `json` fell through to a raw-byte hash of
the whole document. For a Socrata-backed corpus that is a permanent false-positive generator:
the manifest watches a metadata document to avoid row-level noise, and gets counter-level
noise instead. `oregon-budget`'s three JSON sources produced six distinct hashes across two
consecutive weekly runs in a week nothing upstream changed.

```yaml
    format: json
    watch: [rowsUpdatedAt, "columns[].name", "columns[].dataTypeName"]
```

The hash covers only those paths, canonicalised, so upstream re-serialising is not a change.
An **allowlist** rather than a list of keys to ignore: a new vendor counter is inert by
construction, where a blocklist makes each one a fresh false positive until somebody extends
it.

A declared path the document does not contain is an **error** (`WATCH PATH MISSING`), not an
empty value and not a fetch failure — two documents both lacking it would otherwise hash
equal and read as unchanged, which is the corpus reporting stability exactly when upstream
removed the field. It is reported on stderr with a run annotation, counted separately from
fetch failures, kept out of the `SYSTEMIC` access threshold, and **exits non-zero** whether
or not `--strict` is set: the bytes arrived, so this is not upstream being briefly
unreachable, and the source stays uncompared on every run until somebody looks.

The per-group breakdown now marks a group where sources were not compared —
`blocked 0/2 [2 not compared]`, which used to render as `blocked 0/2` and read exactly like
a group compared in full and found stable. **This covers failed fetches too**, so it is
visible on runs that have nothing to do with `watch`.

A body that will not parse as json is `WATCH BODY UNREADABLE`, distinct again: a 200 carrying
an error page is a fact about the response, not about the `watch` list.

A `watch` list is checked before the first request — after the `--group` filter, so a typo
in one group does not abort another group's cron. A `watch` source must be json — `format: json`/`geojson`, or a
url ending `.json` when no format is declared — checked there too. Anything else is refused
by name: the body would not parse, and the run would report that as an unreadable response
rather than as the declaration it is. A bare string
(`watch: rowsUpdatedAt`) is iterated character by character, so an authoring typo used to
surface as `watched path 'r' is not present` — an upstream schema change that never
happened. A valueless `watch:` and an empty `watch: []` are refused for the same reason:
each reverts the source to a hash that reports what it was declared not to. So are
`columns[]name` (a dot short of a projection), `columns[ ].name`, and any segment that is
only `[]` — each was otherwise reported as a path the document does not contain, a typo read
as an upstream schema change. Whitespace around a path or around a segment is trimmed rather
than refused, so `columns[] . name` works. One grammar, checked before the crawl and again at the hash by the same
parser.

`not compared` means one thing on every line that says it — the totals line, the per-group
breakdown and the `--record-baseline` tally all count a source that was in scope and never
compared to a baseline, whatever the reason, with the reasons broken out on the totals line.

A document that is a top-level JSON array — Socrata's `/resource/{id}.json`, the sibling of
the endpoint this feature is for — reports that a watch path addresses keys and points at
`url`, rather than reporting a schema change.

Bodies are decoded `utf-8-sig`: a BOM is routine from IIS/.NET-backed endpoints and must not
make a source permanently uncomparable.

**`--record-baseline` no longer breaks when a source key holds a block sequence above
`sha256:`.** The rewrite planner treated any nested list item as the start of a new source,
so the `sha256:` line was orphaned, a second one was inserted, the re-parse check failed and
the entire group file was refused — nothing written, including sources that verified.

This is **pre-existing, not new with `watch`**: `oregon-records-retention` ships
`references_out:` as a block sequence in 76 of its sources today, and is saved only by
writing `sha256:` above it. Reordering those two keys was enough to hit it. `watch` would
have made it routine.

See MIGRATION.md — adopting it re-baselines that source, so land it on its own PR.

Closes corpus-toolkit#72.


### Fixed — a `tools_module` tool colliding with a built-in refuses to start

**Can break you only if your `tools_module` registers a tool named for a built-in** — and if
it does, that tool has never run. No corpus does: the five live extension-tool registrations across the
platform are of three names — `list_datasets` and `query_dataset` in `oregon-legislature`,
and `join_lookup`, `list_datasets` and `query_dataset` in `oregon-budget`.

Both SDK majors keep the tool registered FIRST, so a corpus tool named `corpus_overview` was
discarded and the built-in answered in its place. Nothing said so — the startup summary
infers what was added by set difference, and a shadowed name was already present before the
hook ran, so it could never appear in the difference. The line read `+1 corpus tool(s):
join_lookup` and an operator read that as success.

A corpus author shipped a tool, it never ran, and the failure was indistinguishable from the
tool working, because a built-in of the same name answered.

The server now records what the module ATTEMPTS to register — wrapping `add_tool`, the
choke point every registration passes through, so a corpus calling it directly cannot walk
past the check — and refuses to start, naming the tools. Three shapes are refused: a name
already registered, a name **reserved by the contract even when this corpus does not serve
it** (`authority_chain` and `issuing_body_profile` are conditional, so keying on what is
present let a corpus claim one today and turn fatal later), and a name the module registers
twice. That is the policy the surrounding code already applied to a
module that fails to load: a server that starts anyway "looks healthy, answers every built-in
call correctly, and is silently missing the tools the corpus was built to provide".

The degenerate case is fixed with it: when every registration collided, the difference was
empty and the pre-existing guard raised "registered no tools" — blaming the corpus for the
opposite mistake. The two errors are now distinct.

Closes corpus-toolkit#111.


### Fixed — the release gate runs corpus-template's own build commands

**Nothing required of a corpus.** This changes what the toolkit's own CI asserts, not what a
corpus does.

`release-gate.yml` checked `corpus-template` out and ran `contract_smoke.py` against it — a
Python-level check that never executed the template's **Dockerfile**. That `RUN` is the one
artifact in the org describing how a corpus actually starts, and no CI anywhere ran it.

corpus-toolkit#75 deleted `CorpusFramework.ensure_index` after searching `corpus_toolkit/`
and `tests/` and finding no caller. The template's Dockerfile calls it at image build. The
gate went green, v1.25.0 and v1.26.0 both shipped, and every corpus image build failed until
v1.26.1 — while a pull-based reconcile loop retried the failing build every ten minutes and
contributed to the deploy host reaching 100% disk.

The gate now **extracts** the toolkit-facing commands from the template's `RUN` instructions
and runs them against the candidate toolkit. Extracted rather than copied: a duplicate in CI
drifts from the Dockerfile and then asserts nothing, which is the same species of bug. It
**refuses a step it does not recognise** and reports what it skipped, because an extractor
that quietly matches nothing rebuilds the green this exists to remove.

Verified the only way that matters: deleting `ensure_index` again turns the gate red with the
same `AttributeError` that shipped.

`CONTEXT.md` gains **consumed surface** — what a corpus reaches into the toolkit for that is
not an MCP tool, including the `CMD` argv and the extras a corpus's `requirements.txt` names
— and `AGENTS.md` the rule that would have prevented the deletion outright: anything
reachable from a corpus repo is public, whether or not this repo calls it. That rule says
plainly what the gate does and does not yet cover, rather than implying it covers all of it:
the `CMD` argv and the requirements extras are still uncovered, tracked as
corpus-toolkit#116.

Closes corpus-toolkit#100.


### Fixed — release-note guidance to pass `--rebuild-image` is corrected

Docs only; no code change. Five places across this file and `MIGRATION.md` told a maintainer
with a baked-image corpus to run `deploy.sh <corpus> <ref> --rebuild-image` after an FTS
schema bump. The flag is guarded on a corpus being **mounted**, `platform-deploy` mounts
none, and an ordinary deploy builds the image anyway — so the index is re-baked without it.
Verified on the v1.27.0 pin wave: ERF's image was rebuilt by the ordinary deploy, no flag
passed.

The rebuild itself is real and still required; only the flag was unnecessary. The old
guidance is annotated rather than deleted, so anyone who followed it and saw a "did nothing"
notice can find out why. It becomes correct again the day a corpus is mounted.

Closes corpus-toolkit#114.

## v1.27.0 — 2026-08-19

### Fixed — a graph relation name can no longer displace a response key

**Can affect you only if your `_meta/graph.json` declares a relation named `corpus`,
`archetype`, `authoritative_source`, `id` or `title`.** None does today — the relation types
in use across the platform are `references_external`, `related`, `supersedes`, `implements`
and `implemented_by` — so this is protective rather than a migration.

If one ever did, **`graph_neighbors` stops answering for that corpus** and returns an error
naming the relation, the reserved set and the graph file. Only that tool:
`corpus_overview`, `resolve_citation` and `authority_chain` share the graph loader but
cannot have a key displaced by a relation name, so they keep working. **The failure is
lazy** — the graph is parsed on the first graph-tool call, not at boot — so a collision is
first observed in production, and no CI gate catches it. MIGRATION.md carries a one-line
check to run before bumping.

`graph_neighbors` writes one response key per edge-relation type, after the envelope, and
nothing constrained those names. A relation named `corpus` overwrote that envelope field
with a list of neighbour records — a hard `ValidationError` since the envelope types it
`str`, so the tool stopped answering for that document. `id` and `title` were worse:
overwritten with **no error at all**, because the envelope model constrains only its own
three fields, so a caller received a list where it expected a document id.

This is the third and last site of the class the two entries below close. `authority_chain`
was audited and is safe — it prefixes every configured relation as `up_{name}`/`down_{name}`,
so a colliding name cannot reach the response.

**The remedy differs from the other two deliberately.** Those merged a *backend's* mapping
over a response and were fixed by re-asserting the framework's keys last: the backend had no
business setting them, so ignoring it costs nothing. A graph relation is the corpus's **own
declared edge**, and silently dropping it would be data loss rather than enforcement — the
author would never learn their relationship had stopped being served.

Detection runs where the graph is parsed, so it costs once per corpus (measured at 0.2s for
ERF's 75,905-node graph); only the reporting lives in the tool. An earlier draft raised from
the loader instead, which took down `corpus_overview` — the tool the server's own
instructions say to call first — along with `resolve_citation` and `authority_chain`, and
reported a condition other than the one that occurred, which convention 5 forbids.

Closes corpus-toolkit#105.

### Fixed — a backend can no longer displace the response envelope

**Can break you only if your corpus supplies `plugins.retrieval_module` AND its `get()` or
`overview()` deliberately sets `corpus`, `archetype` or `authoritative_source`.** Those
values are now ignored rather than obeyed. If you were relying on a backend to set them —
which nothing documented and the envelope exists to prevent — the response will now carry
the config's values instead. `FileBackend` corpora are unaffected.

`CorpusFramework` merges a backend-supplied mapping into a response at exactly two sites,
and at both the mapping won over the envelope: `get_document`'s **not-found** branch merged
the backend's error record over it, and `corpus_overview` merged `overview()` over it. A
wrong string was served silently — misattributing which corpus said "no such document", and
misreporting the corpus's own identity on the tool a client calls first — and since #103 a
non-string was a hard `ValidationError` at serialization. `corpus_overview` also stopped a
backend replacing `disclaimer`, which had let an upstream's terms of use displace the
NON-AUTHORITATIVE warning that response convention 4 names that tool as carrying.

`get_document`'s **success** branch is not touched by this change: a record's
`authoritative_source` still wins, because a document's own `source_url` is the more precise
answer. It is changed by the next entry. See MIGRATION.md — the check to keep on your
backend is narrower than before, not gone.

Closes corpus-toolkit#102 and #104. A third site of the same class, where the mapping comes
from graph data rather than a backend, is corpus-toolkit#105 and is not fixed here.

### Added — a corpus's own tools can satisfy response convention 1

**Nothing breaks and nothing is required.** Extension tools registered through
`plugins.tools_module` keep working unchanged; this adds the means to close a gap, and the
fix is a follow-up in each corpus repo.

Every extension tool on the platform is annotated bare `-> dict`, which makes the SDK emit
**no output schema and no structured content at all**. The answer is in the JSON text block,
which is why nobody has noticed — but a client reading structured content sees a hybrid
corpus as having half a tool surface, with no error anywhere, while every built-in returns a
parsed object. Those responses also carry none of convention 1's three fields, and the two
facts were one problem: the toolkit offered extension tools no supported way to satisfy the
convention.

`CorpusFramework.with_envelope(payload)` is the supported accessor — the same single
assembly point the built-ins use, merged in the same direction they merge it:

```python
@mcp.tool()
def list_datasets() -> ResponseEnvelope:                  # was: -> dict
    return framework.with_envelope({"datasets": [...]})
```

It merges the envelope **over** the payload, so a corpus's own key can never displace the
three fields — the rule the entry above enforces for backends, applied to the tools corpora
write.

**Both lines or neither.** The three fields are required with no defaults, so annotating the
return type while leaving the payload alone is a hard `ToolError` — the tool stops answering
rather than answering weakly. None of the five live extension tools emits any of the three
today, so an annotation-only sweep would take out both hybrid corpora. See MIGRATION.md.

A list-shaped extension tool needs no change: `-> list[dict]` is exempt for the same reason
`search_corpus` is, and the exemption is about shape rather than which module registered the
tool.

Closes corpus-toolkit#96 (the toolkit half). The five annotation changes in
`oregon-legislature` and `oregon-budget` are follow-ups after this release is pinned.

### Added — a corpus can declare which slug values mean "no issuing body"

**Every corpus rebuilds its FTS index once on this bump** (`SCHEMA_VERSION` 3 → 4). A corpus
declaring no sentinels indexes identically to before and pays only the rebuild. **A custom
backend implementing `holdings_for` needs a fourth coverage bucket, `declared_no_body`, only
if its corpus declares sentinels** — without it that backend has counted every sentinel
document as `no_registry_entry`, so it degrades to `complete: null` rather than reporting a
wrong answer. Three-bucket backends serving corpora with no sentinels are unaffected. Note
the coverage key `declared_no_body` is what a backend emits; the response field callers read
is `documents_declared_no_issuing_body`.

`plugins.issuing_body_slug_field` let a corpus name the frontmatter key carrying its registry
slug, but nothing checked the values and nothing let a corpus say which non-registry values
were deliberate. So a misspelling attributed a document to a body that does not exist and
reached no per-agency count silently, while ERF's 37,991 `agency: statewide` documents — 
correct, and carrying no agency by design — were indistinguishable from misspellings. That
made `attribution.complete` report `false` permanently, for a reason that was 99.997%
legitimate, which is the fastest way to teach callers to ignore the field.

```yaml
plugins:
  issuing_body_slug_field: "agency"
  issuing_body_slug_sentinels: ["statewide"]   # values meaning "attributed to no body"
```

Sentinels get their **own** coverage bucket (`documents_declared_no_issuing_body`) and are
never folded into the registry-matched count: "counted for a registry body" and "deliberately
counted for no body" are different answers, and `CONTEXT.md` forbids collapsing two distinct
answers. A corpus where every document either names a registry entry or carries a declared
sentinel now reports `complete: true`.

The declaration is only safe because the values are now **validated**:
`corpus-validate-frontmatter` errors when a declared slug is neither a registry entry nor a
declared sentinel — the check the path-derived half of the same join has always had. Without
it, the sentinel list would be a way to silence the coverage warning rather than answer it.

A sentinel also stops falling through to the path-derived slug. It is the corpus positively
asserting "no body", so re-attributing such a document by its directory contradicts the
corpus about its own document — that is the value change behind the `SCHEMA_VERSION` bump.
The fallback for a genuine **typo** is unchanged: an unchecked value still never displaces
the CI-validated path slug.

Closes corpus-toolkit#94. No corpus declares `issuing_body_slug_field` yet, so nothing
changes on the platform until one adopts both keys.

### Fixed — `corpus.*` string fields are type-checked at load

**Can break you if your `corpus.yml` has one of these wrong** — and if it does, it is
already broken at runtime. `id`, `name`, `jurisdiction` and `authoritative_source` must now
be strings, and a bad one is a `ValueError` naming the field instead of a failure later.
All ten corpus configs on the platform load unchanged; this was verified against each.

`authoritative_source` was stripped without a type check, so a non-string raised
`AttributeError: 'list' object has no attribute 'strip'` — naming neither the file nor the
key, and pre-empting the URL validator downstream whose whole job is to say something useful
about this field.

`id`, `name` and `jurisdiction` had no check at all, so a non-string was accepted in
silence. `id: 90210` loaded as an int; unquoted `id: no` loaded as boolean `False`, because
PyYAML resolves `no`/`off`/`false` and `yes`/`on`/`true` — in any capitalisation — as
booleans. Since the `ResponseEnvelope` entry above types `corpus` as `str` and `config.id`
fills that slot on all six object-shaped tools, that made a single unquoted `id: no` a
`ValidationError` on **every tool call** — at runtime, on a corpus whose config had loaded
cleanly. The error names the trap and tells you to quote **the word you already wrote**:
`corpus.id` is also the MCP server name and how siblings cross-reference this corpus, so
advice that changed the value would quietly rename it.

The `corpus:` block itself is checked too. Present-but-not-a-mapping — an empty block, or
one mis-indented so its fields land elsewhere — used to raise `AttributeError: 'NoneType'
object has no attribute 'get'`, the same shape as the reported bug and a far more common
authoring mistake. An absent `corpus:` key keeps its existing default.

Closes corpus-toolkit#89.

### Fixed — `get_document` cites the document, not the corpus front door, for every backend

**Can change what your responses say only if your corpus supplies
`plugins.retrieval_module`.** If your backend's `get()` returns a `source_url` and no
`authoritative_source`, that slot changes from the corpus front door to the document's own
URL. That is the fix. Nothing breaks, but a response's `authoritative_source` may now differ
from what the same call returned before, so a downstream asserting on it should re-check.
`FileBackend` corpora are unaffected — provably, because both keys come from one column.

The fallback tested the ASSEMBLED RESPONSE's slot rather than the record's `source_url`, and
`_envelope()` has already put the front door there. So for any corpus declaring a front door
the test could never be true and the fallback could never fire. A backend honouring the
documented `get()` contract — "Record metadata + body", which nowhere requires
`authoritative_source` — had the front door stamped over a per-document URL sitting in the
same payload: a wrong answer rather than a missing one, with nothing erroring. It bit hardest
on the `api` and `hybrid` archetypes, the ones that ship a `retrieval_module`.

Resolution is now by precedence, read from the record: its own `authoritative_source`, then
its `source_url` **if that is a string**, then the corpus front door (which may be `null`).
The type check is why a non-string `source_url` — a list of mirrors, say, which the protocol
has never forbidden — stays a harmless extra key instead of becoming a `ValidationError`.

`RetrievalBackend.get()`'s contract is unchanged and `authoritative_source` stays optional on
a record. But `source_url` is now load-bearing on the success path, and the protocol
docstring says so: it should be where a reader verifies the official text, not the endpoint
the record was fetched from.

Closes corpus-toolkit#90.

### Added — object-shaped tools declare response convention 1, openly

The six object-shaped tools are annotated `-> ResponseEnvelope` (new,
`corpus_toolkit/mcp/responses.py`) instead of `-> dict[str, Any]`. Their emitted output
schema goes from this, on both SDK majors:

```
get_document  {"additionalProperties": true, "title": "get_documentDictOutput", "type": "object"}
```

to this:

```
get_document  {"additionalProperties": true, "title": "ResponseEnvelope", "type": "object",
               "required": ["corpus", "archetype", "authoritative_source"],
               "properties": {"corpus": {"type": "string"},
                              "archetype": {"type": "string"},
                              "authoritative_source": {"anyOf": [{"type": "string"},
                                                                 {"type": "null"}]}}}
```

`corpus`, `archetype` and `authoritative_source` were in every response body and named by
no declaration, so a conformance harness, a validating client or a release gate could
assert nothing about the convention beyond string-matching prose (corpus-toolkit#15).
`search_corpus` is untouched — it returns a list and is exempt from the convention.

**Response bodies do not change.** The tools still return the same plain dicts; only the
declared type moved. Verified by `tests/test_result_marshalling.py`, which round-trips
every registered tool's real answer through the SDK's own conversion and asserts
whole-payload equality in both halves, on both majors: 314 passed, 10 subtests on
`mcp[cli]>=1.28,<2` (1.28.1) and `>=2,<3` (2.0.0), up from 312 with the two new tests.

**Why this is not v1.24.0 again.** That release declared a TypedDict, which the SDK turns
into a CLOSED pydantic model: it rejected the documented `authoritative_source: null` and
dropped every undeclared key, so `get_document` returned three envelope fields and no
document body while still reporting success (corpus-toolkit#61). The distinction is
closedness, not declaration — a `-> dict[str, Any]` annotation already builds
`RootModel[dict[str, Any]]` and dumps every response through it, so a pydantic model has
been serializing object responses all along. `ResponseEnvelope` sets `extra="allow"`, and
its output was measured against that RootModel's on both majors — same keys, same values —
for keys shadowing `BaseModel` methods and attributes, leading-underscore and dunder keys,
the empty-string key, non-ASCII keys, non-JSON values, falsy values and deep nesting.

**Key order in structured content changes for one tool.** `model_dump` emits declared
fields before extras, so `resolve_citation` — which merges `**self._envelope()` last — goes
from `['citation','matches','unresolved','corpus',…]` to
`['corpus','archetype','authoritative_source','citation',…]`. JSON objects are unordered,
the content blocks are built from the raw return value and do not move, and every
round-trip test compares mappings — so nothing can observe it. Recorded because the first
draft of this entry called the output byte-identical, which was a stronger claim than the
measurement supported.

Both v1.24.0 failure modes are re-tested directly and pass: a payload carrying
`authoritative_source: null` round-trips as null through every object tool, and
`get_document`'s body survives in both the structured content and the content blocks. The
gate was also re-armed adversarially — re-applying `daff198`'s `ObjectResponse` TypedDict
on top of this change still turns `tests/test_result_marshalling.py` red with
`get_document: keys dropped at serialization: ['body', 'citation', ...]` on both majors.

**One behaviour change, and it can only fire on a non-conforming response.** The three
fields are declared required (and `authoritative_source` nullable with no default), so a
response that omits one is now a `ValidationError` rather than a quietly non-conforming
answer. Every built-in path builds the envelope in `CorpusFramework._envelope()` and
cannot hit this; a corpus supplying its own `plugins.retrieval_module` can — see
MIGRATION.md.

`.github/scripts/contract_smoke.py` gained the matching assertion at step 8: any tool
whose declared schema describes properties at all must name the three. Extension tools
annotated bare `-> dict` declare no schema and are out of scope there, which is
corpus-toolkit#96 and a separate fix.

## v1.26.1 — 2026-08-19

### Fixed — **v1.25.0 and v1.26.0 cannot build a corpus image; take this one**

**If you are pinned to v1.25.0 or v1.26.0, your corpus image build is failing right now.**
Bump the pin. There is no config workaround, and rolling back to v1.24.1 also clears it.

v1.25.0 deleted `CorpusFramework.ensure_index` (corpus-toolkit#75). `corpus-template`'s
Dockerfile — and therefore every corpus built from it — bakes its FTS index at image build by
calling exactly that:

```dockerfile
RUN python3 -c "... CorpusFramework(config_mod.load('_meta/corpus.yml')).ensure_index()" && ...
```

So step 7 of 9 fails with `AttributeError: 'CorpusFramework' object has no attribute
'ensure_index'`, the image never builds, and a pull-based deploy loop re-detects the same drift
forever. Measured on the deploy host: six consecutive ERF deploy attempts in one hour with
`deployed=` never advancing, each rebuilding a 1 GB context, starving every other corpus behind
it and contributing materially to the host reaching 100% disk.

The method is restored as a real method delegating to the backend, not a deprecation — a corpus
is entitled to ask its framework to build the index, and deprecating it would only move the same
breakage to a later release. A backend with no FTS index (the API archetype) still raises, but
with a message naming which backend and why.

**How it passed every gate.** A search of `corpus_toolkit/` and `tests/` found no caller, and
that was the whole of the evidence — the callers live in the eight repositories that pin this
one. The release gate checks out `corpus-template` and runs `contract_smoke.py` against it, but
**never runs its Dockerfile**, so the one artifact representing how a corpus actually starts was
sitting in the job's working directory unexecuted. Tracked as corpus-toolkit#100.

Worse, #75 added `assert not hasattr(f, "ensure_index")` — a test pinning the deletion, which
made the regression read as deliberate to anyone reviewing the suite. That assertion is replaced
by one exercising the call a corpus makes.


## v1.26.0 — 2026-08-19

### Every corpus rebuilds its FTS index on this bump — plan the rollout

`SCHEMA_VERSION` goes 2 to 3, so the cached index is discarded and rebuilt. **This is not
the usual pin bump.** What it costs depends on how your corpus ships:

| | |
|---|---|
| baked image (e.g. ERF) | the rebuild happens at **image build** — `deploy.sh <corpus> main --rebuild-image`, ~70s on 76k documents |
| mounted corpus | a stop-warm-start, **~8 minutes** |
| local checkout / CLI | rebuilt silently on the next command |

A pin bump alone is **inert** for a baked corpus: `requirements.txt` is baked into the
image, so merging the bump changes nothing until the image is rebuilt. `deploy.sh`'s own
help has said `--rebuild-image` is "needed after any toolkit release that changes the FTS
cache schema" — this is such a release, and until now that flag appeared nowhere in the
toolkit's docs.

> **CORRECTION (corpus-toolkit#114).** `--rebuild-image` is guarded on a corpus being
> MOUNTED, and none is — `deploy.sh` declares an empty mounted set and prints a notice if
> the flag is passed anyway. An ordinary `deploy.sh <corpus> <ref>` builds the image every
> time, so a baked corpus's index is re-baked without it. Verified on the v1.27.0 wave: ERF
> was rebuilt by the ordinary deploy, no flag. The table row above is right that the rebuild
> happens at image build; only the flag is unnecessary. It becomes necessary again the day a
> corpus is mounted.

Rebuilding under live traffic is unlocked and uses a fixed temp filename, so a concurrent
warm and a live rebuild collide (`disk I/O error`). Rebuild deliberately rather than
letting the first request do it.

### One REQUIRED action, for any corpus whose manifest has empty `sha256` values

`corpus-detect-changes` now exits **1** rather than 0 on a run that recorded no baseline or
checked nothing. A corpus that has never seeded its baselines goes red on its next
scheduled run. That is the point — it was reporting 100% drift as a clean result — and the
remedy is one command, `corpus-detect-changes --record-baseline`, reviewed as a PR. See
MIGRATION.md.

### Changed — an outbound User-Agent string

The sibling-index fetcher identifies itself as `corpus-toolkit/<installed version>` instead
of the literal `corpus-toolkit/1.1`, which had been frozen since v1.1 and wrong for
twenty-four releases (corpus-toolkit#82).

**This is externally visible.** It is the only thing a remote host learns about us on a
sibling-index fetch. A publisher who has allow-listed, rate-limited or logged on the exact
string `corpus-toolkit/1.1` stops matching. Nothing on this platform is known to do so, but
it is the kind of thing an upstream does without telling you, and the contact URL and
`sibling-index-fetch` purpose token are unchanged so anything matching on those still works.

`sources/changes.py`'s `corpus-toolkit-change-detector` — the agent that fetches sources
during change detection — is **unchanged**. It is the token matched against robots.txt
directives, so a host's `Disallow` naming it keeps matching exactly as before.

### Documentation — `authoritative_source` is the corpus's front door

**Nothing mechanical changes and no bump is required for this.** The type stays
`str | None`, no response shape moves, and no validation is added or tightened. What
changes is what the field *means*, which had never been written down.

Response convention 1 in `docs/mcp-interface-contract.md` now states it: the corpus-level
`authoritative_source` names where a reader starts for **this corpus's** official text —
one URL, per corpus — and is not a citation for whatever the response carrying it happens
to describe. Per-answer precision already exists and comes from `get_document`, which
returns the document's own `source_url` in that slot and falls back to the corpus URL only
for a document carrying none.

**What this asks of a corpus**: one spanning several publishers declares its best single
entry point rather than leaving the field unset, and that is correct rather than a
compromise. `executive-regulatory-frameworks` — 1,972 sources across 7 hosts, measured
2026-08-11 — was the one holdout with a *reason*, and this removes it. It is not the last
holdout: `oregon-budget` and `oregon-legislature` are also undeclared, and corpus-toolkit#11
still needs all three before its precondition is met. Neither of those two needed this
settled; each has one dominant host.

The message a corpus sees while the field is unset is reworded to match, in both places it
appears: `corpus-validate-frontmatter`'s warning and `corpus_overview`'s `config_warning`.
Both used to say "set it to the URL where the official text lives", which reads as a
promise that every document sits under that URL — the reading that kept a seven-publisher
corpus from declaring anything at all. A corpus asserting on either string in its own CI
should expect it to have changed. (corpus-toolkit#70)

### Fixed — drift detection could not record a baseline, and a truncated run looked clean

**Read this if your corpus runs `detect-upstream-changes.yml`. Two exit codes change, and
one of them will turn some scheduled runs red on purpose.**

Three defects, one shape: the drift report said things about upstream that were really
facts about the corpus, and said them quietly.

- **`corpus-detect-changes` never wrote the baseline it computed** (#68). The manifest's
  `sha256` was documented as "recorded at last ingest/refresh" and nothing in the toolkit
  ever assigned it, so the only route to one was a per-corpus script reimplementing
  `content_hash` — format inference, volatile normalization, `pdftotext -layout`,
  whitespace normalization, the <200-char raw-byte fallback — where any divergence is
  silent and permanent. oregon-counties (3,447 sources) and oregon-kpm (789) ran their
  whole lifetimes with every `sha256: ''`: everything CHANGED every week, 25 spurious
  issues filed, the rest dropped, run concluded `success`.

  **New: `--record-baseline`.** It writes the freshly computed hash into the manifest group
  files, in the working tree only — the manifest is curated data, so the diff goes through
  review like any other, and nothing is committed or pushed. Bare (`seed`) fills sources
  with **no** recorded baseline and leaves recorded ones alone; `--record-baseline=refresh`
  also replaces recorded baselines, which is you accepting the observed change. A source
  whose fetch failed is never written — a 403 must not overwrite a good baseline. Sources
  are located by id and only their `sha256` value is rewritten; the edit is re-parsed and
  compared before anything is written, and a file that does not verify is left untouched
  and named. Comments, key order, and every other key survive. `--record-baseline` refuses
  to run with `--open-issues`: seeding is not a drift report.

  **Do not seed from frontmatter `source_sha256`.** Different hash, different input. The
  two agree only for image-only scans, where both fall back to raw bytes — so a corpus that
  seeds from frontmatter and spot-checks a scan sees a clean result and gets permanent
  drift on every text-layer PDF, now with a populated "previous" hash that reads as a real
  upstream change.

- **`VOLATILE_PATTERNS` shipped empty with no way for a corpus to add to it** (#66), so
  `normalize_volatile()` was an identity function for every consumer and the guarantee its
  comment describes did not hold. One OARD footer bump (`v2.1.7` → `v2.1.8`) turned all 484
  sources in ERF's `oar` group into drift with zero rule text changed.

  **New: optional `volatile_patterns:` in `_meta/corpus.yml`** — a list of regexes, stripped
  from the raw bytes on the HTML/XML path before hashing. Compiled once at load, and a bad
  one fails there rather than mid-crawl: a bare string, a non-string entry, an empty
  pattern, or an invalid regex is refused by name. **The built-in list stays empty**, so a
  corpus that declares nothing hashes byte-identically to v1.25.0 — shipping "universal"
  defaults would have re-hashed existing sources across the platform in a version bump. A
  declared pattern that matches nothing in a run is reported, because a configured pattern
  doing nothing is the bug this key exists to fix. So is the opposite and worse case: every
  run reports how many bytes each pattern removed and what share of the fetched HTML/XML
  that is, and warns above 10%. A pattern wide enough to swallow the body deletes content
  before hashing — two versions differing only inside it hash identically and can never
  report drift again — and that is measured and stated rather than forbidden, since how
  much of a page is genuinely volatile is a corpus's call to make in a PR.

- **A capped run reported as a clean run, and named a cause it had not checked** (#67). The
  truncation notice went to stderr and the run exited 0; the message asserted an empty
  baseline, which was exactly right for oregon-counties and exactly wrong for ERF, whose
  maintainer checked and found zero. Both runs went green either way.

  Every run now prints a **per-group breakdown** (`oar 484/484, oam 2/173`), capped or not,
  with unseeded counts marked — the one line that separates a template change from a stale
  baseline from real revisions. The capped message describes the shape of the drift and
  reports the **measured** unseeded count instead of guessing, including when it is zero.

**Exit-code changes.** A run now exits 1 when the issue cap truncated the report; when no
in-scope source has a recorded baseline (that run cannot detect drift; `--record-baseline`
is the fix, and a recording run exits 0); when the run's scope came out **empty**, e.g. a
typo'd `--group` that checked 0 sources; and when `--record-baseline` **refused** a rewrite
it could not account for, which in CI was otherwise a green run that recorded nothing.
`--github-output` gains `unseeded=N` and stops reporting `changed=true` on an inert run. Under GitHub Actions both also emit a `::warning`
annotation. An uncapped, seeded run is unchanged: drift is still a signal, not an error, and
isolated fetch failures are still tolerated. `detect-upstream-changes.yml` now runs its
STATUS.md steps with `if: always()`, so a red drift step no longer skips them.

A corpus with a wholly unseeded manifest also stops having issues filed against it — the
first run against a fresh manifest is a seeding operation, and 25 tickets a week whose
"previous sha256" is empty were noise. Seed, review the diff, then let the cron report.

### Fixed — `issuing_body_profile` counted 1% of a corpus and said nothing about the other 99%

**The number moves, a lot. That is the point of this note.** `in_repo` was counted from an
index column populated only for documents under a `scoped: true` content root. Measured on
`executive-regulatory-frameworks` on 2026-08-18: 960 of 75,905 documents, **1.3%**. Its
Department of Environmental Quality reported **53** documents against the **1,929** that
actually carry it — a **97% under-report, ~36×**, and the same order for every large agency,
because an agency's OAR rules are filed under their chapter and no agency directory can
contain them. Nothing about the response said so: the call succeeded and the field was
populated, so a caller comparing agencies got a ranking of *who has a policy directory*.

Two changes, and a corpus needs the first to see the second.

- **A corpus may declare `plugins.issuing_body_slug_field`** — the frontmatter key carrying
  its registry slugs (`agency` on ERF, present on 100% of documents). It wins **where its
  value names a registry entry**; otherwise the path-derived scope slug, which CI already
  validates, keeps the document. That order is deliberate: nothing checks the frontmatter
  field (corpus-toolkit#94), and letting an unchecked value override a checked one means a
  single typo silently REMOVES a correctly-filed document from a count that was previously
  right. **No count can go down because of this release**, and a corpus that declares
  nothing reports exactly the counts it reported before. The path mechanism is not
  deprecated: a corpus genuinely organised by issuing body is served correctly by it.
- **Every success now carries `attribution`**, saying what the count could see — as three
  buckets, not a boolean, because "has a slug" and "is counted for somebody" are different
  questions. `documents_matched_to_a_registry_entry` are the only ones any per-body count
  can include; `documents_naming_no_registry_entry` and `documents_with_no_issuing_body`
  are counted for nobody. `complete: true` means the first bucket is everything; `false`
  means the count is a **lower bound**, with the numbers; `null` means nobody measured — an
  old-shape backend, coverage reported without its counts, or an empty index — which is
  unknown, not none. `in_repo` for a body with nothing held says which nothing it means:
  the old "no documents ingested for this issuing body yet" is now reserved for a corpus
  where everything reaches a count.

**Expect `complete: false` on ERF, and that is the honest answer.** 37,992 of its 75,905
documents (50.05%) carry `agency: statewide` (37,991) or `agency: external` (1) — values the
registry does not contain. From the toolkit those are indistinguishable from a typo, so they
are reported as counted-for-nobody rather than assumed deliberate. corpus-toolkit#94 adds the
sentinel declaration that lets a corpus say which values mean "no issuing body", after which
ERF reports complete.

**Contract stays v1** — additive fields, `in_repo`'s own shape unchanged,
`docs/mcp-interface-contract.md` updated in the same change. **The FTS schema version is
bumped to 3**, so every corpus rebuilds its `_meta/.cache` index once; a baked image needs
`deploy.sh … --rebuild-image` (see MIGRATION), because without the rebuild the old values
keep being served from a cache nothing else would invalidate.

> **CORRECTION (corpus-toolkit#114):** the rebuild is real, but `--rebuild-image` is not
> needed for it — an ordinary deploy builds the image. See the v1.26.0 correction earlier in
> this entry, and the annotations in `MIGRATION.md` under corpus-toolkit#114.

`RetrievalBackend.holdings_for(slug)` now returns `{"counts": ..., "coverage": ...}`. A
corpus-supplied backend still returning v1.25.0's bare `{content_mode: count}` keeps
working unchanged and reports coverage `null`. (corpus-toolkit#71)

Known gap, tracked as corpus-toolkit#94: nothing checks that a declared slug value names a
registry entry. It can no longer shrink a count — an unregistered value never overrides a
validated path — but for a document no directory attributes, a typo still lands in
`documents_naming_no_registry_entry` and is counted for no body. Live on ERF today at one
document (`agency: external`), alongside 37,991 deliberate `statewide`.

### Internal — what a client RECEIVES is now asserted, for every tool

**Nothing about a corpus's behaviour changes and no bump is required for this.** No
response shape moves, no annotation changes, no validation is added or tightened. It is
test and release-gate coverage, plus two functions on the SDK compat seam.

Every assertion this repo made about a tool — in `tests/`, in the release gate, everywhere
— went through `_sdk.call_tool`, which passes `convert_result=False` and therefore sees the
tool's raw Python return value rather than the response a client is sent. That is
deliberate and stays: the gate asserts that an external graph neighbour comes back
`{citation, external: true}`, and asserting that through the SDK's marshalling would test
the SDK. The gap was that nothing asserted the marshalling either — which is how v1.24.0
shipped an output schema that dropped every document body on the way out, reported success
doing it, and passed the `corpus-end-to-end` gate green (corpus-toolkit#61, #63).

Added, without flipping that flag, so behaviour and marshalling stay separately pinned and
a failure says which one broke:

- `tests/test_result_marshalling.py` round-trips EVERY registered tool's real answer, from
  a real corpus on disk, through the SDK's own conversion and asserts whole-payload
  equality in both halves of the response — the content blocks a client renders and the
  structured content it parses. It covers what `tests/test_output_schemas.py` (which pins
  response convention 1 on the six object-shaped tools) structurally cannot: `search_corpus`,
  whose list answer takes a different conversion path entirely — one content block per hit,
  wrapped as `{"result": [...]}` — and the `tools_module` extension tools a hybrid or api
  corpus registers, which nothing reached at all. Its fixtures declare an
  `issuing_body_registry` so `issuing_body_profile` — config-gated, one of the six tools
  v1.24.0 annotated, and previously round-tripped by nothing anywhere — is actually served,
  and its coverage guard fires in both directions so a listed-but-unregistered tool cannot
  read as covered.
- Step 8 of `.github/scripts/contract_smoke.py` does the same against the corpus the gate
  already builds, on the same calls it already makes, including the hybrid extension tool.
  It also pushes `authoritative_source: null` through each object tool's own converter,
  which is the other half of what #61 broke. Both round trips treat "no structured content"
  as legitimate only when the tool DECLARED no output schema; declaring one and serializing
  nothing is reported as the regression it is.
- `_sdk.serialized_result()` returns both halves of a conversion, `_sdk.tools_by_name()`
  returns the registered tool objects, `_sdk.declares_list_result()` answers whether a tool
  declares the SDK's list wrapper (so a caller keys on the declared shape rather than on a
  tool's name), and `structured_result()` is now the narrow form of the first. A third result shape the seam did not know about is handled: on mcp 1.x a tool
  with no declared output schema converts to a bare list of blocks rather than a
  `(blocks, structured)` tuple. Every extension tool on the platform is in that state, which
  is corpus-toolkit#96.

Verified by re-applying v1.24.0's `TypedDict` annotation: the new coverage goes red on both
SDK majors, naming each dropped key and each rejected null, while the existing behaviour
assertions stay green — the exact green that shipped the incident.

## v1.25.0 — 2026-08-11

### One behaviour change on the serve path, then fixes

**Read this if your corpus sets `plugins.semantic_search_module`.** The shared semantic
module now resolves its embeddings artifact from `config.root`, not from the process's
working directory. In the containers those are the same path (WORKDIR is the repo root),
and `CORPUS_SEMANTIC_DIR` still overrides both, so a normal deployment is unaffected — but
it is the code path every semantic query runs, so rebuild deliberately rather than letting
it ride along on an unrelated image build. A corpus that builds its artifact with
`--out` still needs `CORPUS_SEMANTIC_DIR` set at serve time; that was true before and has
not changed.

Nothing else changes for a corpus. No `_meta/corpus.yml` edits, no schema change, no MCP
contract change. The four items below came out of an architecture review of the retrieval
and plugin seams (corpus-toolkit#73, #74, #75).

### Fixed

- **Citation schemes silently vanished on a second `CorpusFramework` over one corpus**
  (#73). `load_module` caches a corpus's citation module, so the second construction re-ran
  none of its top-level `register_scheme` calls and collected nothing — then fell back to a
  process-wide list the collector had deliberately bypassed, which was therefore empty.
  `resolve_citation` answered *"no citation scheme recognized this format"* about a corpus
  that recognizes it perfectly well, reported `schemes_attempted: []`, and skipped sibling
  resolution entirely — so a sibling citation came back `unresolved` with no
  `sibling_unavailable` marker. That is "could not check" served as "not there".

  **No deployed server hit this**: `server.py` builds one framework per process. It was
  reachable from a corpus's own `tools_module`, a CLI, or any multi-corpus process.

- **The semantic seam had no per-corpus state** (#74). The plugin contract passed no
  corpus, so the module read `Path.cwd()` for its artifact path and kept its loaded index in
  a module global — one installed module object shared by every framework in the process.
  A server started outside the repo root served keyword-only while reporting healthy, and
  two corpora in one process shared whichever index loaded first. The builder never had this
  problem (`semantic/build.py` has always written to `cfg.root/_meta/embeddings`), so the two
  halves of one artifact disagreed about where it lives.

- **`corpus_toolkit.semantic.search.selftest()` crashed** and had for some time. Its
  synthetic fixture was a 5-tuple while the loader had grown to 6 when `rank_chunks` added
  `rows`, so two of its four checks had not executed since — including the one guarding the
  degrade path `backends.py` calls with no `try`/`except`. It was written to run without the
  artifact specifically so CI could run it, and CI never called it. It does now, and `numpy`
  joins the `test` extra so the check runs rather than skipping.

### Added

- **`RetrievalBackend.holdings_for(slug)`** (#75) — optional, and the only optional member
  of the protocol. `issuing_body_profile` used to run raw SQL against `FileBackend`'s `docs`
  table through `ensure_index()`, so the tool was unavailable to any other backend **at any
  price**, and three separate guards existed to keep that from surfacing as a crash. A
  corpus-supplied backend can now serve the tool by implementing one documented method; the
  startup message says so when it does not. `FileBackend` implements it, so a
  document-archetype corpus does nothing.

- **`plugins.load_module(..., force=True)`** — re-executes a module already in
  `sys.modules`, for the case where the import *is* the effect. Keyword-only and off by
  default; only the citation-scheme collector passes it.

- **`corpus_toolkit.semantic.search.make(config)`** — the per-corpus factory
  `CorpusFramework` now prefers. A semantic module without it is duck-typed exactly as
  before.

### Internal

- `extract_section` is a module function in `backends.py`. `CorpusFramework._extract_section`
  called it as `FileBackend._extract_section(self, ...)` — an unbound method of an unrelated
  class handed a `CorpusFramework` as `self`, which held only while the body ignored `self`.
  Both classes keep the name.
- `DOC_*` column constants for `_doc_row`, and the existing `KW_*` applied to the
  `_doc_meta_row` readers that were still positional. `tests/test_row_offsets.py` executes
  the real queries and asserts each constant lands on the column it names, so a reordered
  `SELECT` fails there instead of serving a document with its citation in the `title` field.
- `REQUIRED_BACKEND_METHODS` moved next to the protocol it restates.
- First tests for `issuing_body_profile` — `issuing_body_registry` previously appeared
  nowhere in `tests/`.

Suite: 205 tests → 231.

## v1.24.1 — 2026-08-04

### Fixed — **v1.24.0 is broken; take this one**

**If you are pinned to v1.24.0, every object-shaped tool on your corpus is failing right
now.** Bump to v1.24.1 and rebuild the image; there is no config workaround, and rolling
back the pin to v1.23.x also clears it. A pin-bump PR should have been opened against your
corpus automatically — merging it is not enough on its own, the image must rebuild.

v1.24.0 declared a `TypedDict` output schema on the six object-shaped tools
(`get_document`, `resolve_citation`, `graph_neighbors`, `corpus_overview`,
`authority_chain`, `issuing_body_profile`). That made the SDK serialize every response
through a pydantic model, which broke two things at once (corpus-toolkit#61):

- **`authoritative_source: null` was rejected.** It is a documented value for a corpus
  that declares no source, so `corpus_overview`, `resolve_citation` and unknown-id
  `get_document` returned a hard `ValidationError` instead of a response.
- **Document bodies were dropped.** Keys the schema did not declare were discarded on the
  way out, so `get_document` returned its three envelope fields and no content —
  **silently**, with the call still reporting success. This is the dangerous half: an
  agent gets a well-formed answer containing nothing.

`search_corpus` was never affected (it returns a list and was deliberately left alone),
which is also the proof that no corpus content was lost — the failure was entirely in
response serialization.

Object tools now return `dict[str, Any]`: still a real output schema, but one that permits
`null` and strips nothing. Field-level validation of response convention 1 is reopened as
corpus-toolkit#15, and `docs/mcp-interface-contract.md` now records why the obvious
implementation is the one that must not be used.

## v1.24.0 — 2026-08-04

**Rebuild corpus images to pick this up.** The serve path changed: `server.py` now serves
the app it verified rather than letting the SDK build a second one. Behaviour is unchanged
for a server started without `--allowed-origin` — same uvicorn options, and
`forwarded_allow_ips` still defaults from the `FORWARDED_ALLOW_IPS` environment variable —
but it is the code path every corpus runs, so it wants a deliberate rollout rather than
riding along on the next unrelated rebuild.

Nothing here is breaking. No config change is required by any corpus.

### Added

- **CORS** via `--allowed-origin` (repeatable), so a browser MCP client can complete the
  handshake at all. Exposes `mcp-session-id`, without which the preflight passes,
  `initialize` returns 200, and the client dies on `Missing session ID` (#37).
- **`corpus-detect-changes --check-robots`** — report each source host's robots.txt
  position, including hosts that permit our agent while blocking named AI crawlers.
  Reports only; nothing blocks a fetch (#29). Found on its first run:
  `www.yamhillcounty.gov` carries `Content-Signal: ai-train=no` and already supplies five
  documents to oregon-collective-bargaining.
- **Output schemas** on the six object-shaped tools, so response convention 1's three
  fields are visible to schema-driven validation instead of only present in the JSON text
  (#15). Extras are still permitted — a schema that constrained each tool's payload would
  break every corpus.
- **`bump_pins.py`** — move every toolkit pin in a corpus, and `--check` to detect pins
  that disagree. Measured across 10 repos: 126 pin sites, six toolkit versions live at
  once, and one repo running two versions inside one `ci.yml` (#9). A `propagate-pin`
  workflow opens the bump PRs, and needs a `CORPUS_PIN_TOKEN` secret to do anything.
- **`corpus-build-semantic-index`** — the semantic arm was the only CLI without a console
  entry point, which also kept it outside the `entrypoints` CI job (#41).
- **`check-links` gains `exclude-urls`** for hosts quoted inside mirrored third-party text,
  where the link belongs to the source document and cannot be corrected (#51).

### Fixed

- `BIG_DOC_BYTES` was defined twice, 1,200 bytes apart, and `framework.py` shadowed its own
  import of it (#52).
- Drift issue creation discarded every `gh` failure. 618 creations failed silently because
  the `source-change` label did not exist — a hard dependency of `--label` that nothing
  created (#53). Now creates the label, reports failures, and caps a run at 25.
- **`pyproject` version was five releases stale** (1.18.0 against tag v1.23.0), so an
  install reported a version it was not. Now gated: a tag whose version disagrees is
  deleted (#41).

### Documentation

CHANGELOG.md now exists; MIGRATION.md covers v1.9.0–v1.23.0; `docs/semantic.md` documents
~730 LOC that had none; `AGENTS.md` states plainly that nothing enforces robots.txt.

## v1.23.0 — 2026-08-03

`_sdk` spans the **client** side of the mcp 1.x/2.x break, not just the server side.

Six client-side breaks between majors — entry-point name, signature, which httpx it wants,
the arity it yields, and two silently renamed model fields. corpus-gateway crash-looped
four times discovering them one at a time. `open_client_streams`, `tool_input_schema` and
`result_is_error` now hide all six (#49).

## v1.22.0 — 2026-08-02

M4 integrity: `corpus-verify-extraction`, `source_data_file` provenance, fetch-failure
tolerance in `corpus-detect-changes`, and `corpus-verify` — the one tool that writes
`last_verified`/`verified_by`, which until then nothing on the platform could set.

## v1.21.0 — 2026-08-02

**Serve the inside of big documents.** `### ` subsections, chunk paging, chunk-aware search
hits. Before this, `get_document` on a 900 KB body was a glance-or-everything binary.

Corpora with anchored large documents need **at least this version** for those anchors to
be addressable at all — below it, `part=` cannot reach a subsection.

## v1.20.1 — 2026-08-02

`viz` slot guard requires a letter, so a bare `______` is treated as content.

## v1.20.0 — 2026-08-02

`corpus_toolkit.viz`: the shared chart chrome (M5-A).

## v1.19.0 — 2026-08-01

**Possibly breaking.** Archetype is enforced; `toolkit-ref` becomes required on the
reusable workflows. Also: status in index rows, multi-scheme matching, and per-corpus
`schema.doc_types` — a new instrument family no longer costs a toolkit release plus an
org-wide pin bump (#40).

## v1.18.0 — 2026-08-01

Declare `numpy` as the semantic extra, and say why the loader failed instead of degrading
silently.

## v1.17.0 — 2026-08-01

`corpus_toolkit.semantic`: semantic search any corpus can enable.

## v1.16.0 — 2026-08-01

`corpus_toolkit.site`: the shared landing-page shell, replacing eight copies of the same
chrome and the same cross-corpus contracts.

## v1.15.0 — 2026-08-01

Scheme-bug guard: a candidate id that is unresolvable by construction is reported as a
scheme bug rather than as a missing document. The two mean opposite things to a curator.

## v1.14.0 — 2026-08-01

`authority_chain` walks the relations a corpus declares, rather than a hardcoded set.

## v1.13.0 — 2026-07-31

The `performance_report` doc_type.

## v1.12.0 — 2026-07-30

The `federal_instrument` doc_type.

## v1.11.0 — 2026-07-30

Serve corpus-specific frontmatter on `get_document` (`extra_document_fields`).

## v1.10.0 — 2026-07-30

The `audit_report` doc_type.

## v1.9.0 — 2026-07-29

Contentless FTS5. The `fts` table no longer stores text, so reading its columns returns
NULL instead of raising — tests that inspected `fts.body` were asserting on an
implementation detail.

## v1.8.0 and earlier

See `MIGRATION.md`, which covers v1.0.3 through v1.8.0 with full upgrade notes.
