# Completing a chain is not weakening verification

Some government hosts serve their leaf certificate without the intermediate that links it to
a root. Browsers paper over this — they cache intermediates and chase the certificate's AIA
extension — so the link looks fine to a person and fails for `curl`, for `lychee`, and for
the fetches in this package.

The corpora met it as a quiet outage rather than a loud one. In
`executive-regulatory-frameworks#140`, 45 DHS/OHA sources stopped being fetchable on
2026-08-05 and nothing said so: `corpus-detect-changes` runs in default mode precisely so an
isolated failure does not kill a 1,347-source run, the systemic guard trips above 20%, and
45/1347 is 3.3%. The run reported `success` for three weeks while those sources had no drift
detection at all. Unlike a stale document this leaves no trace — the last-known-good snapshot
just keeps looking current.

We decided that a corpus **may supply a missing intermediate for a named host**, and may
never relax verification for any host.

The distinction is measurable, and was measured on 2026-08-27 against
`sharedsystems.dhsoha.state.or.us`:

```
python, system store only                        CERTIFICATE_VERIFY_FAILED
python, system store + DigiCert G2 intermediate  HTTP 200, chain verified
python, that intermediate ALONE, no root         REFUSED, "unable to get issuer certificate"
```

The third line is the decision's whole basis. A supplement is not a trust anchor: OpenSSL
still requires the path to terminate at a self-signed certificate the system already trusts,
so supplying an intermediate adds no authority to the trusted set. Hostname, dates and
signature path are all still checked. What changes is that a link the server should have sent
is present, and 45 sources are compared instead of skipped.

## Considered options

**Disabling verification for the affected host** is the obvious shortcut and is refused
outright, here and permanently. It trades a known, bounded problem for an unbounded blind
spot: a host exempted from verification is a host whose certificate could be replaced by
anyone positioned to do it, and this platform's documents exist to be checked against
official sources. The refusal is worth writing down precisely because the pressure to reach
for it recurs every time a state server is misconfigured.

**Doing nothing, and treating the outage as upstream's fault**, was the position both ERF
tickets took: the misconfiguration is Oregon's, and every remedy inside the repo looked worse
than the problem. It reads as rigour and is not. Refusing to fetch is not strict verification;
it is *no* verification, on sources that then go unwatched indefinitely while we wait on a
state IT ticket that may never be answered. The corpus verified nothing on those 45 sources
for three weeks and called it caution.

**Rewriting the affected `source_url`s to `http://`, or re-pointing them at a mirror that
verifies**, are both rejected for the same reason. A provenance link is a citation to where a
document actually came from. Downgrading it to plaintext to satisfy a checker, or pointing it
at a nicer copy, damages the one mechanism by which a reader of a non-authoritative corpus can
decide whether to believe it.

**Fetching the intermediate at run time from the certificate's AIA extension** is the most
self-healing option — nothing is committed, and a rotation upstream is picked up for free. We
rejected it because it makes every drift run depend on a second live host. A DigiCert outage
would resurrect exactly the failure this decision exists to end, and the workaround would fail
in the same silent shape as the original.

So the intermediate is **committed**, with its provenance recorded beside it, and the
supplement is refused rather than loaded if it would widen trust or has gone stale:

- **A self-signed certificate in a supplement is refused.** A root is a trust anchor, which is
  the thing this is not. Without that refusal the mechanism would be a general-purpose way to
  trust anything, wearing the name of a narrow fix.
- **The filename is the host.** `_meta/tls-chain/<host>.pem`, mounted on `https://<host>` and
  nothing else, so host scoping is structural rather than promised: a supplement covering two
  hosts is two files, and there is no way to write one that covers all of them.
- **A certificate within 30 days of expiry is refused**, so the run that would otherwise start
  failing mysteriously fails legibly first, naming the file and the date. A spent supplement
  fails exactly like the missing intermediate it was added to fix.
- **Every run says which supplements it loaded.** An exception nobody can see in a log is how
  a workaround outlives its cause.

## Consequences

A supplement is a **workaround with an end condition**, not a setting, and `still_needed()`
is how it ends: it re-requests the host *without* the supplement and distinguishes three
answers — still broken, fixed (remove the file), and could-not-tell. The third matters more
than it looks. An unreachable host is not a fixed one, and a check that conflated them would
retire a workaround on the strength of an outage.

This does not fix the class. A run can still report `success` while a persistent access
failure goes unremarked, for any cause — that is tracked separately, and it is the finding
`#140` was really about. This decision only makes one common cause fixable without lying about
what verification means.
