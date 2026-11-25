"""Run the four recovery drills against a private PostgreSQL lab cluster."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from shop import init_database, make_server
from .postgres import dump, pg_tool, restore, run_databases
from .report import compare_orders, write_report


def order(number):
    return {"order_id": f"ORD-{number:03d}", "amount_cents": 1000 + number * 37, "currency": "USD"}


@contextmanager
def shop_server(database, journal=None):
    conninfo = make_conninfo(dbname=database, connect_timeout=5)
    server = make_server(conninfo, journal_path=journal, read_only=journal is None)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


def http_json(url, payload=None):
    request = Request(url, headers={"Accept": "application/json"})
    if payload is not None:
        request.data = json.dumps(payload).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=5) as response:
        return response.status, json.load(response)


def journal_rows(journal):
    uri = journal.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT order_id, amount_cents, currency, accepted_at FROM receipts ORDER BY order_id")
        return [dict(row) for row in rows]
    finally:
        connection.close()


def prepare(create, work):
    source = create("source")
    init_database(make_conninfo(dbname=source, connect_timeout=5))
    journal = work / "receipts.sqlite3"
    archives = {name: work / f"{name}.dump" for name in ("fresh", "stale", "altered", "truncated")}
    with shop_server(source, journal) as url:
        for number in range(1, 101):
            payload = order(number)
            status, accepted = http_json(url + "/orders", payload)
            if status != 201 or accepted != payload:
                raise RuntimeError(f"Order {number} did not receive the expected new-order acknowledgement.")
            if number == 95:
                dump(source, archives["stale"])
    dump(source, archives["fresh"])
    receipts = journal_rows(journal)
    baseline = compare_orders([order(n) for n in range(1, 101)], receipts)
    if baseline["matched_count"] != 100 or baseline["recovered_count"] != 100:
        raise RuntimeError("The independent journal does not match the 100 service acknowledgements.")

    altered_source = create("altered_source", template=source)
    with psycopg.connect(dbname=altered_source, connect_timeout=5) as conn:
        result = conn.execute("UPDATE public.orders SET amount_cents = amount_cents + 100 WHERE order_id = %s",
                              ("ORD-042",))
        if result.rowcount != 1:
            raise RuntimeError("The altered archive setup could not locate its demonstration order.")
    dump(altered_source, archives["altered"])
    with archives["fresh"].open("rb") as stream:
        archives["truncated"].write_bytes(stream.read(8))
    return archives, receipts


def expected_evidence(name):
    evidence = {"missing_ids": [], "unexpected_ids": [], "changed": [], "recovered_count": 100}
    if name == "stale":
        evidence.update(missing_ids=[order(number)["order_id"] for number in range(96, 101)],
                        recovered_count=95)
    elif name == "altered":
        evidence["changed"] = [{"order_id": "ORD-042",
                                "expected": {"amount_cents": 2554, "currency": "USD"},
                                "actual": {"amount_cents": 2654, "currency": "USD"}}]
    elif name == "truncated":
        return {"archive_bytes": 8, "orders_table_present": False, "ready_seconds": None}
    elif name != "fresh":
        raise ValueError(f"Unknown scenario: {name}")
    return evidence


def classify(comparison):
    missing = bool(comparison["missing_ids"])
    changed = bool(comparison["changed"])
    if comparison["unexpected_ids"] or (missing and changed):
        return "divergent"
    if missing:
        return "missing_orders"
    if changed:
        return "changed_orders"
    return "verified"


EXPECTED = {"fresh": "verified", "stale": "missing_orders", "altered": "changed_orders",
            "truncated": "restore_failed"}


def matches_drill(row):
    if row["outcome"] != row["expected_outcome"]:
        return False
    if row["name"] == "truncated":
        # A credentials or connectivity failure is not evidence of a damaged archive.
        diagnostic = (row["restore_error"] or "").lower()
        archive_error = any(term in diagnostic for term in ("end of file", "too short", "eof"))
        return (archive_error and row["restore_exit_code"] != 0 and row["comparison"] is None
                and all(row[key] == value for key, value in row["expected_evidence"].items()))
    comparison = row["comparison"]
    return (row["restore_exit_code"] == 0 and row["ready_seconds"] is not None
            and row["probe_http_status"] == 200 and comparison["expected_count"] == 100
            and all(comparison[key] == value for key, value in row["expected_evidence"].items()))


def run_case(name, database, archive, receipts):
    raw = archive.read_bytes()
    row = {"name": name, "expected_outcome": EXPECTED[name], "expected_evidence": expected_evidence(name),
           "archive_sha256": hashlib.sha256(raw).hexdigest(), "archive_bytes": len(raw),
           "ready_seconds": None, "probe_order_id": None, "probe_http_status": None,
           "comparison": None, "restore_error": None}
    recovered = None
    started = time.monotonic()
    restored = restore(database, archive)
    row.update(restore_exit_code=restored.returncode, restore_seconds=round(time.monotonic() - started, 4))
    if restored.returncode:
        row.update(outcome="restore_failed", restore_error=restored.stderr.strip()[-1500:])
        with psycopg.connect(dbname=database, connect_timeout=5) as conn:
            row["orders_table_present"] = conn.execute(
                "SELECT to_regclass('public.orders') IS NOT NULL").fetchone()[0]
    else:
        probe = {key: receipts[0][key] for key in ("order_id", "amount_cents", "currency")}
        row["probe_order_id"] = probe["order_id"]
        with shop_server(database) as url:
            try:
                status, data = http_json(url + "/orders/" + probe["order_id"])
                row["probe_http_status"] = status
                if status == 200 and data == probe:
                    row["ready_seconds"] = round(time.monotonic() - started, 4)
            except HTTPError as error:
                row["probe_http_status"] = error.code
                row["probe_error"] = f"HTTP {error.code}"
            except (URLError, TimeoutError, ValueError) as error:
                row["probe_error"] = str(error)
        with psycopg.connect(dbname=database, connect_timeout=5, row_factory=dict_row) as conn:
            rows = conn.execute("SELECT order_id, amount_cents, currency FROM public.orders ORDER BY order_id")
            recovered = rows.fetchall()
            comparison = compare_orders(receipts, recovered)
        row.update(comparison=comparison, orders_table_present=True,
                   outcome=classify(comparison) if row["ready_seconds"] is not None else "service_unavailable")
    row["passed"] = matches_drill(row)
    return row, recovered


def write_observations(out, run_id, receipts, recovered):
    evidence = {"schema_version": 1, "run_id": run_id, "journal": receipts, "recovered": recovered}
    data = (json.dumps(evidence, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    out.mkdir(parents=True, exist_ok=True)
    temp = out / f".observations-{run_id}.tmp"
    try:
        temp.write_bytes(data)
        temp.replace(out / "observations.json")
    finally:
        temp.unlink(missing_ok=True)
    return hashlib.sha256(data).hexdigest()


def run(out):
    run_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="recovery-drill-") as tmp, run_databases(run_id) as (create, version):
        archives, receipts = prepare(create, Path(tmp))
        scenarios = []
        recovered = {}
        for name, archive in archives.items():
            result, recovered[name] = run_case(name, create(name), archive, receipts)
            scenarios.append(result)
            print(f"{name:10} {result['outcome']:20} {'PASS' if result['passed'] else 'FAIL'}", flush=True)
        report = {"schema_version": 1, "run_id": run_id, "started_at": started_at,
                  "journal_count": len(receipts), "currency": "USD", "postgres_version": version,
                  "pg_dump_version": pg_tool("pg_dump", "--version").stdout.strip(),
                  "pg_restore_version": pg_tool("pg_restore", "--version").stdout.strip(),
                  "scenarios": scenarios, "passed": all(case["passed"] for case in scenarios)}
    report["observations_sha256"] = write_observations(out, run_id, receipts, recovered)
    write_report(out, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("reports"), help="Directory for the report and raw observations")
    args = parser.parse_args(argv)
    try:
        report = run(args.out)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, psycopg.Error, subprocess.SubprocessError) as error:
        print(f"Drill incomplete: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        return 2
    print(f"Reports: {args.out.resolve()}")
    return 0 if report["passed"] else 1
