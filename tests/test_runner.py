"""Check how the runner judges recovery evidence, independently of the CLI smoke drill."""

import hashlib
import json
import sqlite3

import pytest

from drill.postgres import run_databases
from drill.report import compare_orders
from drill.runner import classify, expected_evidence, journal_rows, matches_drill, order, write_observations


def test_stale_drill_requires_exact_missing_ids():
    expected = [order(n) for n in range(1, 101)]
    observation = compare_orders(expected, expected[:95])
    row = {"name": "stale", "outcome": classify(observation), "expected_outcome": "missing_orders",
           "expected_evidence": expected_evidence("stale"), "comparison": observation,
           "restore_exit_code": 0, "ready_seconds": 0.1, "probe_http_status": 200}
    assert matches_drill(row)
    row["comparison"] = compare_orders(expected, expected[5:])
    assert not matches_drill(row)


def test_ready_service_and_matching_counts_can_contain_bad_data():
    expected = [order(n) for n in range(1, 101)]
    recovered = [dict(receipt) for receipt in expected]
    recovered[41]["amount_cents"] += 100
    diff = compare_orders(expected, recovered)
    assert classify(diff) == "changed_orders"
    assert diff["changed"] == expected_evidence("altered")["changed"]
    recovered[41]["order_id"] = "ORD-999"
    assert classify(compare_orders(expected, recovered)) == "divergent"


def test_archive_failure_requires_specific_diagnostic_and_clean_target():
    row = {"name": "truncated", "outcome": "restore_failed", "expected_outcome": "restore_failed",
           "expected_evidence": expected_evidence("truncated"), "restore_exit_code": 1,
           "restore_error": "could not read from input file: end of file", "comparison": None,
           "orders_table_present": False, "archive_bytes": 8, "ready_seconds": None}
    assert matches_drill(row)
    row["restore_error"] = "could not connect to server"
    assert not matches_drill(row)
    row.update(restore_error="archive too short", orders_table_present=True)
    assert not matches_drill(row)


def test_missing_journal_cannot_be_silently_created(tmp_path):
    absent = tmp_path / "no-receipts.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        journal_rows(absent)
    assert not absent.exists()


def test_observations_hash_the_actual_rows_and_preserve_receipt_time(tmp_path):
    source = tmp_path / "journal.sqlite3"
    db = sqlite3.connect(source)
    with db:
        db.execute("CREATE TABLE receipts(order_id TEXT, amount_cents INTEGER, currency TEXT, accepted_at TEXT)")
        db.execute("INSERT INTO receipts VALUES ('order-A', 20, 'USD', '2026-01-01T00:00:00+00:00')")
    db.close()
    receipts = journal_rows(source)
    assert receipts[0]["accepted_at"] == "2026-01-01T00:00:00+00:00"
    restored = {"altered": [{"order_id": "order-A", "amount_cents": 31, "currency": "USD"}],
                "truncated": None}
    digest = write_observations(tmp_path, "test-run", receipts, restored)
    raw = (tmp_path / "observations.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest
    assert json.loads(raw) == {"schema_version": 1, "run_id": "test-run", "journal": receipts,
                               "recovered": restored}


def test_runner_requires_explicit_database_host(monkeypatch):
    monkeypatch.delenv("PGHOST", raising=False)
    with pytest.raises(ValueError, match="PGHOST"):
        with run_databases("test"):
            pytest.fail("This path must not connect to PostgreSQL")
