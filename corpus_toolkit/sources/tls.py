"""Per-host TLS chain supplements — completing a chain a server fails to serve.

A correctly-configured HTTPS server sends its leaf certificate together with every
intermediate needed to build a path to a trusted root. Some government hosts send the leaf
alone. Browsers paper over that -- they cache intermediates and chase the certificate's AIA
extension -- and strict clients do not. The fetches in this package are strict clients.

The failure this exists to end is not a red run, it is a QUIET one. A source that cannot be
fetched is a source that is never compared, and default-mode `corpus-detect-changes` reports
`success` while it happens: `executive-regulatory-frameworks#140` sat at 45 unmonitored
DHS/OHA sources from 2026-08-05, under the 20% systemic threshold at 3.3%, with the last
known snapshot continuing to look current the whole time.

WHAT THIS IS NOT
================

It is not `verify=False`, and it is not a widened trust store. Measured 2026-08-27 against
`sharedsystems.dhsoha.state.or.us`, whose leaf is served with no intermediate:

    python, system store only                         CERTIFICATE_VERIFY_FAILED
    python, system store + DigiCert G2 intermediate   HTTP 200, chain verified
    python, that intermediate ALONE, no root          REFUSED, "unable to get issuer
                                                      certificate"

The third line is the argument. A supplement is not a trust anchor: OpenSSL still requires
the path to terminate at a self-signed certificate the SYSTEM already trusts, so supplying an
intermediate adds no authority to the trusted set -- it restores a link the server should
have sent, and everything else about verification stays on. Hostname, dates and signature
path are all still checked.

(`curl` differs, and the difference is worth knowing before someone reproduces this by hand:
`curl --cacert <intermediate-only>` ACCEPTS the chain, because it permits a partial chain
terminating at a store certificate. The fetches here are Python's, where it is refused.)

THE GUARD THAT MAKES THE ABOVE TRUE
===================================

A self-signed certificate in a supplement WOULD be a new trust anchor -- that is exactly what
a root is -- so one is refused rather than loaded. Without that refusal this module would be
a general-purpose way to trust anything, wearing the name of a narrow fix. Everything else
here (the filename IS the host, one context per host, mounts that cannot match another host)
exists so the exception can never quietly widen.

A supplement is also a workaround with an expiry date in two senses: the certificate's own
`notAfter`, and the day the server starts serving its chain. Both are checkable, and
`supplement_still_needed()` is the second one, so an exception has a way to end rather than
outliving its cause in a repository nobody re-reads.
"""
from __future__ import annotations

import re
import ssl
from datetime import datetime, timezone
from pathlib import Path

# A supplement's FILENAME is the host it applies to. Not a field inside the file, not an
# entry in a config listing several: the name on disk is the scope, so a supplement that
# applies to two hosts is two files, and there is no way to write one that applies to all.
SUPPLEMENT_DIR_NAME = "tls-chain"

# Deliberately narrower than the RFC. A supplement filename is written by a curator naming a
# host they measured, so the shapes worth admitting are the ones a real host has; anything
# with a slash, a wildcard, a port or a scheme in it is a mistake worth refusing loudly.
_HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                      r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")

# How close to `notAfter` a supplement may come before loading it is an error rather than a
# fetch. 30 days is a re-ingest cycle plus room to notice: the point is that the run which
# would OTHERWISE start failing mysteriously fails legibly first, naming the file and the
# date. An expired supplement does not degrade gracefully -- it fails exactly like the
# missing intermediate it was added to fix, which is the confusion this avoids.
EXPIRY_WARNING_DAYS = 30


class SupplementRefused(Exception):
    """A chain supplement was present and is not loadable.

    Raised rather than warned. A supplement that cannot be loaded means the sources it
    covers cannot be fetched, and this package's whole position on a failed fetch is that it
    must not pass quietly as an absence of change.
    """


def _decode(path: Path) -> list[dict]:
    """The certificates in `path`, as ssl's decoded dicts.

    `SSLContext.get_ca_certs()` is stdlib and public, which is why the parse happens through
    a throwaway context rather than by adding a certificate library to a dependency list that
    every corpus image inherits.
    """
    probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        probe.load_verify_locations(cafile=str(path))
    except ssl.SSLError as e:
        raise SupplementRefused(f"{path.name}: not a readable PEM certificate ({e})") from e
    certs = probe.get_ca_certs()
    if not certs:
        raise SupplementRefused(f"{path.name}: contains no certificate")
    return certs


