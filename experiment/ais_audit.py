"""Replay fixed AIS reporting windows and expose only read-only diagnostic evidence."""

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path


HOME_DIR = Path(__file__).resolve().parent
DATA_DIR = HOME_DIR / "data"
LOG_DIR = HOME_DIR / "results"
SEED = 20261119
BUDGET = 2
WIDTH = 600
TOOLS = ("source", "cutoff", "window")
CAUSES = ("input_cutoff", "processing_time_window")
SCENARIOS = {
    "normal": 4,
    "legal_late": 4,
    "wrong_window": 4,
    "equal_count_swap": 4,
    "wrong_cutoff": 4,
    "mixed": 6,
    "insufficient": 4,
    "common_mode": 2,
}


def encode(data, *, pretty=False):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2 if pretty else None)


def output(data):
    print(encode(data, pretty=True))


def read_json(path):
    return Path(path).read_text(encoding="utf-8-sig")


def load_json(path):
    return json.loads(read_json(path))


def write_frozen(path, data):
    serialized = encode(data, pretty=True) + "\n"
    if path.exists():
        if read_json(path) != serialized:
            raise ValueError(f"Frozen file differs: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as target:
        target.write(serialized)


def checksum(data):
    return hashlib.sha256(encode(data).encode("utf-8")).hexdigest()


def utc_text(moment):
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id(rng, prefix):
    return f"{prefix}-{rng.getrandbits(48):012x}"


def event_window(seconds):
    return (seconds // WIDTH) * WIDTH


def canonical_members(raw_records, freeze):
    """The reference reads only raw records and the published window contract."""
    return {
        record["mmsi"]
        for record in raw_records
        if 0 <= record["event"] < WIDTH and record["received"] <= freeze
    }


def simulate_reporting(raw_records, freeze, time_overrides, cutoff_overrides):
    """Emulate receipts and window assignment separately from the batch reference."""
    intake = []
    assignments = []
    for record in raw_records:
        record_id = record["record_id"]
        used_cutoff = cutoff_overrides.get(record_id, freeze)
        accepted = record["received"] <= used_cutoff
        intake.append((record, used_cutoff, accepted))
        if accepted:
            clock = record["processed"] if record_id in time_overrides else record["event"]
            assignments.append((record, event_window(clock)))
    return intake, assignments


def make_case(rng, group, variant, number):
    case_id = new_id(rng, "case")
    base = datetime(2026, 8, 1, 6, tzinfo=timezone.utc) + timedelta(days=number)

    def at(second):
        return utc_text(base + timedelta(seconds=second))

    freeze = rng.randint(640, 665)
    published = freeze + 55
    later_freeze = freeze + 180
    raw_records = []
    cutoff_overrides = {}
    time_overrides = set()
    withheld = set()
    vessel_ids = set()
    injected = {}

    def new_vessel():
        while True:
            mmsi = str(rng.randrange(970000000, 979999999))
            if mmsi not in vessel_ids:
                vessel_ids.add(mmsi)
                return mmsi

    def record(mmsi, event, receipt, channel):
        row = {
            "record_id": new_id(rng, "r"),
            "mmsi": mmsi,
            "event": event,
            "received": receipt,
            "processed": receipt + rng.randint(3, 9),
            "channel": channel,
        }
        raw_records.append(row)
        return row

    def ordinary_ships():
        for index in range(rng.randint(3, 6)):
            mmsi = new_vessel()
            start = rng.randint(5, 400)
            record(mmsi, start, start + 12, "coast")
            if index < 2:
                record(mmsi, start + 80, start + 92, "coast")
        record(new_vessel(), -110, -98, "coast")
        record(new_vessel(), WIDTH + 4, WIDTH + 12, "coast")

    def late_control():
        mmsi = new_vessel()
        row = record(mmsi, WIDTH - 18, published + 13, "relay")
        # Its next-window appearance comes from another raw report.
        record(mmsi, WIDTH + 9, WIDTH + 15, "coast")
        return row

    def shifted(*, extra=False):
        mmsi = new_vessel()
        if extra:
            row = record(mmsi, -rng.randint(14, 42), rng.randint(8, 33), "relay")
            record(mmsi, -135, -120, "coast")
        else:
            row = record(mmsi, WIDTH - rng.randint(14, 42), WIDTH + rng.randint(5, 18), "relay")
            record(mmsi, WIDTH + 2, WIDTH + 4, "coast")
        time_overrides.add(row["record_id"])
        injected[mmsi] = {"finding": "processing_time_window", "record_id": row["record_id"]}
        return row

    def cut_shift(*, extra=False):
        mmsi = new_vessel()
        receipt = freeze + 17 if extra else freeze - 4
        row = record(mmsi, WIDTH - rng.randint(14, 42), receipt, "relay")
        cutoff_overrides[row["record_id"]] = freeze + 27 if extra else freeze - 12
        record(mmsi, WIDTH + 4, WIDTH + 10, "coast")
        injected[mmsi] = {"finding": "input_cutoff", "record_id": row["record_id"]}
        return row

    ordinary_ships()
    legal_late_row = late_control() if group in ("legal_late", "mixed") else None
    if group == "wrong_window":
        shifted(extra=bool(variant % 2))
    elif group == "equal_count_swap":
        shifted()
        if variant % 2:
            cut_shift(extra=True)
        else:
            shifted(extra=True)
    elif group == "wrong_cutoff":
        cut_shift(extra=bool(variant % 2))
    elif group == "mixed":
        shifted(extra=variant % 3 == 2)
        cut_shift(extra=variant % 3 != 1)
    elif group == "insufficient":
        row = shifted(extra=variant == 1) if variant < 2 else cut_shift(extra=variant == 3)
        withheld.add(("window" if variant < 2 else "cutoff", row["record_id"]))
    elif group == "common_mode":
        shifted(extra=bool(variant))

    intake, assignments = simulate_reporting(raw_records, freeze, time_overrides, cutoff_overrides)
    correct = canonical_members(raw_records, freeze)
    revised = canonical_members(raw_records, later_freeze)
    observed = {record["mmsi"] for record, start in assignments if start == 0}
    if group == "common_mode":
        # Separate batch code using the same faulty clock choice for the affected report.
        batch = {
            row["mmsi"] for row in raw_records
            if row["received"] <= freeze
            and 0 <= (row["processed"] if row["record_id"] in time_overrides else row["event"]) < WIDTH
        }
    else:
        batch = correct
    missing = batch - observed
    unexpected = observed - batch
    differences = missing | unexpected
    neighbors = {
        vessel: sorted({start for record, start in assignments if record["mmsi"] == vessel and abs(start) == WIDTH})
        for vessel in differences
    }
    relevant = [record for record in raw_records if record["mmsi"] in differences]

    def evidence(record, log_name):
        return {
            "evidence_id": f'{log_name}-{record["record_id"]}',
            "tool": log_name,
            "record_id": record["record_id"],
            "mmsi": record["mmsi"],
        }

    logs = {
        "source": [
            {
                **evidence(record, "source"),
                "event_utc": at(record["event"]),
                "received_utc": at(record["received"]),
                "processed_utc": at(record["processed"]),
                "channel": record["channel"],
            }
            for record in relevant
        ],
        "cutoff": [
            {
                **evidence(record, "cutoff"),
                "event_utc": at(record["event"]),
                "received_utc": at(record["received"]),
                "effective_input_cutoff_utc": at(used_cutoff),
                "accepted": accepted,
            }
            for record, used_cutoff, accepted in intake
            if record["mmsi"] in differences and ("cutoff", record["record_id"]) not in withheld
        ],
        "window": [
            {
                **evidence(record, "window"),
                "assigned_start_utc": at(start),
            }
            for record, start in assignments
            if record["mmsi"] in differences and ("window", record["record_id"]) not in withheld
        ],
    }
    for records in logs.values():
        records.sort(key=lambda row: (row["mmsi"], row["record_id"]))

    briefing = []
    for mmsi in sorted(differences):
        reports = [row for row in relevant if row["mmsi"] == mmsi and row["received"] <= published]
        briefing.append({
            "mmsi": mmsi,
            "direction": "missing" if mmsi in missing else "unexpected",
            "seen_in_adjacent_stream_windows": [at(start) for start in neighbors[mmsi]],
            "any_receive_after_nominal_event_window_end": any(
                row["received"] > event_window(row["event"]) + WIDTH for row in reports
            ),
            "latest_receive_utc_at_publication": max((at(row["received"]) for row in reports), default=None),
        })

    # Common evidence reflects what an operator can see before extra log queries.
    # Publish all receipt originals in most cases, only a partial join in others.
    if group in ("wrong_window", "wrong_cutoff") and variant == 0:
        attachments = [row for rows in logs.values() for row in rows]
    elif group in ("equal_count_swap", "mixed") and variant in (1, 4):
        attachments = [row for row in logs["source"] if row["channel"] == "relay"]
    elif variant % 3 == 2:
        attachments = [
            row for row in logs["cutoff"]
            if row["accepted"] and row["effective_input_cutoff_utc"] == at(freeze)
        ]
    elif variant % 3 == 1:
        attachments = [row for row in logs["source"] if row["channel"] == "coast"]
    else:
        attachments = list(logs["source"])

    manifest = {
        "case_id": case_id,
        "window": {
            "evidence_id": f"contract-{case_id}",
            "sea_area": "SYNTH-OFFSHORE-01",
            "start_utc": at(0),
            "end_utc": at(WIDTH),
            "declared_input_cutoff_utc": at(freeze),
            "published_utc": at(published),
            "revision_input_cutoff_utc": at(later_freeze),
        },
        "batch_count": len(batch),
        "stream_count": len(observed),
        "batch_members": sorted(batch),
        "stream_members": sorted(observed),
        "missing": sorted(missing),
        "unexpected": sorted(unexpected),
        "precheck": briefing,
        "initial_evidence": attachments,
    }
    manifest["routing"] = {
        "count_alert": len(batch) != len(observed),
        "member_alert": bool(differences),
        "additional_diagnosis": requires_agent(manifest),
    }
    private = {
        "case_id": case_id,
        "group": group,
        "correct_members": sorted(correct),
        "revised_correct_members": sorted(revised),
        "canonical_mismatch": sorted(correct ^ observed),
        "underlying_findings": injected,
        "late_control": None if legal_late_row is None else {
            "record_id": legal_late_row["record_id"],
            "mmsi": legal_late_row["mmsi"],
            "event_utc": at(legal_late_row["event"]),
            "received_utc": at(legal_late_row["received"]),
        },
        "raw_frozen_input": raw_records,
        "actual_cutoff_overrides": cutoff_overrides,
        "actual_processing_time_records": sorted(time_overrides),
    }
    if group != "common_mode":
        assert differences == set(injected), (group, case_id, differences, injected)
    if legal_late_row:
        mmsi = legal_late_row["mmsi"]
        assert mmsi not in batch | observed and mmsi in revised
        _, replayed = simulate_reporting(raw_records, later_freeze, time_overrides, cutoff_overrides)
        assert any(row["record_id"] == legal_late_row["record_id"] and start == 0 for row, start in replayed)
    return {"manifest": manifest, "logs": logs}, private


def prepare():
    rng = random.Random(SEED)
    groups = [(group, variant) for group, count in SCENARIOS.items() for variant in range(count)]
    rng.shuffle(groups)
    observations = {}
    truth = {}
    for number, (group, variant) in enumerate(groups):
        case, private = make_case(rng, group, variant, number)
        case_id = private["case_id"]
        assert case_id not in observations
        observations[case_id] = case
        truth[case_id] = private
    design = {
        "seed": SEED,
        "probe_budget": BUDGET,
        "case_count": len(observations),
        "observations_sha256": checksum(observations),
        "truth_sha256": checksum(truth),
    }
    write_frozen(DATA_DIR / "observations.json", observations)
    write_frozen(DATA_DIR / "truth.json", truth)
    write_frozen(DATA_DIR / "design.json", design)
    output(design)


def observations():
    design = load_json(DATA_DIR / "design.json")
    cases = load_json(DATA_DIR / "observations.json")
    if checksum(cases) != design["observations_sha256"]:
        raise ValueError("Observation file differs from frozen design")
    return design, cases


def window_from_time(moment, window):
    parsed = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    start = datetime.fromisoformat(window["start_utc"].replace("Z", "+00:00"))
    seconds = int((parsed - start).total_seconds())
    return utc_text(start + timedelta(seconds=event_window(seconds)))


def keyed(records, tool):
    return {row["record_id"]: row for row in records if row["tool"] == tool}


def evidence_support(manifest, records, mmsi):
    """Return explanations that the visible evidence supports for one membership delta."""
    contract = manifest["window"]
    start = contract["start_utc"]
    end = contract["end_utc"]
    declared = contract["declared_input_cutoff_utc"]
    missing = mmsi in manifest["missing"]
    records = [row for row in records if row["mmsi"] == mmsi]
    supported = []
    for row in keyed(records, "cutoff").values():
        if not start <= row["event_utc"] < end:
            continue
        expected_acceptance = row["received_utc"] <= declared
        if row["accepted"] != expected_acceptance and expected_acceptance == missing:
            supported.append({
                "mmsi": mmsi,
                "finding": "input_cutoff",
                "record_id": row["record_id"],
                "evidence_ids": [contract["evidence_id"], row["evidence_id"]],
            })
    for record_id, row in keyed(records, "source").items():
        assignment = keyed(records, "window").get(record_id)
        if assignment is None or row["received_utc"] > declared:
            continue
        event_start = window_from_time(row["event_utc"], contract)
        processing_start = window_from_time(row["processed_utc"], contract)
        entered = assignment["assigned_start_utc"]
        if entered == processing_start != event_start and ((event_start == start) if missing else (entered == start)):
            supported.append({
                "mmsi": mmsi,
                "finding": "processing_time_window",
                "record_id": record_id,
                "evidence_ids": [contract["evidence_id"], row["evidence_id"], assignment["evidence_id"]],
            })
    return supported


def requires_agent(manifest):
    records = manifest["initial_evidence"]
    return any(
        row["any_receive_after_nominal_event_window_end"]
        and row["seen_in_adjacent_stream_windows"]
        and len(evidence_support(manifest, records, row["mmsi"])) != 1
        for row in manifest["precheck"]
    )


def manifest_for(cases, show_all, selected=()):
    unknown = set(selected) - cases.keys()
    if unknown:
        raise ValueError(f"Unknown cases: {', '.join(sorted(unknown))}")
    manifests = [
        case["manifest"]
        for case in cases.values()
        if (not selected or case["manifest"]["case_id"] in selected)
        and (show_all or case["manifest"]["routing"]["additional_diagnosis"])
    ]
    return {
        "case_count": len(manifests),
        "additional_calls_per_case": BUDGET,
        "tools": {
            "source": "Diagnostic vessel original reports with event/receipt/processing clocks.",
            "cutoff": "Intake ledger with effective cutoff, observed acceptance and the recorded event/receipt clocks.",
            "window": "Published routing trace with assigned event-time window key by record ID.",
        },
        "answer_schema": {
            "case_id": "case-...",
            "findings": [{
                "mmsi": "each missing or unexpected vessel",
                "finding": "input_cutoff | processing_time_window | unresolved",
                "record_id": "raw report ID, omit for unresolved",
                "evidence_ids": ["IDs from the common briefing and your own probe receipts"],
            }],
        },
        "contracts": [
            "Synthetic records, one fixed offshore region. Compare distinct MMSI members for the half-open UTC event-time window.",
            "Each initial batch and stream report is judged at the same declared inclusive input cutoff for that case.",
            "The later revision cutoff is background information. It does not change the initial target.",
            "An arrival after nominal window end can still be included if it arrives by the declared input cutoff.",
            "A vessel can appear in adjacent windows because a different record belongs there. Follow the same record ID.",
            "The difference lists are diagnostic observations, not a diagnosis. Cite the window contract and supporting records.",
            "Trials can inspect at most two of the three log sources per case. Return unresolved when the observed log evidence is insufficient.",
        ],
        "cases": manifests,
    }


def trial_log(trial):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", trial):
        raise ValueError("Invalid trial name")
    return LOG_DIR / f"{trial}_probes.jsonl"


def receipts(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in read_json(path).splitlines() if line.strip()]


@contextmanager
def trial_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.with_suffix(".lock")
    with lock_file.open("x", encoding="ascii"):
        pass
    try:
        yield
    finally:
        lock_file.unlink()


def run_probes(design, cases, trial, requests):
    if not isinstance(requests, list) or not requests:
        raise ValueError("Provide a nonempty JSON array of probe requests")
    path = trial_log(trial)
    with trial_lock(path):
        previous = receipts(path)
        used = Counter(record["case_id"] for record in previous)
        pending = []
        for request in requests:
            case_id = request["case_id"]
            tool = request["tool"]
            if case_id not in cases or tool not in TOOLS:
                raise ValueError(f"Invalid case or tool: {case_id}, {tool}")
            routing = cases[case_id]["manifest"]["routing"]
            eligible = routing["member_alert"] if trial == "fixed" else routing["additional_diagnosis"]
            if not eligible:
                raise ValueError(f"Case {case_id} did not enter diagnostic routing for trial {trial}")
            if used[case_id] >= BUDGET:
                raise ValueError(f"Exhausted {BUDGET}-query budget for {case_id}")
            used[case_id] += 1
            pending.append({
                "trial": trial,
                "case_id": case_id,
                "tool": tool,
                "case_query": used[case_id],
                "issued_utc": utc_text(datetime.now(timezone.utc)),
                "observations_sha256": design["observations_sha256"],
                "rows": cases[case_id]["logs"][tool],
            })
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.writelines(encode(reply) + "\n" for reply in pending)
    return pending


def audited_receipts(design, cases, trial):
    by_case = {}
    for row in receipts(trial_log(trial)):
        case_id = row["case_id"]
        tool = row["tool"]
        case = cases[case_id]
        routing = case["manifest"]["routing"]
        eligible = routing["member_alert"] if trial == "fixed" else routing["additional_diagnosis"]
        seen = by_case.setdefault(case_id, [])
        if (
            row["trial"] != trial
            or row["observations_sha256"] != design["observations_sha256"]
            or row["rows"] != case["logs"][tool]
            or row["case_query"] != len(seen) + 1
            or len(seen) >= BUDGET
            or not eligible
        ):
            raise ValueError(f"Inconsistent receipt for {trial}, {case_id}, {tool}")
        seen.append(row)
    return by_case


def visible_records(manifest, issued):
    records = {row["evidence_id"]: row for row in manifest["initial_evidence"]}
    for reply in issued:
        for row in reply["rows"]:
            records[row["evidence_id"]] = row
    return list(records.values())


def affected_members(manifest):
    return sorted(set(manifest["missing"]) | set(manifest["unexpected"]))


def fixed_tool(manifest, records, open_members, queried):
    """Prefer proof-completing calls and still try plausible alternate explanations."""
    contract = manifest["window"]
    start = contract["start_utc"]
    end = contract["end_utc"]
    cutoff = contract["declared_input_cutoff_utc"]
    observed_source = keyed(records, "source")
    observed_windows = keyed(records, "window")
    hints = {row["mmsi"]: row for row in manifest["precheck"]}
    scores = {tool: 0 for tool in TOOLS if tool not in queried}

    def credit(tool, weight):
        if tool in scores:
            scores[tool] += weight

    for mmsi in open_members:
        missing = mmsi in manifest["missing"]
        reachable = []
        for record_id, row in observed_source.items():
            if row["mmsi"] != mmsi or row["received_utc"] > cutoff:
                continue
            event_start = window_from_time(row["event_utc"], contract)
            processing_start = window_from_time(row["processed_utc"], contract)
            if event_start != processing_start and (event_start if missing else processing_start) == start:
                reachable.append(record_id)
        witness_in_trace = any(
            row["mmsi"] == mmsi
            and ((row["assigned_start_utc"] != start) if missing else (row["assigned_start_utc"] == start))
            and record_id not in observed_source
            for record_id, row in observed_windows.items()
        )
        after_cutoff = (hints[mmsi]["latest_receive_utc_at_publication"] or "") > cutoff
        late_candidate = any(
            row["mmsi"] == mmsi
            and start <= row["event_utc"] < end
            and row["received_utc"] > cutoff
            for row in observed_source.values()
        )
        credit("cutoff", 1)
        if (not missing and after_cutoff) or late_candidate:
            credit("cutoff", 6)
        if reachable:
            if "window" in queried and not any(record_id in observed_windows for record_id in reachable):
                credit("cutoff", 5)
            else:
                credit("window", 6)
        elif witness_in_trace:
            credit("source", 6)
        elif "source" not in queried and "window" not in queried and len(queried) < BUDGET - 1:
            credit("window", 2)
    # Resolve score ties by the lowest number of prerequisites.
    preference = ("cutoff", "window", "source")
    return max(scores, key=lambda tool: (scores[tool], -preference.index(tool)))


def visible_findings(manifest, records):
    findings = []
    for mmsi in affected_members(manifest):
        supported = evidence_support(manifest, records, mmsi)
        if len(supported) == 1:
            findings.append(supported[0])
        else:
            inspected = sorted(row["evidence_id"] for row in records if row["mmsi"] == mmsi)
            findings.append({"mmsi": mmsi, "finding": "unresolved", "evidence_ids": inspected})
    return findings


def run_fixed(design, cases):
    issued = audited_receipts(design, cases, "fixed")
    answers = []
    for case_id, case in cases.items():
        manifest = case["manifest"]
        if not manifest["routing"]["member_alert"]:
            continue
        probes = issued.get(case_id, [])
        while True:
            records = visible_records(manifest, probes)
            findings = visible_findings(manifest, records)
            open_members = [row["mmsi"] for row in findings if row["finding"] == "unresolved"]
            if not open_members or len(probes) == BUDGET:
                break
            selected = fixed_tool(manifest, records, open_members, {probe["tool"] for probe in probes})
            request = {"case_id": case_id, "tool": selected}
            probes.extend(run_probes(design, cases, "fixed", [request]))
        answers.append({"case_id": case_id, "findings": findings})
    write_frozen(LOG_DIR / "fixed_answers.json", answers)
    return {"answered_cases": len(answers), "queries": len(receipts(trial_log("fixed")))}


def require_answer_scope(answers, cases, *, fixed):
    if not isinstance(answers, list):
        raise ValueError("Answers must be an array")
    scope = "member_alert" if fixed else "additional_diagnosis"
    expected = {case_id for case_id, case in cases.items() if case["manifest"]["routing"][scope]}
    if not fixed and any(answer["case_id"] not in expected for answer in answers):
        expected = set(cases)
    mapped = {}
    for answer in answers:
        case_id = answer["case_id"]
        if case_id not in expected or case_id in mapped:
            raise ValueError(f"Unexpected or duplicate case answer: {case_id}")
        findings = answer["findings"]
        if not isinstance(findings, list):
            raise ValueError(f"Expected findings array for {case_id}")
        actual_members = [row["mmsi"] for row in findings]
        if sorted(actual_members) != affected_members(cases[case_id]["manifest"]):
            raise ValueError(f"Answer must cover all difference vessels exactly once: {case_id}")
        mapped[case_id] = findings
    if expected != mapped.keys():
        raise ValueError(f"Missing answers: {', '.join(sorted(expected - mapped.keys()))}")
    return mapped


def witness_matches(witness, intended):
    return witness["finding"] == intended["finding"] and witness["record_id"] == intended["record_id"]


def grade_case(case, oracle, findings, issued):
    manifest = case["manifest"]
    rows = {row["evidence_id"]: row for row in visible_records(manifest, issued)}
    contract = manifest["window"]["evidence_id"]
    answers = {}
    for answer in findings:
        mmsi = answer["mmsi"]
        claimed = answer["finding"]
        cited = answer.get("evidence_ids", [])
        if claimed not in (*CAUSES, "unresolved") or not isinstance(cited, list):
            raise ValueError(f"Invalid evidence assertion: {manifest['case_id']}, {mmsi}")
        if any(not isinstance(key, str) or key not in rows and key != contract for key in cited):
            status = "unsupported"
        elif claimed == "unresolved":
            status = "unresolved"
        elif not witness_matches(
            {"finding": claimed, "record_id": answer.get("record_id")},
            oracle["underlying_findings"][mmsi],
        ):
            status = "wrong"
        elif contract not in cited or not any(
            witness_matches(witness, oracle["underlying_findings"][mmsi])
            for witness in evidence_support(manifest, [rows[key] for key in cited if key in rows], mmsi)
        ):
            status = "unsupported"
        else:
            status = "supported_correct"
        answers[mmsi] = status
    return {
        "judgments": answers,
        "complete": bool(answers) and all(status == "supported_correct" for status in answers.values()),
        "queries": len(issued),
        "tool_sequence": [row["tool"] for row in issued],
    }


def evidence_feasible(case, oracle):
    manifest = case["manifest"]
    expected = oracle["underlying_findings"]
    for size in range(BUDGET + 1):
        for subset in combinations(TOOLS, size):
            rows = manifest["initial_evidence"] + [row for tool in subset for row in case["logs"][tool]]
            if all(
                any(witness_matches(proof, intended) for proof in evidence_support(manifest, rows, mmsi))
                for mmsi, intended in expected.items()
            ):
                return True
    return False


def strict_handoff_records(case):
    """Find a subset where two explanations are compatible with a linked report."""
    manifest = case["manifest"]
    if not manifest["routing"]["additional_diagnosis"]:
        return []
    contract = manifest["window"]
    start = contract["start_utc"]
    end = contract["end_utc"]
    freeze = contract["declared_input_cutoff_utc"]
    publication = contract["published_utc"]
    initial = manifest["initial_evidence"]
    attached_stages = {
        (row["tool"], row["record_id"]) for row in initial if row["tool"] != "source"
    }
    # Consult record identifiers in the log indexes, without reading hidden clock
    # values, routing decisions, labels or answers.
    index = {
        tool: {(row["mmsi"], row["record_id"]) for row in case["logs"][tool]}
        for tool in TOOLS
    }

    def both_stages_unseen(row):
        record_id = row["record_id"]
        return all((tool, record_id) not in attached_stages for tool in ("cutoff", "window"))

    def has_new_stage(row):
        target = (row["mmsi"], row["record_id"])
        return any(target in index[tool] for tool in ("cutoff", "window"))

    ambiguous = []
    for mmsi in affected_members(manifest):
        if evidence_support(manifest, initial, mmsi):
            continue
        source = [row for row in initial if row["tool"] == "source" and row["mmsi"] == mmsi]
        if len(source) != sum(vessel == mmsi for vessel, _ in index["source"]):
            continue
        if mmsi in manifest["missing"]:
            eligible = [
                row for row in source
                if start <= row["event_utc"] < end and row["received_utc"] <= freeze
            ]
            if len(eligible) != 1:
                continue
            row = eligible[0]
            if row["processed_utc"] >= end and both_stages_unseen(row) and has_new_stage(row):
                ambiguous.append({"mmsi": mmsi, "candidate_records": [row["record_id"]]})
        else:
            candidates = [row for row in source if both_stages_unseen(row)]
            cutoff_candidates = [
                row for row in candidates
                if start <= row["event_utc"] < end and freeze < row["received_utc"] <= publication
            ]
            clock_candidates = [
                row for row in candidates
                if not start <= row["event_utc"] < end
                and row["received_utc"] <= freeze
                and start <= row["processed_utc"] < end
            ]
            if cutoff_candidates and clock_candidates and any(map(has_new_stage, cutoff_candidates + clock_candidates)):
                ambiguous.append({
                    "mmsi": mmsi,
                    "candidate_records": sorted(row["record_id"] for row in cutoff_candidates + clock_candidates),
                })
    return ambiguous


def strict_scope(design, cases):
    candidates = []
    for case_id, case in cases.items():
        competing = strict_handoff_records(case)
        if competing:
            candidates.append({"case_id": case_id, "compatible_members": competing})
    policy = {
        "observations_sha256": design["observations_sha256"],
        "case_count": len(candidates),
        "rule": [
            "This sensitivity scope is frozen from initial observations and log record-ID availability only.",
            "Use only original additional-diagnosis cases. A provable report is not escalated.",
            "Require a complete inventory of initially evidenced raw clocks for at least one difference vessel.",
            "For a missing vessel, require a single eligible report that could have been excluded by an earlier cutoff or sent to another window by its processing clock.",
            "For an unexpected vessel, require separate raw reports that could explain excess membership by a later cutoff or processing-clock assignment.",
            "Require the competing reports to lack initially linked cutoff/window decisions and at least one indexed supplemental decision.",
            "This is a conservative subset. Other cases may be ambiguous if relevant clocks are not initially attached.",
        ],
        "cases": candidates,
    }
    write_frozen(DATA_DIR / "strict_policy.json", policy)
    return policy


def read_truth(design, cases):
    truth = load_json(DATA_DIR / "truth.json")
    if checksum(truth) != design["truth_sha256"] or truth.keys() != cases.keys():
        raise ValueError("Truth file differs from frozen design")
    return truth


def replay_revision(design, cases):
    """Compare all members after the later cutoff while retaining injected faults."""
    truth = read_truth(design, cases)
    rows = []
    for case_id, oracle in truth.items():
        contract = cases[case_id]["manifest"]["window"]
        start = datetime.fromisoformat(contract["start_utc"].replace("Z", "+00:00"))
        cutoff = datetime.fromisoformat(contract["revision_input_cutoff_utc"].replace("Z", "+00:00"))
        freeze = int((cutoff - start).total_seconds())
        raw = oracle["raw_frozen_input"]
        expected = canonical_members(raw, freeze)
        if sorted(expected) != oracle["revised_correct_members"]:
            raise ValueError(f"Revision reference differs from frozen oracle for {case_id}")
        _, assignments = simulate_reporting(
            raw,
            freeze,
            set(oracle["actual_processing_time_records"]),
            oracle["actual_cutoff_overrides"],
        )
        observed = {record["mmsi"] for record, window in assignments if window == 0}
        control = oracle["late_control"]
        rows.append({
            "case_id": case_id,
            "revision_input_cutoff_utc": contract["revision_input_cutoff_utc"],
            "independent_reference_members": sorted(expected),
            "stream_replay_members": sorted(observed),
            "missing": sorted(expected - observed),
            "unexpected": sorted(observed - expected),
            "late_control_in_both": control["mmsi"] in expected & observed if control else None,
        })
    result = {
        "evaluation": "deterministic second-cutoff replay, independent reference, retained original fault rules",
        "frozen_design": design,
        "replayed_windows": len(rows),
        "matched_membership_windows": sum(not row["missing"] and not row["unexpected"] for row in rows),
        "mismatched_membership_windows": sum(bool(row["missing"] or row["unexpected"]) for row in rows),
        "late_control_windows": sum(row["late_control_in_both"] is not None for row in rows),
        "late_control_entered_both_windows": sum(row["late_control_in_both"] is True for row in rows),
        "per_window": rows,
    }
    write_frozen(LOG_DIR / "revision_check.json", result)
    return {field: value for field, value in result.items() if field != "per_window"}


def comparison_summary(case_ids, results):
    grouped = [results[case_id] for case_id in case_ids]
    findings = Counter(status for row in grouped for status in row["judgments"].values())
    return {
        "windows": len(grouped),
        "fully_diagnosed_windows": sum(row["complete"] for row in grouped),
        "incomplete_windows": sum(not row["complete"] for row in grouped),
        "vessel_findings": dict(sorted(findings.items())),
        "additional_queries": sum(row["queries"] for row in grouped),
        "queried_windows": sum(row["queries"] > 0 for row in grouped),
    }


def policy_summary(cases, truth, fixed, diagnostic, *, audit, prefer_agent):
    formal = [case_id for case_id in cases if truth[case_id]["group"] != "common_mode"]
    flag = "count_alert" if audit == "counts" else "member_alert"
    flagged = [case_id for case_id in formal if cases[case_id]["manifest"]["routing"][flag]]
    results = {
        case_id: diagnostic[case_id] if prefer_agent and case_id in diagnostic else fixed[case_id]
        for case_id in flagged
    }
    distinct = sum(bool(truth[case_id]["canonical_mismatch"]) for case_id in formal)
    false_positives = sum(not truth[case_id]["canonical_mismatch"] for case_id in flagged)
    return {
        "evaluation": (
            "offline counterfactual using agents' real responses on the shared membership-triggered cases"
            if prefer_agent and audit == "counts"
            else "observed agent queries plus deterministic precheck/fallback"
            if prefer_agent
            else "deterministic workflow applied to the frozen routing policy"
        ),
        "formal_windows": len(formal),
        "true_anomaly_windows_denominator": distinct,
        "flagged_windows": len(flagged),
        "false_positive_windows": false_positives,
        "missed_anomaly_windows": distinct - len(flagged) + false_positives,
        "omitted_from_diagnosis_windows": len(formal) - len(flagged),
        "agent_windows": sum(case_id in diagnostic for case_id in flagged) if prefer_agent else 0,
        **comparison_summary(flagged, results),
    }


def score_results(design, cases, trial, answers_path):
    if trial == "fixed":
        raise ValueError("Score requires a separate measured agent trial")
    truth = read_truth(design, cases)
    raw_fixed = require_answer_scope(load_json(LOG_DIR / "fixed_answers.json"), cases, fixed=True)
    raw_agent = require_answer_scope(load_json(answers_path), cases, fixed=False)
    fixed_probes = audited_receipts(design, cases, "fixed")
    agent_probes = audited_receipts(design, cases, trial)

    def grade(arm, evidence):
        return {
            case_id: grade_case(cases[case_id], truth[case_id], findings, evidence.get(case_id, []))
            for case_id, findings in arm.items()
        }

    fixed = grade(raw_fixed, fixed_probes)
    agent = grade(raw_agent, agent_probes)
    routed_agent = {
        case_id: row for case_id, row in agent.items()
        if cases[case_id]["manifest"]["routing"]["additional_diagnosis"]
    }
    formal_anomalies = [
        case_id for case_id, oracle in truth.items()
        if oracle["group"] != "common_mode" and oracle["canonical_mismatch"]
    ]
    feasible = {
        case_id: evidence_feasible(cases[case_id], truth[case_id])
        for case_id in formal_anomalies
    }
    groups = {}
    by_window = []
    for group in SCENARIOS:
        scoped = sorted(case_id for case_id, oracle in truth.items() if oracle["group"] == group)
        group_rows = []
        for case_id in scoped:
            case = cases[case_id]
            oracle = truth[case_id]
            flags = case["manifest"]["routing"]
            row = {
                "case_id": case_id,
                "scenario": group,
                "canonical_mismatch_members": oracle["canonical_mismatch"],
                "count_alert": flags["count_alert"],
                "member_alert": flags["member_alert"],
                "additional_diagnosis": flags["additional_diagnosis"],
                "evidence_feasible_within_budget": feasible.get(case_id),
                "fixed": fixed.get(case_id),
                "agent": agent.get(case_id),
            }
            if oracle["late_control"]:
                row["late_control"] = oracle["late_control"]
                row["late_control_in_revision"] = oracle["late_control"]["mmsi"] in oracle["revised_correct_members"]
            group_rows.append(row)
            by_window.append(row)
        groups[group] = {
            "windows": len(scoped),
            "canonical_anomalies": sum(bool(row["canonical_mismatch_members"]) for row in group_rows),
            "count_alerts": sum(row["count_alert"] for row in group_rows),
            "membership_alerts": sum(row["member_alert"] for row in group_rows),
            "fully_diagnosed_fixed": sum(row["fixed"]["complete"] for row in group_rows if row["fixed"]),
            "fully_diagnosed_agent": sum(row["agent"]["complete"] for row in group_rows if row["agent"]),
        }

    results = {
        "frozen_design": design,
        "formal_anomalies_evidence_feasible": {
            "anomaly_windows": len(formal_anomalies),
            "feasible_with_at_most_two_extra_queries": sum(feasible.values()),
            "infeasible_with_at_most_two_extra_queries": sum(not value for value in feasible.values()),
            "window_selection": "all formal anomalies, including those resolved with initial evidence",
        },
        "shared_additional_diagnosis_cases": {
            "fixed": comparison_summary(routed_agent, fixed),
            "agent": comparison_summary(routed_agent, routed_agent),
            "evidence_feasible_within_budget": sum(feasible[case_id] for case_id in routed_agent),
        },
        "policy_results": {
            f"{audit}+{diagnoser}": policy_summary(
                cases, truth, fixed, routed_agent, audit=audit, prefer_agent=diagnoser == "routed_agent",
            )
            for audit in ("counts", "members")
            for diagnoser in ("fixed", "routed_agent")
        },
        "scenario_results": groups,
        "per_window": by_window,
    }
    if agent.keys() == cases.keys():
        results["all_case_agent"] = {
            "evaluation": "one blind batch with an answer for each case; only the 20 shared routing cases could query logs",
            "answered_windows": len(agent),
            "formal_windows": sum(oracle["group"] != "common_mode" for oracle in truth.values()),
            "common_mode_controls": sum(oracle["group"] == "common_mode" for oracle in truth.values()),
            "diagnosis_on_formal_anomalies": comparison_summary(formal_anomalies, agent),
            "total_additional_queries": sum(len(rows) for rows in agent_probes.values()),
            "windows_using_additional_queries": len(agent_probes),
        }
    strict_file = DATA_DIR / "strict_policy.json"
    if strict_file.exists():
        policy = load_json(strict_file)
        if policy["observations_sha256"] != design["observations_sha256"]:
            raise ValueError("Strict sensitivity scope refers to different observations")
        matched = [row["case_id"] for row in policy["cases"]]
        matched_agent = {case_id: agent[case_id] for case_id in matched}
        results["strict_handoff_sensitivity"] = {
            "evaluation": "offline subset of the same recorded agent queries; no separate trial",
            "case_ids": matched,
            "fixed": comparison_summary(matched, fixed),
            "agent": comparison_summary(matched, agent),
            "membership_route_projection": policy_summary(
                cases, truth, fixed, matched_agent, audit="members", prefer_agent=True,
            ),
        }
        results["strict_handoff_sensitivity"]["membership_route_projection"]["evaluation"] = (
            "offline projection retaining fixed diagnosis outside the stricter subgroup"
        )
    write_frozen(LOG_DIR / f"{trial}_scores.json", results)
    return {field: value for field, value in results.items() if field != "per_window"}


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    sub = command.add_subparsers(dest="mode", required=True)
    sub.add_parser("prepare", help="Write or verify the frozen synthetic cases")
    manifest = sub.add_parser("manifest", help="Return only public case observations and common evidence")
    manifest.add_argument("--trial", default="agent", help="Trial name (for alignment with probe and score)")
    manifest.add_argument("--all", action="store_true", help="Include untriggered cases and controls")
    manifest.add_argument("--ids", action="store_true", help="Only list the selected case IDs")
    manifest.add_argument("--case", action="append", default=[], help="Restrict to this case ID, repeat as needed")
    manifest.add_argument("--compact", action="store_true", help="Print compact JSON")
    probe = sub.add_parser("probe", help="Read one or more case-specific logs and record query receipts")
    probe.add_argument("--trial", required=True)
    probe.add_argument("--requests", type=Path, help="Read a JSON array of requests from this file")
    probe.add_argument("--request-json", help="Read a JSON array of requests from this argument")
    probe.add_argument("--case", help="Case ID for a single query")
    probe.add_argument("--tool", choices=TOOLS, help="Log name for a single query")
    probe.add_argument("--compact", action="store_true", help="Print compact JSON")
    sub.add_parser("fixed", help="Run the frozen fixed diagnostic tree")
    sub.add_parser("strict-scope", help="Freeze the strict handoff subset without using truth or answers")
    sub.add_parser("revision-check", help="Compare independent batch and stream replay at the second cutoff")
    score = sub.add_parser("score", help="Evaluate both workflows against the frozen truth")
    score.add_argument("--trial", default="agent")
    score.add_argument("--answers", type=Path, required=True)
    return command


def main():
    arguments = parser().parse_args()
    if arguments.mode == "prepare":
        prepare()
        return
    design, cases = observations()
    if arguments.mode == "manifest":
        trial_log(arguments.trial)
        report = manifest_for(cases, arguments.all, arguments.case)
        if arguments.ids:
            report = [row["case_id"] for row in report["cases"]]
        print(encode(report, pretty=not arguments.compact))
    elif arguments.mode == "probe":
        if arguments.requests:
            requests = load_json(arguments.requests)
        elif arguments.request_json:
            requests = json.loads(arguments.request_json)
        elif arguments.case and arguments.tool:
            requests = [{"case_id": arguments.case, "tool": arguments.tool}]
        else:
            raise ValueError("Provide --requests, --request-json, or both --case and --tool")
        print(encode(run_probes(design, cases, arguments.trial, requests), pretty=not arguments.compact))
    elif arguments.mode == "fixed":
        output(run_fixed(design, cases))
    elif arguments.mode == "strict-scope":
        output(strict_scope(design, cases))
    elif arguments.mode == "revision-check":
        output(replay_revision(design, cases))
    elif arguments.mode == "score":
        output(score_results(design, cases, arguments.trial, arguments.answers))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"ais_audit: {error}", file=sys.stderr)
        sys.exit(2)
