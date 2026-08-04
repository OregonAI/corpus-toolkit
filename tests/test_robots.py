"""robots.txt awareness (corpus-toolkit#29).

The toolkit fetched from third-party hosts on a cron without ever reading robots.txt. The
oregon-counties survey measured, over 165 hosts: 42 with no reachable robots.txt, 8 blocking
a named AI crawler, 5 naming ClaudeBot with `Disallow: /` — every commercial code vendor
among them, several carrying `Content-Signal: search=yes, ai-train=no`.

The shape that makes this hard is **generic-crawling-permitted, AI-crawling-refused**. A
blanket `User-agent: * / Disallow: /` is obvious. A rule naming one agent and allowing
everyone else is invisible unless something looks for it — and `allowed()` correctly returns
True for us in that case, which is exactly why the position has to be reported separately.

Parsing is tested against real fixture text rather than over the network: the point is what
we conclude from a given file, and a network test would assert on someone else's server.
"""
import unittest
from unittest import mock

from corpus_toolkit.sources import robots

# The vendor shape: everyone may crawl, named AI agents may not.
VENDOR = """\
User-agent: *
Allow: /
Crawl-delay: 5

User-agent: ClaudeBot
Disallow: /

User-agent: GPTBot
User-agent: CCBot
Disallow: /

Content-Signal: search=yes, ai-train=no, use=reference
"""

PERMISSIVE = """\
User-agent: *
Disallow:
"""

BLANKET_BLOCK = """\
User-agent: *
Disallow: /
"""

WITH_COMMENTS = """\
# our policy
User-agent: ClaudeBot   # the anthropic one
Disallow: /             # everything
"""


class ParseAiBlocksTest(unittest.TestCase):
    def test_named_ai_agents_are_collected_including_grouped_ones(self):
        blocked = robots._parse_ai_blocks(VENDOR)
        self.assertIn("ClaudeBot", blocked)
        self.assertIn("GPTBot", blocked)
        self.assertIn("CCBot", blocked,
                      "consecutive User-agent lines share one group's rules")

    def test_empty_disallow_is_permission_not_a_block(self):
        """`Disallow:` with no value means allow everything — counting it would report a
        permissive host as refusing."""
        self.assertEqual(robots._parse_ai_blocks(PERMISSIVE), [])

    def test_wildcard_block_names_no_ai_agent(self):
        """A blanket block is a real restriction, but it is not a STATEMENT about AI
        crawling — `allowed()` is what catches it, and it must not be double-counted here."""
        self.assertEqual(robots._parse_ai_blocks(BLANKET_BLOCK), [])

    def test_comments_are_stripped(self):
        self.assertEqual(robots._parse_ai_blocks(WITH_COMMENTS), ["ClaudeBot"])

    def test_a_new_user_agent_after_rules_starts_a_new_group(self):
        """Otherwise the first group's agents inherit every later group's Disallow, and a
        permissive host reads as blocking everyone."""
        text = ("User-agent: Googlebot\nDisallow: /private\n"
                "User-agent: ClaudeBot\nDisallow: /\n")
        self.assertEqual(robots._parse_ai_blocks(text), ["ClaudeBot"])

        text2 = ("User-agent: ClaudeBot\nAllow: /\n"
                 "User-agent: SomeOtherBot\nDisallow: /\n")
        self.assertEqual(robots._parse_ai_blocks(text2), [],
                         "ClaudeBot's group ended before the Disallow")


class ContentSignalTest(unittest.TestCase):
    def test_surfaced_verbatim(self):
        self.assertEqual(robots._content_signal(VENDOR),
                         "search=yes, ai-train=no, use=reference")

    def test_absent_is_none(self):
        self.assertIsNone(robots._content_signal(PERMISSIVE))


class AllowedTest(unittest.TestCase):
    def setUp(self):
        robots.clear_cache()

    def _with_body(self, body, status=200):
        resp = mock.MagicMock()
        resp.status = status
        resp.read.return_value = body.encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        return mock.patch.object(robots.urllib.request, "urlopen", return_value=resp)

    def test_our_agent_is_permitted_while_ai_agents_are_blocked(self):
        """THE CENTRAL CASE. Our UA is not named, so we are allowed — and the host has
        still plainly stated a position. Both must be reported, and neither may be
        collapsed into the other."""
        with self._with_body(VENDOR):
            self.assertIs(robots.allowed("https://vendor.example/x", "corpus-toolkit"), True)
            rec = robots.ai_position("https://vendor.example/x", "corpus-toolkit")
        self.assertTrue(rec.states_ai_position)
        self.assertIn("ClaudeBot", rec.blocked_ai_agents)

    def test_blanket_block_denies_us(self):
        with self._with_body(BLANKET_BLOCK):
            self.assertIs(robots.allowed("https://closed.example/x", "corpus-toolkit"), False)

    def test_unreachable_robots_is_unknown_not_permission(self):
        """None, never True. Folding a missing answer into permission is how a silent
        assumption gets made on the operator's behalf."""
        with mock.patch.object(robots.urllib.request, "urlopen",
                               side_effect=OSError("connection refused")):
            self.assertIsNone(robots.allowed("https://down.example/x", "corpus-toolkit"))

    def test_404_is_a_real_answer_meaning_no_restrictions(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with mock.patch.object(robots.urllib.request, "urlopen", side_effect=err):
            self.assertIs(robots.allowed("https://bare.example/x", "corpus-toolkit"), True)

    def test_403_is_not_permission(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with mock.patch.object(robots.urllib.request, "urlopen", side_effect=err):
            self.assertIsNone(robots.allowed("https://walled.example/x", "corpus-toolkit"))

    def test_result_is_cached_per_host(self):
        with self._with_body(VENDOR) as m:
            robots.allowed("https://vendor.example/a", "corpus-toolkit")
            robots.allowed("https://vendor.example/b", "corpus-toolkit")
        self.assertEqual(m.call_count, 1, "robots.txt must be fetched once per host")

    def test_crawl_delay_is_surfaced(self):
        with self._with_body(VENDOR):
            rec = robots.ai_position("https://vendor.example/x", "corpus-toolkit")
        self.assertEqual(rec.crawl_delay, 5.0)


if __name__ == "__main__":
    unittest.main()
