"""Offline checks for the independent receipt reconciliation and its output."""

from html.parser import HTMLParser
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from drill.report import compare_orders, write_report


def order(key, cents, currency="USD", **extra):
    return {"order_id": key, "amount_cents": cents, "currency": currency, **extra}


class ComparisonTests(unittest.TestCase):
    def test_id_and_money_equality_ignores_row_order_and_journal_timestamp(self):
        receipts = [order("A", 231, accepted_at="2025-10-01T00:00:00Z"), order("B", 100)]
        comparison = compare_orders(receipts, [order("B", 100), order("A", 231)])
        self.assertEqual(comparison["matched_count"], 2)
        self.assertEqual(comparison["completeness_pct"], 100.0)
        for key in ("changed", "missing_ids", "unexpected_ids"):
            self.assertEqual(comparison[key], [])

    def test_equal_counts_can_hide_replaced_order_and_edited_values(self):
        receipts = [order("Z", 900), order("B", 175), order("C", 250), order("D", 400)]
        restored = [order("B", 176), order("D", 400), order("X", 900), order("C", 250, "CAD")]
        r = compare_orders(receipts, restored)
        self.assertEqual(r["expected_count"], r["recovered_count"])
        self.assertEqual(r["missing_ids"], ["Z"])
        self.assertEqual(r["unexpected_ids"], ["X"])
        self.assertEqual(r["matched_count"], 1)
        self.assertEqual(r["completeness_pct"], 25.0)
        self.assertEqual([row["order_id"] for row in r["changed"]], ["B", "C"])
        self.assertEqual(r["changed"][0]["actual"]["amount_cents"], 176)
        self.assertEqual(r["changed"][1]["expected"]["currency"], "USD")
        self.assertEqual(r["changed"][1]["actual"]["currency"], "CAD")

    def test_no_reference_records_has_undefined_percentage(self):
        r = compare_orders([], [order("X", 150)])
        self.assertIsNone(r["completeness_pct"])
        self.assertEqual(r["unexpected_ids"], ["X"])

    def test_duplicate_identity_is_an_invalid_comparison_input(self):
        duplicate = [order("A", 150), order("A", 200)]
        for expected, actual in ((duplicate, []), ([], duplicate)):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, "Duplicate"):
                compare_orders(expected, actual)


class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


class OutputTests(unittest.TestCase):
    def test_html_is_portable_and_escapes_all_external_evidence(self):
        untrusted = "<script>alert('test')</script>"
        comparison = compare_orders([order(untrusted, 100)], [order(untrusted, 200, untrusted)])
        altered = {"name": untrusted, "expected_outcome": "changed_orders", "outcome": "changed_orders",
                   "passed": True, "archive_sha256": untrusted, "archive_bytes": 380,
                   "restore_exit_code": 0, "restore_seconds": 0.1054, "ready_seconds": 0.6139,
                   "probe_http_status": 200, "probe_order_id": untrusted, "comparison": comparison,
                   "expected_evidence": {"id": untrusted}}
        truncated = {"name": "truncated", "expected_outcome": "restore_failed", "outcome": "restore_failed",
                     "passed": True, "comparison": None, "restore_error": untrusted,
                     "ready_seconds": None, "restore_seconds": 0.102}
        report = {"run_id": untrusted, "started_at": "2025-01-15T16:00:00Z", "journal_count": 1,
                  "scenarios": [altered, truncated], "passed": True}
        with TemporaryDirectory() as parent:
            out = Path(parent) / "run"
            write_report(out, report)
            html = (out / "report.html").read_text(encoding="utf-8")
            document = Document()
            document.feed(html)
            self.assertNotIn("script", document.tags)
            self.assertNotIn(untrusted, html)
            self.assertIn("&lt;script&gt;", html)
            self.assertIn("0.614 s", html)
            self.assertIn("Order completeness is unknown", html)
            self.assertIn("2 / 2", html)
            self.assertIn('href="report.json"', html)
            self.assertEqual(json.loads((out / "report.json").read_text(encoding="utf-8")), report)
            report["run_id"] = "repeat"
            write_report(out, report)
            self.assertIn("repeat", (out / "report.html").read_text(encoding="utf-8"))
            self.assertEqual({path.name for path in out.iterdir()}, {"report.json", "report.html"})


if __name__ == "__main__":
    unittest.main()
