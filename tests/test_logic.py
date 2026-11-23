"""Dependency-free tests for the reference plugin's pure logic.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "plugin" / "__init__.py"

spec = importlib.util.spec_from_file_location("risk_gate_plugin", PLUGIN_PATH)
assert spec and spec.loader
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class ClassifierTests(unittest.TestCase):
    def test_high_consequence_terms(self):
        self.assertEqual(plugin.classify("Draft the client invoice for last month"), "high")
        self.assertEqual(plugin.classify("Explain this force-push recovery"), "high")
        self.assertEqual(plugin.classify("Review the security policy"), "high")

    def test_medium_factual_and_technical_terms(self):
        self.assertEqual(plugin.classify("What is the latest stable version of Python?"), "medium")
        self.assertEqual(plugin.classify("Compare these two API designs"), "medium")
        self.assertEqual(plugin.classify("Fix the failing build"), "medium")

    def test_low_casual_and_creative_terms(self):
        self.assertEqual(plugin.classify("Give me three coffee shop names"), "low")
        self.assertEqual(plugin.classify("Write a short poem about rain"), "low")

    def test_high_wins_over_medium(self):
        self.assertEqual(
            plugin.classify("What is the latest client pricing?"),
            "high",
        )


class PolicyTests(unittest.TestCase):
    def test_low_has_no_injected_policy(self):
        self.assertIsNone(plugin._policy_for("low"))

    def test_medium_policy_mentions_verification(self):
        policy = plugin._policy_for("medium")
        self.assertIn("MEDIUM RISK", policy)
        self.assertIn("verify", policy.lower())

    def test_high_policy_requires_approval_for_irreversible_action(self):
        policy = plugin._policy_for("high")
        self.assertIn("HIGH RISK", policy)
        self.assertIn("approval", policy.lower())


class JsonTests(unittest.TestCase):
    def test_plain_json(self):
        parsed = plugin._extract_json('{"risk":"medium","verdict":"pass","issues":[]}')
        self.assertEqual(parsed["verdict"], "pass")

    def test_fenced_json(self):
        parsed = plugin._extract_json(
            'Here is the review:\n```json\n{"risk":"high","verdict":"block","issues":[]}\n```'
        )
        self.assertEqual(parsed["risk"], "high")

    def test_json_with_surrounding_prose(self):
        parsed = plugin._extract_json(
            'Review follows. {"risk":"low","verdict":"pass","issues":[]} Done.'
        )
        self.assertEqual(parsed["verdict"], "pass")

    def test_non_json_returns_none(self):
        self.assertIsNone(plugin._extract_json("I could not review this."))


class QuoteTests(unittest.TestCase):
    def test_exact_quote_survives(self):
        answer = "The latest release is v0.34.1 and it ships on Friday."
        issues = [
            'The answer says "v0.34.1" but the source says v0.35.0.',
        ]
        self.assertEqual(plugin._filter_quoted_issues(answer, issues), issues)

    def test_fabricated_quote_is_dropped(self):
        answer = "The latest release is v0.34.1 and it ships on Friday."
        issues = [
            'The answer says "v0.33.3" but that version is stale.',
        ]
        self.assertEqual(plugin._filter_quoted_issues(answer, issues), [])

    def test_mixed_issues_keep_only_the_supported_one(self):
        answer = "The latest release is v0.34.1 and it ships on Friday."
        good = 'The answer says "v0.34.1" but the source says v0.35.0.'
        bad = 'The answer says "ships on Monday" which is false.'
        self.assertEqual(
            plugin._filter_quoted_issues(answer, [good, bad]),
            [good],
        )

    def test_whitespace_and_case_are_normalized(self):
        answer = "The API returns   a JSON object."
        issues = ['The answer says "api returns a json object" incorrectly.']
        self.assertEqual(plugin._filter_quoted_issues(answer, issues), issues)

    def test_non_string_issues_are_dropped(self):
        answer = "The answer says hello."
        self.assertEqual(plugin._filter_quoted_issues(answer, [None, 42, {}]), [])


class SanitizerTests(unittest.TestCase):
    def test_no_surviving_issues_forces_pass(self):
        answer = "The latest release is v0.34.1."
        review = {
            "risk": "medium",
            "verdict": "revise",
            "issues": ['The answer says "v0.33.3" which is stale.'],
            "corrected_answer": "The latest release is v0.33.3.",
            "evidence_needed": ["version source"],
        }
        cleaned = plugin._sanitize_review(answer, review)
        self.assertEqual(cleaned["verdict"], "pass")
        self.assertEqual(cleaned["issues"], [])
        self.assertIsNone(cleaned["corrected_answer"])
        self.assertEqual(cleaned["evidence_needed"], [])

    def test_surviving_issue_keeps_revise(self):
        answer = "The latest release is v0.34.1."
        review = {
            "risk": "medium",
            "verdict": "revise",
            "issues": ['The answer says "v0.34.1" but the source says v0.35.0.'],
            "corrected_answer": "The latest release is v0.35.0.",
            "evidence_needed": [],
        }
        cleaned = plugin._sanitize_review(answer, review)
        self.assertEqual(cleaned["verdict"], "revise")
        self.assertEqual(len(cleaned["issues"]), 1)
        self.assertEqual(cleaned["corrected_answer"], "The latest release is v0.35.0.")


class FormattingTests(unittest.TestCase):
    def test_note_lists_issues_and_evidence(self):
        note = plugin._format_issues(
            ["A quoted defect."],
            ["release page"],
        )
        self.assertIn("Verification note", note)
        self.assertIn("A quoted defect.", note)
        self.assertIn("release page", note)

    def test_note_has_no_bullets_when_empty(self):
        note = plugin._format_issues([], [])
        self.assertIn("Verification note", note)
        self.assertNotIn("- ", note)


if __name__ == "__main__":
    unittest.main()