def _check(path: Path, now: datetime) -> None:
    """Refuse a supplement that would widen trust, that is spent, or that holds a secret."""
    # A PRIVATE KEY HAS NO BUSINESS HERE, and saying so is not paranoia about a mistake
    # nobody would make. A corpus adopting this feature has to punch a hole in a `*.pem`
    # gitignore rule that exists to keep tunnel credentials out of the repository
    # (executive-regulatory-frameworks/.gitignore). The hole is narrowed to one directory;
    # this is the other half, and it fails the run rather than warning, because a key that
    # reached this directory has already been committed by the time anyone reads a warning.
    if b"PRIVATE KEY" in path.read_bytes():
        raise SupplementRefused(
            f"{path.name}: contains a PRIVATE KEY block. A chain supplement is a public CA "
            f"certificate. Treat this key as compromised and rotate it.")
    for cert in _decode(path):
        # SELF-SIGNED IS THE ONE REFUSAL THIS MODULE EXISTS TO MAKE. A self-signed
        # certificate is a root, and a root in here is a new trust anchor -- the thing the
        # measured argument above says a supplement is not. Everything else in this file is
        # hygiene; this is the property.
        if cert.get("subject") == cert.get("issuer"):
            raise SupplementRefused(
                f"{path.name}: contains a self-signed certificate "
                f"({_cn(cert.get('subject'))}). A supplement completes a chain to a root the "
                f"system already trusts; a root here would ADD a trust anchor, which is what "
                f"this is not.")
        not_after = cert.get("notAfter")
        if not not_after:
            raise SupplementRefused(f"{path.name}: certificate carries no notAfter")
        expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc)
        days = (expires - now).days
        if days <= EXPIRY_WARNING_DAYS:
            raise SupplementRefused(
                f"{path.name}: certificate {_cn(cert.get('subject'))} expires {not_after} "
                f"({days} days). Re-fetch it from the host's AIA extension, or remove the "
                f"supplement if the server now serves its own chain.")


def _cn(name) -> str:
    """The commonName out of ssl's nested-tuple name, for a message a human can act on."""
    for rdn in (name or ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return "?"


def load(meta_dir: Path, now: datetime | None = None) -> dict[str, ssl.SSLContext]:
    """Host -> verifying SSL context, one per `<meta_dir>/tls-chain/<host>.pem`.

    Each context is the DEFAULT one -- system trust store, hostname checking, dates, the lot
    -- with the supplement's certificates added to what it may use to build a path. Nothing
    is turned off, and there is no code path here that can turn anything off.
    """
    now = now or datetime.now(timezone.utc)
    directory = Path(meta_dir) / SUPPLEMENT_DIR_NAME
    if not directory.is_dir():
        return {}
    contexts: dict[str, ssl.SSLContext] = {}
    for path in sorted(directory.glob("*.pem")):
        host = path.stem.lower()
        if not _HOST_RE.match(host):
            raise SupplementRefused(
                f"{path.name}: the filename is the host this supplement applies to, and "
                f"{host!r} is not a hostname. A supplement that applies to more than one "
                f"host is more than one file.")
        _check(path, now)
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=str(path))
        contexts[host] = ctx
    return contexts


def mounts(contexts: dict[str, ssl.SSLContext]) -> dict[str, object]:
    """httpx mounts that apply each context to ITS host and no other.

    Host scoping is structural rather than promised: a mount key names one host, so there is
    no arrangement of these files that produces a client verifying some OTHER host against a
    supplemented context. `https://` only -- a supplement is meaningless over plaintext, and
    a key that matched `all://` would silently cover an `http://` source too.
    """
    import httpx

    return {f"https://{host}": httpx.HTTPTransport(http2=True, verify=ctx)
            for host, ctx in contexts.items()}


def supplement_still_needed(host: str, url: str, timeout: float = 30.0) -> bool | None:
    """Does `url` still fail without its supplement?

    True  -- the server still omits its chain; the supplement is doing work.
    False -- the server now serves a complete chain. REMOVE the supplement and the file.
    None  -- could not tell: the request failed for some reason that was not verification,
             so this says nothing either way. An unreachable host is not a fixed one, and
             reporting it as one would retire a workaround on the strength of an outage.
    """
    import httpx

    try:
        with httpx.Client(http2=True, follow_redirects=True, timeout=timeout) as client:
            client.get(url)
        return False
    except Exception as e:  # noqa: BLE001 -- the classification IS the return value
        if _is_verification_failure(e):
            return True
        return None


def _is_verification_failure(exc: BaseException) -> bool:
    """True when `exc` is a certificate verification failure, at any depth.

    httpx wraps the ssl error in a ConnectError, so the cause chain is walked rather than the
    top exception typed. Matching on the message is deliberate too: `SSLCertVerificationError`
    alone would miss the same failure arriving from a transport that re-raises it as a
    ConnectError with the reason in the string, which is what httpx does today.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False
