"""A chain supplement completes a chain; it never widens trust.

The fixtures are a real self-signed root and a real certificate that root signed, generated
once and committed (`tests/fixtures/tls/`), so these assertions run against certificates ssl
actually parses rather than against dicts shaped like certificates. They expire in 2046; the
expiry test passes its own `now` rather than waiting, so nothing here rots on a date.
"""
import ssl
from datetime import datetime, timedelta, timezone

import pytest

from corpus_toolkit.sources import changes, tls

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures" / "tls"
BEFORE_EXPIRY = datetime(2030, 1, 1, tzinfo=timezone.utc)


def _supplement(tmp_path, filename, fixture="example-intermediate.pem"):
    directory = tmp_path / "tls-chain"
    directory.mkdir(exist_ok=True)
    (directory / filename).write_bytes((FIXTURES / fixture).read_bytes())
    return tmp_path


def test_a_signed_intermediate_loads_and_is_keyed_by_its_filename(tmp_path):
    meta = _supplement(tmp_path, "sharedsystems.dhsoha.state.or.us.pem")
    loaded = tls.load(meta, now=BEFORE_EXPIRY)
    assert list(loaded) == ["sharedsystems.dhsoha.state.or.us"]
    assert isinstance(loaded["sharedsystems.dhsoha.state.or.us"], ssl.SSLContext)


def test_a_self_signed_certificate_is_refused(tmp_path):
    """THE property. A root here would be a new trust anchor, which is what this is not."""
    meta = _supplement(tmp_path, "example.gov.pem", fixture="example-root.pem")
    with pytest.raises(tls.SupplementRefused) as e:
        tls.load(meta, now=BEFORE_EXPIRY)
    assert "self-signed" in str(e.value)


def test_verification_still_fails_for_a_host_the_supplement_does_not_cover(tmp_path):
    """The context is not a bypass: it verifies everything a default context verifies.

    Loaded against a certificate signed by a root NOTHING trusts, so a chain built through
    it cannot terminate anywhere the system store admits — the same refusal the real fix
    depends on, run without a network.
    """
    meta = _supplement(tmp_path, "example.gov.pem")
    ctx = tls.load(meta, now=BEFORE_EXPIRY)["example.gov"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_a_spent_certificate_is_refused_before_it_can_fail_a_fetch(tmp_path):
    meta = _supplement(tmp_path, "example.gov.pem")
    almost = datetime(2046, 8, 23, tzinfo=timezone.utc) - timedelta(days=5)
    with pytest.raises(tls.SupplementRefused) as e:
        tls.load(meta, now=almost)
    assert "expires" in str(e.value)


def test_the_filename_must_be_one_host(tmp_path):
    meta = _supplement(tmp_path, "*.dhsoha.state.or.us.pem")
    with pytest.raises(tls.SupplementRefused) as e:
        tls.load(meta, now=BEFORE_EXPIRY)
    assert "not a hostname" in str(e.value)


def test_no_directory_means_no_supplements(tmp_path):
    assert tls.load(tmp_path) == {}


def test_mounts_name_one_host_each_and_never_a_wildcard():
    """Host scoping is structural. There is no arrangement of files that widens it."""
    contexts = {"a.example.gov": ssl.create_default_context(),
                "b.example.gov": ssl.create_default_context()}
    keys = set(tls.mounts(contexts))
    assert keys == {"https://a.example.gov", "https://b.example.gov"}
    assert not any(k.startswith("all://") or k == "https://" for k in keys)


def test_a_corpus_with_no_supplement_mounts_nothing(tmp_path, monkeypatch):
    """The default path is untouched: no supplements, no mounts, one ordinary client."""
    monkeypatch.setattr(changes, "_client", None)
    assert changes.configure_chain_supplements(tmp_path) == {}
    assert tls.mounts({}) == {}


class _Verify(Exception):
    pass


def test_a_wrapped_verification_failure_is_recognised():
    """httpx wraps the ssl error, so the cause chain is walked rather than the type matched."""
    inner = ssl.SSLCertVerificationError("certificate verify failed")
    outer = _Verify("connection failed")
    outer.__cause__ = inner
    assert tls._is_verification_failure(outer) is True
    assert tls._is_verification_failure(_Verify("connection reset by peer")) is False


def test_still_needed_says_nothing_when_the_failure_was_not_verification(monkeypatch):
    """An unreachable host is not a fixed one — that distinction retires a workaround."""
    import httpx

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "Client", _Client)
    assert tls.supplement_still_needed("h.example.gov", "https://h.example.gov/x") is None


def test_a_private_key_in_a_supplement_is_refused(tmp_path):
    """Adopting this feature means punching a hole in a `*.pem` gitignore rule. This is the
    other half of that hole: a key that reached the directory fails the run outright."""
    meta = _supplement(tmp_path, "example.gov.pem")
    path = meta / "tls-chain" / "example.gov.pem"
    path.write_text("-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n"
                    + path.read_text())
    with pytest.raises(tls.SupplementRefused) as e:
        tls.load(meta, now=BEFORE_EXPIRY)
    assert "PRIVATE KEY" in str(e.value)
