"""Deterministically replay three query orders after the original AIS trial."""

from copy import deepcopy
from datetime import datetime

import ais_audit as audit


PAIR_ID = "case-e28855ccfbd2"
PAIR_COUNTEREXAMPLE = "constructed-same-preview-wrong-window"
ORDER_DESCRIPTIONS = {
    "original": "The original evidence-based tool selector at every step.",
    "cutoff_first": "Use cutoff for the first additional ordinary query in every unresolved window.",
    "conditional_cutoff_first": (
        "Use cutoff first when an unresolved missing vessel has an accepted, ordinary-cutoff "
        "next-window report in the preview, all currently linked reports have earlier receipts "
        "than the latest visible receipt, that latest receipt lies after window end and by the "
        "declared cutoff, and no current-window original report is yet linked."
    ),
}


def qualifies_for_cutoff(manifest, mmsi):
    if mmsi not in manifest["missing"]:
        return False
    window = manifest["window"]
    start = window["start_utc"]
    end = window["end_utc"]
    declared = window["declared_input_cutoff_utc"]
    hint = next(row for row in manifest["precheck"] if row["mmsi"] == mmsi)
    latest = hint["latest_receive_utc_at_publication"]
    linked = [
        row for row in manifest["initial_evidence"]
        if row["mmsi"] == mmsi and row["tool"] in ("source", "cutoff")
    ]
    neighbor_link = any(
        row["tool"] == "cutoff"
        and row["accepted"]
        and row["effective_input_cutoff_utc"] == declared
        and audit.window_from_time(row["event_utc"], window) == end
        for row in linked
    )
    return (
        latest is not None
        and end < latest <= declared
        and end in hint["seen_in_adjacent_stream_windows"]
        and neighbor_link
        and all(row["received_utc"] < latest for row in linked)
        and all(not start <= row["event_utc"] < end for row in linked)
    )


def replay(case, oracle, order, *, budget=audit.BUDGET, prefix=()):
    manifest = case["manifest"]
    issued = list(prefix)
    while True:
        records = audit.visible_records(manifest, issued)
        findings = audit.visible_findings(manifest, records)
        unresolved = [row["mmsi"] for row in findings if row["finding"] == "unresolved"]
        if not unresolved or len(issued) == budget:
            break
        queried = {row["covered_tool"] for row in issued}
        if not issued and (
            order == "cutoff_first"
            or order == "conditional_cutoff_first"
            and any(qualifies_for_cutoff(manifest, mmsi) for mmsi in unresolved)
        ):
            tool = "cutoff"
        else:
            tool = audit.fixed_tool(manifest, records, unresolved, queried)
        issued.append({"tool": tool, "covered_tool": tool, "rows": case["logs"][tool]})
    return audit.grade_case(case, oracle, findings, issued)


def construct_counterexample(case, oracle):
    """Preserve the entire preview and switch the missing report's hidden cause."""
    clone = deepcopy(case)
    simulated = deepcopy(oracle)
    manifest = clone["manifest"]
    window = manifest["window"]
    mmsi, = manifest["missing"]
    intended = simulated["underlying_findings"][mmsi]
    report_id = intended["record_id"]
    assert intended["finding"] == "input_cutoff"
    assert report_id in simulated["actual_cutoff_overrides"]
    del simulated["actual_cutoff_overrides"][report_id]
    simulated["actual_processing_time_records"].append(report_id)
    simulated["underlying_findings"][mmsi]["finding"] = "processing_time_window"

    start = datetime.fromisoformat(window["start_utc"].replace("Z", "+00:00"))
    deadline = datetime.fromisoformat(window["declared_input_cutoff_utc"].replace("Z", "+00:00"))
    _, assignments = audit.simulate_reporting(
        simulated["raw_frozen_input"],
        int((deadline - start).total_seconds()),
        set(simulated["actual_processing_time_records"]),
        simulated["actual_cutoff_overrides"],
    )
    target_members = sorted({row["mmsi"] for row, assigned in assignments if assigned == 0})
    assert manifest["stream_members"] == target_members
    record = next(row for row in clone["logs"]["source"] if row["record_id"] == report_id)
    assigned = audit.window_from_time(record["processed_utc"], window)
    assert assigned == window["end_utc"]
    assert assigned in manifest["precheck"][0]["seen_in_adjacent_stream_windows"]
    receipt = next(row for row in clone["logs"]["cutoff"] if row["record_id"] == report_id)
    receipt["effective_input_cutoff_utc"] = window["declared_input_cutoff_utc"]
    receipt["accepted"] = True
    clone["logs"]["window"].append({
        "evidence_id": f"window-{report_id}",
        "tool": "window",
        "mmsi": mmsi,
        "record_id": report_id,
        "assigned_start_utc": assigned,
    })
    clone["logs"]["window"].sort(key=lambda row: (row["mmsi"], row["record_id"]))
    assert clone["manifest"] == case["manifest"]
    return clone, simulated


def indexed_receipt_prefix(case):
    """Lookup the intake receipt by the preview's exact vessel and receipt clock."""
    manifest = case["manifest"]
    rows = [
        row for row in case["logs"]["cutoff"]
        for hint in manifest["precheck"]
        if row["mmsi"] == hint["mmsi"]
        and row["received_utc"] == hint["latest_receive_utc_at_publication"]
    ]
    assert len(rows) == 1
    return {"tool": "receipt_index", "covered_tool": "cutoff", "rows": rows}


