"""Reconcile a recovered order store against receipts and render its drill report."""

from __future__ import annotations

from html import escape
import json
from pathlib import Path
from uuid import uuid4


def _index_orders(rows):
    indexed = {}
    for row in rows:
        key = row["order_id"]
        if key in indexed:
            raise ValueError(f"Duplicate order ID in reconciliation input: {key!r}")
        indexed[key] = {"amount_cents": row["amount_cents"], "currency": row["currency"]}
    return indexed


def compare_orders(expected_rows, actual_rows):
    """Return exact ID and money differences; receipt timestamps do not affect equality."""
    expected = _index_orders(expected_rows)
    actual = _index_orders(actual_rows)
    missing = sorted(expected.keys() - actual.keys())
    unexpected = sorted(actual.keys() - expected.keys())
    changed = [
        {"order_id": key, "expected": expected[key], "actual": actual[key]}
        for key in sorted(expected.keys() & actual.keys())
        if expected[key] != actual[key]
    ]
    matched = len(expected) - len(missing) - len(changed)
    return {
        "expected_count": len(expected),
        "recovered_count": len(actual),
        "matched_count": matched,
        "missing_ids": missing,
        "unexpected_ids": unexpected,
        "changed": changed,
        "completeness_pct": round(100 * matched / len(expected), 2) if expected else None,
    }


STYLE = """
:root { color-scheme: light; --ink: #172b36; --quiet: #405964; --line: #b8cbd0;
        --paper: #fff; --accent: #005b61; }
* { box-sizing: border-box }
body { margin: 0; background: #f2f6f5; color: var(--ink);
       font: 1rem/1.55 Inter,"Segoe UI",system-ui,sans-serif }
main { max-width: 76rem; margin: 2rem auto; padding: clamp(1rem,4vw,3rem);
       background: var(--paper); border-top: .35rem solid var(--accent) }
h1,h2,h3,h4,p,dl { margin: 0 }
h1 { font-size: clamp(1.9rem,4vw,3rem); line-height: 1.12; letter-spacing: -.035em }
h2 { margin-top: 2.3rem; font-size: 1.4rem }
h3 { margin-top: 1.5rem; font-size: 1.13rem }
h4 { margin-top: 1rem; font-size: 1rem }
a,summary { color: var(--accent) }
a:focus-visible,summary:focus-visible { outline: 3px solid #b34c00; outline-offset: 3px }
.subtle,.note,dt { color: var(--quiet) }
.note { margin: .6rem 0; max-width: 83ch }
.metrics { display: grid; grid-template-columns: repeat(auto-fit,minmax(12rem,1fr));
           gap: 1.1rem; margin-top: 1.5rem; padding: 1rem 0; border-block: 1px solid var(--line) }
dd { margin: .1rem 0 0; overflow-wrap: anywhere }
.metrics dd { font-size: 1.4rem; font-weight: 650 }
.scroll { overflow-x: auto }
table { width: 100%; border-collapse: collapse; margin: .65rem 0 }
th,td { padding: .55rem .65rem; border-bottom: 1px solid var(--line);
        text-align: left; vertical-align: top }
thead th { color: var(--quiet); background: #eff5f4; font-size: .9rem }
th:first-child,td:first-child { padding-left: .45rem }
.good { color: #006043 }
.bad { color: #8a2e19; font-weight: 650 }
details { border-bottom: 1px solid var(--line); padding: .8rem 0 }
summary { cursor: pointer; font-weight: 650; width: fit-content }
.facts { display: grid; grid-template-columns: repeat(auto-fit,minmax(12rem,1fr));
         gap: .55rem 1rem; margin: .9rem 0 }
.mono,code,pre { font-family: ui-monospace,Consolas,monospace; overflow-wrap: anywhere }
pre { white-space: pre-wrap; padding: .7rem; background: #eff5f4 }
ul { margin: .3rem 0; padding-left: 1.4rem }
footer { margin-top: 1.5rem; color: var(--quiet) }
@media (max-width: 44rem) { main { margin: 0 } .metrics { gap: .6rem } }
"""

NAMES = {"fresh": "Fresh archive", "stale": "Stale archive", "altered": "Altered order",
         "truncated": "Truncated archive"}
OUTCOMES = {"verified": "Verified", "missing_orders": "Missing orders",
            "changed_orders": "Changed orders", "restore_failed": "Restore failed",
            "divergent": "Data diverged", "service_unavailable": "Service unavailable"}


def _text(value):
    return escape(str(value), quote=True)


def _label(value, labels):
    return _text(labels.get(value, value))


def _seconds(value):
    return "—" if value is None else f"{value:.3f} s"


def _fact(label, value):
    return f"<div><dt>{label}</dt><dd>{_text(value)}</dd></div>"


