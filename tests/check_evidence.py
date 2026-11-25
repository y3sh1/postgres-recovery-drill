"""Independent acceptance check for a report copied from the disposable lab."""
import json
from pathlib import Path
import sys


def check(report):
    assert report["schema_version"] == 1 and report["passed"] is True
    assert report["journal_count"] == 100
    rows = {row["name"]: row for row in report["scenarios"]}
    outcomes = {"fresh": "verified", "stale": "missing_orders",
                "altered": "changed_orders", "truncated": "restore_failed"}
    assert len(report["scenarios"]) == len(outcomes)
    assert set(rows) == set(outcomes)
    for name, verdict in outcomes.items():
        scenario = rows[name]
        assert scenario["passed"] is True and scenario["outcome"] == verdict, name
        assert len(scenario["archive_sha256"]) == 64
        if name != "truncated":
            assert scenario["restore_exit_code"] == 0
            assert scenario["probe_http_status"] == 200 and scenario["probe_order_id"] == "ORD-001"
            assert scenario["ready_seconds"] >= scenario["restore_seconds"] >= 0
            assert scenario["comparison"]["unexpected_ids"] == []
    assert not any(rows["fresh"]["comparison"][key] for key in ("missing_ids", "changed"))
    assert rows["stale"]["comparison"]["missing_ids"] == [f"ORD-{n:03}" for n in range(96, 101)]
    assert rows["stale"]["comparison"]["changed"] == []
    assert rows["altered"]["comparison"]["missing_ids"] == []
    assert rows["altered"]["comparison"]["changed"] == [{
        "order_id": "ORD-042", "expected": {"amount_cents": 2554, "currency": "USD"},
        "actual": {"amount_cents": 2654, "currency": "USD"},
    }]
    rejected = rows["truncated"]
    assert rejected["restore_exit_code"] != 0 and rejected["archive_bytes"] == 8
    assert rejected["orders_table_present"] is False
    assert rejected["comparison"] is None and rejected["ready_seconds"] is None


if __name__ == "__main__":
    check(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    print("The four restores match the independent fixture contract.")