def run_followup():
    design, cases = audit.observations()
    truth = audit.read_truth(design, cases)
    paired, paired_truth = construct_counterexample(cases[PAIR_ID], truth[PAIR_ID])
    protocol = {
        "conditions": ORDER_DESCRIPTIONS,
        "max_additional_queries": audit.BUDGET,
        "original_observations_sha256": design["observations_sha256"],
        "original_truth_sha256": design["truth_sha256"],
        "paired_manifest_sha256": audit.checksum(paired["manifest"]),
        "counterexample_name": PAIR_COUNTEREXAMPLE,
        "scope": (
            "Follow-up mechanism check after the original result. Rules and constructed same-preview "
            "case are set before running these comparisons. The clone is not an independent holdout."
        ),
        "unchanged_components": (
            "Initial evidence, member routing, proof requirements and number of additional queries. "
            "Each policy sees only chosen log rows before making the next decision."
        ),
    }
    protocol_hash = audit.checksum(protocol)
    old_receipts = audit.audited_receipts(design, cases, "fixed")
    old_answers = audit.require_answer_scope(
        audit.load_json(audit.LOG_DIR / "fixed_answers.json"), cases, fixed=True,
    )
    by_case = []
    for case_id, case in cases.items():
        oracle = truth[case_id]
        flagged = case["manifest"]["routing"]["member_alert"]
        row = {
            "case_id": case_id,
            "scenario": oracle["group"],
            "member_alert": flagged,
            "observable_condition": any(
                qualifies_for_cutoff(case["manifest"], mmsi)
                for mmsi in case["manifest"]["missing"]
            ),
        }
        if flagged:
            row["policies"] = {
                order: replay(case, oracle, order) for order in ORDER_DESCRIPTIONS
            }
            original_grade = audit.grade_case(case, oracle, old_answers[case_id], old_receipts.get(case_id, []))
            if row["policies"]["original"] != original_grade:
                raise ValueError(f"Original replay differs from the recorded baseline for {case_id}")
        by_case.append(row)

    counterexample = {
        "case_id": PAIR_COUNTEREXAMPLE,
        "scenario": "constructed_processing_time_window",
        "member_alert": True,
        "observable_condition": True,
        "policies": {order: replay(paired, paired_truth, order) for order in ORDER_DESCRIPTIONS},
    }
    original_anomalies = [row for row in by_case if row["member_alert"]]
    original_pair = next(row for row in by_case if row["case_id"] == PAIR_ID)
    pair = [original_pair, counterexample]

    def compare(rows):
        by_order = {}
        for order in ORDER_DESCRIPTIONS:
            judged = [row["policies"][order] for row in rows]
            by_order[order] = {
                "diagnosis_windows": len(rows),
                "fully_diagnosed_windows": sum(grade["complete"] for grade in judged),
                "additional_queries": sum(grade["queries"] for grade in judged),
                "improved_vs_original": [
                    row["case_id"] for row in rows
                    if row["policies"][order]["complete"] and not row["policies"]["original"]["complete"]
                ],
                "regressed_vs_original": [
                    row["case_id"] for row in rows
                    if row["policies"]["original"]["complete"] and not row["policies"][order]["complete"]
                ],
                "different_query_sequence": [
                    row["case_id"] for row in rows
                    if row["policies"][order]["tool_sequence"] != row["policies"]["original"]["tool_sequence"]
                ],
            }
        return by_order

    receipt_demo = []
    for label, case, oracle in ((PAIR_ID, cases[PAIR_ID], truth[PAIR_ID]), (PAIR_COUNTEREXAMPLE, paired, paired_truth)):
        prefix = indexed_receipt_prefix(case)
        receipt_demo.append({
            "case_id": label,
            "same_target_record_receipt": prefix["rows"][0],
            "budget_two_including_receipt": replay(case, oracle, "original", prefix=(prefix,)),
            "budget_three_including_receipt": replay(case, oracle, "original", budget=3, prefix=(prefix,)),
        })

    result = {
        "protocol": protocol,
        "protocol_sha256": protocol_hash,
        "original_batch_windows": len(by_case),
        "original_batch": compare(original_anomalies),
        "constructed_pair": {
            "identical_initial_preview": cases[PAIR_ID]["manifest"] == paired["manifest"],
            "diagnosis": compare(pair),
            "windows": pair,
        },
        "extended_batch_including_constructed_case": compare(original_anomalies + [counterexample]),
        "receipt_demo": {
            "evaluation": (
                "The same pre-selection lookup is charged one tool read on both pair members. "
                "The quota counts that receipt plus any downstream source or window reads. "
                "The three-read calculation relaxes the quota only to expose the proof cost."
            ),
            "windows": receipt_demo,
        },
        "per_original_window": by_case,
        "agent_reexecuted": False,
    }
    audit.write_frozen(audit.LOG_DIR / "query_order_followup.json", result)
    audit.output({field: value for field, value in result.items() if field != "per_original_window"})


if __name__ == "__main__":
    run_followup()