def _comparison_html(comparison):
    if comparison is None:
        return "<p class='note'>Reconciliation did not run. Order completeness is unknown.</p>"
    expected = comparison["expected_count"]
    recovered = comparison["recovered_count"]
    matched = comparison["matched_count"]
    pct = comparison["completeness_pct"]
    pct_label = "" if pct is None else f" ({pct:g}%)"
    parts = [f"<p>{_text(matched)} of {_text(expected)} journal receipts matched exactly{pct_label}. "
             f"The recovered database contained {_text(recovered)} orders.</p>"]
    for key, title in (("missing_ids", "Missing order IDs"), ("unexpected_ids", "Unexpected order IDs")):
        if comparison[key]:
            ids = ", ".join(_text(order_id) for order_id in comparison[key])
            parts.append(f"<h4>{title}</h4><p class='mono'>{ids}</p>")
    if comparison["changed"]:
        parts.append("<h4>Changed orders</h4><div class='scroll'><table>"
                     "<thead><tr><th scope='col'>Order</th><th scope='col'>Journal amount (cents)</th>"
                     "<th scope='col'>Journal currency</th><th scope='col'>Recovered amount (cents)</th>"
                     "<th scope='col'>Recovered currency</th></tr></thead><tbody>")
        for row in comparison["changed"]:
            want, got = row["expected"], row["actual"]
            values = (want["amount_cents"], want["currency"], got["amount_cents"], got["currency"])
            cells = "".join(f"<td>{_text(value)}</td>" for value in values)
            parts.append(f"<tr><th scope='row'>{_text(row['order_id'])}</th>{cells}</tr>")
        parts.append("</tbody></table></div>")
    return "".join(parts)


def _scenario_evidence(case):
    fields = (("Archive SHA-256", "archive_sha256"), ("Archive size (bytes)", "archive_bytes"),
              ("Restore exit code", "restore_exit_code"), ("HTTP probe order", "probe_order_id"),
              ("HTTP status", "probe_http_status"))
    facts = "".join(_fact(label, case[key]) for label, key in fields if case.get(key) is not None)
    if "orders_table_present" in case:
        facts += _fact("Orders table after restore", "Present" if case["orders_table_present"] else "Absent")
    if case.get("outcome") == "restore_failed":
        facts += _fact("HTTP order probe", "Skipped after restore failure")
    sections = [f"<details><summary>{_label(case['name'], NAMES)}</summary><dl class='facts'>{facts}</dl>"]
    sections.append(_comparison_html(case.get("comparison")))
    if case.get("restore_error"):
        sections.append("<h4>Restore diagnostic</h4><pre>" + _text(case["restore_error"]) + "</pre>")
    if case.get("probe_error"):
        sections.append("<h4>HTTP probe diagnostic</h4><pre>" + _text(case["probe_error"]) + "</pre>")
    if case.get("expected_evidence"):
        expected = json.dumps(case["expected_evidence"], indent=2, ensure_ascii=False)
        sections.append("<h4>Expected evidence</h4><pre>" + _text(expected) + "</pre>")
    sections.append("</details>")
    return "".join(sections)


def _render(report):
    cases = report["scenarios"]
    matched = sum(case["passed"] is True for case in cases)
    metrics = (_fact("Scenarios matching expectations", f"{matched} / {len(cases)}")
               + _fact("Receipts in the journal", report["journal_count"]))
    rows = []
    for case in cases:
        comparison = case.get("comparison")
        count = "—" if comparison is None else f"{comparison['matched_count']} / {comparison['expected_count']}"
        verdict = "<span class='good'>Matched</span>" if case["passed"] else "<span class='bad'>Unexpected</span>"
        rows.append(f"<tr><th scope='row'>{_label(case['name'], NAMES)}</th>"
                    f"<td>{_label(case['expected_outcome'], OUTCOMES)}</td>"
                    f"<td>{_label(case['outcome'], OUTCOMES)}</td>"
                    f"<td>{_seconds(case.get('restore_seconds'))}</td>"
                    f"<td>{_seconds(case.get('ready_seconds'))}</td><td>{_text(count)}</td><td>{verdict}</td></tr>")
    versions = [f"{label} {_text(report[key])}" for label, key in (
        ("Server", "postgres_version"), ("Dump tool", "pg_dump_version"),
        ("Restore tool", "pg_restore_version")) if report.get(key)]
    evidence = "".join(_scenario_evidence(case) for case in cases)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PostgreSQL recovery drill</title><style>{STYLE}</style></head><body><main>
<header><h1>PostgreSQL recovery drill</h1>
<p class="subtle">Run <code>{_text(report['run_id'])}</code> · Started {_text(report['started_at'])}</p></header>
<dl class="metrics">{metrics}</dl>
<h2>Recovery outcomes</h2>
<p class="note">First order read measures elapsed time from the start of restoration through the first successful
HTTP order lookup. Exact matches compare each receipt’s order ID, amount and currency.</p>
<div class="scroll"><table><thead><tr><th scope="col">Archive</th><th scope="col">Expected</th>
<th scope="col">Observed</th><th scope="col">Restore</th><th scope="col">First order read</th>
<th scope="col">Exact matches</th><th scope="col">Scenario check</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<h2>Evidence by scenario</h2>{evidence}
<footer><p>{' · '.join(versions)}</p><a href="report.json">Open JSON report</a>
 · <a href="observations.json">Open captured journal and recovered orders</a></footer>
</main></body></html>
"""


def _write_atomic(path, text):
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as output:
            output.write(text)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def write_report(out_dir, report):
    """Save a machine-readable report and its portable HTML view."""
    json_text = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    html = _render(report)
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    _write_atomic(destination / "report.json", json_text)
    _write_atomic(destination / "report.html", html)
