"""Reconcile a saved recovery report against its captured observations, offline."""

import argparse
from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

from .report import compare_orders


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON property: {key}")
        result[key] = value
    return result


def read_object(raw):
    obj = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    if not isinstance(obj, dict) or type(obj.get("schema_version")) is not int or obj["schema_version"] != 1:
        raise ValueError("Expected an evidence object with schema_version 1")
    return obj


def check_rows(rows, name, *, journal=False):
    if not isinstance(rows, list):
        raise ValueError(f"{name}: expected a list of orders")
    ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{name}: expected an order object")
        key = row["order_id"]
        if not isinstance(key, str) or not key or type(row["amount_cents"]) is not int:
            raise ValueError(f"{name}: invalid order ID or amount")
        if not isinstance(row["currency"], str) or not row["currency"]:
            raise ValueError(f"{name}: missing currency for {key}")
        if key in ids:
            raise ValueError(f"{name}: duplicate order ID {key!r}")
        ids.add(key)
        if journal and datetime.fromisoformat(row["accepted_at"]).utcoffset() != timedelta(0):
            raise ValueError(f"{name}: receipt {key} needs a UTC acceptance time")


def same_json(left, right):
    # JSON booleans and numbers differ even where Python would consider them equal.
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def data_outcome(comparison):
    missing, changed = comparison["missing_ids"], comparison["changed"]
    if comparison["unexpected_ids"] or (missing and changed):
        return "divergent"
    if missing:
        return "missing_orders"
    if changed:
        return "changed_orders"
    return "verified"


def verify_case(case, recovered, journal):
    name = case["name"]
    code, outcome = case["restore_exit_code"], case["outcome"]
    if type(code) is not int:
        raise ValueError(f"{name}: invalid restore exit code")
    if code != 0:
        empty_fields = ("comparison", "ready_seconds", "probe_order_id", "probe_http_status")
        if recovered is not None or outcome != "restore_failed" or any(case[key] is not None for key in empty_fields):
            raise ValueError(f"{name}: failed restore must have null captured orders, comparison and probe")
        return
    check_rows(recovered, name)
    computed = compare_orders(journal, recovered)
    if not same_json(computed, case["comparison"]):
        raise ValueError(f"{name}: recorded comparison differs from captured orders")
    ready = case["ready_seconds"]
    if ready is None:
        observed = "service_unavailable"
    else:
        restore = case["restore_seconds"]
        if (type(ready) not in (int, float) or type(restore) not in (int, float)
                or not math.isfinite(ready) or not math.isfinite(restore)
                or not 0 <= restore <= ready or case["probe_http_status"] != 200):
            raise ValueError(f"{name}: first-read timing or HTTP status is inconsistent")
        probe_id = case["probe_order_id"]
        receipt = next((row for row in journal if row["order_id"] == probe_id), None)
        stored = next((row for row in recovered if row["order_id"] == probe_id), None)
        if receipt is None or stored is None or any(
            receipt[field] != stored[field] for field in ("amount_cents", "currency")
        ):
            raise ValueError(f"{name}: the successful HTTP probe has no matching captured purchase")
        observed = data_outcome(computed)
    if observed != outcome:
        raise ValueError(f"{name}: observed outcome should be {observed!r}")


def verify(path):
    """Accept a report directory (or its report.json) and return the checked run ID."""
    root = Path(path)
    if root.name == "report.json" and root.is_file():
        root = root.parent
    report = read_object((root / "report.json").read_bytes())
    raw = (root / "observations.json").read_bytes()
    if sha256(raw).hexdigest() != report["observations_sha256"]:
        raise ValueError("SHA-256 of observations.json differs from the report")
    observations = read_object(raw)
    run_id = report["run_id"]
    if not isinstance(run_id, str) or not run_id or run_id != observations["run_id"]:
        raise ValueError("Run IDs differ or are empty")
    journal = observations["journal"]
    check_rows(journal, "journal", journal=True)
    if type(report["journal_count"]) is not int or len(journal) != report["journal_count"]:
        raise ValueError("Journal count differs from captured receipts")
    cases = report["scenarios"]
    if not isinstance(cases, list) or not all(isinstance(row, dict) for row in cases):
        raise ValueError("Expected a list of scenario objects")
    names = [row["name"] for row in cases]
    expected = {"fresh", "stale", "altered", "truncated"}
    captured = observations["recovered"]
    if (not isinstance(captured, dict) or len(names) != len(expected)
            or set(names) != expected or set(captured) != expected):
        raise ValueError("Expected a unique report and capture for each of the four scenarios")
    for case in cases:
        verify_case(case, captured[case["name"]], journal)
        if type(case["passed"]) is not bool or (case["passed"] and case["outcome"] != case["expected_outcome"]):
            raise ValueError(f"{case['name']}: scenario check contradicts the declared expectation")
        if not isinstance(case["expected_evidence"], dict):
            raise ValueError(f"{case['name']}: expected evidence must be a JSON object")
        if case["passed"]:
            actual = case if case["outcome"] == "restore_failed" else case["comparison"]
            for key, value in case["expected_evidence"].items():
                if key not in actual or not same_json(actual[key], value):
                    raise ValueError(f"{case['name']}: scenario passed despite unexpected {key}")
    if report["passed"] is not all(row["passed"] for row in cases):
        raise ValueError("Overall scenario check contradicts individual results")
    return run_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Report directory or path to report.json")
    args = parser.parse_args(argv)
    try:
        run_id = verify(args.path)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"Evidence verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Verified {run_id}: observation checksum, journal and all four reconciliations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
