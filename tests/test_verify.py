from contextlib import redirect_stderr
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from drill.report import compare_orders
from drill.verify import main, verify


class VerifierTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        a = {"order_id": "A", "amount_cents": 300, "currency": "USD"}
        b = {"order_id": "B", "amount_cents": 100, "currency": "USD"}
        receipts = [{**row, "accepted_at": "2026-06-01T12:00:00Z"} for row in (a, b)]
        captures = {"fresh": [a, b], "stale": [a], "altered": [a, {**b, "amount_cents": 200}],
                    "truncated": None}
        outcomes = ("verified", "missing_orders", "changed_orders", "restore_failed")
        cases = []
        for (name, rows), outcome in zip(captures.items(), outcomes):
            ok = rows is not None
            cases.append({"name": name, "passed": True, "expected_outcome": outcome,
                          "expected_evidence": {}, "outcome": outcome,
                          "restore_exit_code": 0 if ok else 1, "restore_seconds": .2,
                          "comparison": compare_orders(receipts, rows) if ok else None,
                          "ready_seconds": .5 if ok else None, "probe_order_id": "A" if ok else None,
                          "probe_http_status": 200 if ok else None})
        self.obs = {"schema_version": 1, "run_id": "run-1", "journal": receipts, "recovered": captures}
        self.report = {"schema_version": 1, "run_id": "run-1", "journal_count": 2,
                       "scenarios": cases, "passed": True}
        self.save()

    def save(self):
        raw = (json.dumps(self.obs) + "\n").encode()
        (self.root / "observations.json").write_bytes(raw)
        self.report["observations_sha256"] = sha256(raw).hexdigest()
        (self.root / "report.json").write_text(json.dumps(self.report), encoding="utf-8")

    def test_complete_evidence_verifies_from_directory_or_file(self):
        self.assertEqual(verify(self.root), "run-1")
        self.assertEqual(verify(self.root / "report.json"), "run-1")

    def test_byte_change_is_detected_and_cli_exits_nonzero(self):
        with (self.root / "observations.json").open("ab") as f:
            f.write(b"\n")
        err = StringIO()
        with redirect_stderr(err):
            self.assertEqual(main([str(self.root)]), 1)
        self.assertIn("SHA-256", err.getvalue())

    def test_run_ids_must_match(self):
        self.obs["run_id"] = "other-run"
        self.save()
        with self.assertRaisesRegex(ValueError, "Run IDs"):
            verify(self.root)

    def test_changed_capture_fails_even_with_matching_hash_and_same_count(self):
        self.obs["recovered"]["altered"][1]["amount_cents"] = 100
        self.save()
        with self.assertRaisesRegex(ValueError, "altered: recorded comparison"):
            verify(self.root)

    def test_duplicate_receipt_and_duplicate_scenario_are_rejected(self):
        self.obs["journal"].append(self.obs["journal"][0])
        self.save()
        with self.assertRaisesRegex(ValueError, "duplicate order ID"):
            verify(self.root)
        self.obs["journal"].pop()
        self.report["scenarios"][0]["name"] = "stale"
        self.save()
        with self.assertRaisesRegex(ValueError, "unique report and capture"):
            verify(self.root)

    def test_only_failed_restore_allows_null_rows_and_comparison(self):
        self.obs["recovered"]["truncated"] = []
        self.save()
        with self.assertRaisesRegex(ValueError, "failed restore"):
            verify(self.root)
        self.obs["recovered"]["truncated"] = None
        self.obs["recovered"]["fresh"] = None
        self.save()
        with self.assertRaisesRegex(ValueError, "fresh: expected a list"):
            verify(self.root)

    def test_no_count_allowed_for_failed_restore(self):
        self.report["scenarios"][3]["comparison"] = compare_orders(self.obs["journal"], [])
        self.save()
        with self.assertRaisesRegex(ValueError, "failed restore"):
            verify(self.root)

    def test_first_read_requires_an_exact_purchase_in_both_stores(self):
        cases = {case["name"]: case for case in self.report["scenarios"]}
        for scenario, order_id in (("fresh", "never-accepted"), ("stale", "B"), ("altered", "B")):
            with self.subTest(scenario=scenario):
                case = cases[scenario]
                original = case["probe_order_id"]
                case["probe_order_id"] = order_id
                self.save()
                with self.assertRaisesRegex(ValueError, "successful HTTP probe"):
                    verify(self.root)
                case["probe_order_id"] = original

    def test_wrong_observed_outcome_and_false_success_detected(self):
        self.report["scenarios"][2]["outcome"] = "verified"
        self.save()
        with self.assertRaisesRegex(ValueError, "observed outcome"):
            verify(self.root)
        self.report["scenarios"][2]["outcome"] = "changed_orders"
        self.report["scenarios"][2]["expected_evidence"] = {"changed": []}
        self.save()
        with self.assertRaisesRegex(ValueError, "despite unexpected changed"):
            verify(self.root)

    def test_duplicate_json_key_is_rejected(self):
        path = self.root / "report.json"
        path.write_text('{"run_id":"first",' + path.read_text(encoding="utf-8")[1:], encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate JSON property"):
            verify(self.root)


if __name__ == "__main__":
    unittest.main()
