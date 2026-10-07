"""A4's sealed, offline single-bit repair of budget-ledger.json only."""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from ..events import canonical_json, content_hash
from ..peer_reporting.budget import _locked, validate_ledger_identity, validate_ledger_state
from ..peer_reporting.live import (
    INDEX_FILE,
    JOURNAL_FILE,
    LEDGER_FILE,
    LOCK_FILE,
    PLAN_FILE,
    EvidenceError,
    _exclusive,
    _Journal,
)
from ..peer_reporting.live_integrity import _identifier, _tokens, verify_ledger_history
from ..peer_reporting.storage import atomic_json, check_seal, read_sealed, safe_child, seal
from . import PROTOCOL_ID

REPAIR_DIRECTORY = "ledger-repairs"
REPAIR_KIND = "peer_reporting_v11_ledger_repair"
BIT_MASKS = tuple(1 << bit for bit in range(8))
_SEAL_HEADER = b',"seal_hash":"'
_SEAL_PROPERTY = re.compile(rb',"seal_hash":"([0-9a-f]{64})"')
_ATTEMPT_FIELDS = ("reservation", "notifications", "observed", "status", "actual")


def reconstruct_attempts(records: list[dict], caps: dict) -> dict:
    """Reconstruct exactly A4's journaled fields, without unjournaled admitted_at times."""
    attempts: dict[str, dict] = {}
    for record in records:
        kind, data = record["kind"], record["data"]
        if kind not in {"reservation_admitted", "usage_observed", "usage_settled"}:
            continue
        reservation = _identifier(data.get("reservation_id"), "reservation ID")
        if kind == "reservation_admitted":
            if reservation in attempts:
                raise EvidenceError("duplicate journaled reservation admission")
            attempts[reservation] = {"reservation": caps["reserved_tokens_per_trial"], "notifications": {},
                                     "observed": 0, "status": "active", "actual": None}
            continue
        if reservation not in attempts:
            raise EvidenceError("journaled usage has no admitted reservation")
        attempt = attempts[reservation]
        if attempt["status"] == "settled":
            raise EvidenceError("journaled usage follows a final settlement")
        if kind == "usage_observed":
            notification = _identifier(data.get("notification_id"), "usage notification ID")
            if "cumulative_tokens" not in data:
                raise EvidenceError("journal usage observation omits its value")
            usage = _tokens(data["cumulative_tokens"], "journal usage observation", nullable=True)
            notifications = attempt["notifications"]
            if notification in notifications and notifications[notification] != usage:
                raise EvidenceError("conflicting journal usage notification")
            notifications[notification] = usage
            if usage is None:
                attempt["status"] = "unresolved"
            else:
                attempt["observed"] = max(attempt["observed"], usage)
                attempt["reservation"] = max(attempt["reservation"], usage)
        else:
            status = data.get("status")
            if status not in {"settled", "unresolved"} or "actual_tokens" not in data:
                raise EvidenceError("invalid journal usage settlement")
            actual = _tokens(data["actual_tokens"], "journal settled usage", nullable=status == "unresolved")
            if ((status == "unresolved" and actual is not None)
                    or (status == "settled" and actual < attempt["observed"])):
                raise EvidenceError("invalid journal settled usage")
            attempt.update(status=status, actual=actual)
    return attempts


def _check_attempts(state: dict, records: list[dict], caps: dict) -> dict:
    reconstructed = reconstruct_attempts(records, caps)
    retained = {key: {field: attempt[field] for field in _ATTEMPT_FIELDS}
                for key, attempt in state["attempts"].items()}
    if canonical_json(retained) != canonical_json(reconstructed):
        raise EvidenceError("restored budget attempts differ from the exact journal reconstruction")
    verify_ledger_history(state, records)
    return reconstructed


def _decode_candidate(raw: bytes, marker: dict, caps: dict, plan_hash: str) -> dict:
    state = json.loads(raw.decode("utf-8"))
    if raw != (canonical_json(state) + "\n").encode("utf-8"):
        raise ValueError("ledger candidate is not canonical UTF-8 JSON plus a newline")
    validate_ledger_state(state, marker, caps, plan_hash=plan_hash)
    return state


def single_bit_candidates(corrupt: bytes, marker: dict, caps: dict, *, plan_hash: str) -> list[dict]:
    """Consider all 8*len(bytes) changes, using raw canonical payload hashes before JSON parsing.

    Every structurally valid canonical ledger contains the fixed seal property.
    A candidate either changes a bit inside that property, or leaves an exact
    property already present in the corrupt bytes. Header variants enumerate
    the former, including broken quotes, keys and digest characters. For the
    latter, prefix SHA-256 states avoid rehashing the unchanged prefix. No JSON
    or UTF-8 validity assumption is made about the corrupt input.
    """
    validate_ledger_identity(marker, caps, plan_hash=plan_hash)
    found: dict[tuple[int, int], dict] = {}

    def consider(offset: int, mask: int) -> None:
        key = (offset, mask)
        if key in found:
            return
        raw = corrupt[:offset] + bytes([corrupt[offset] ^ mask]) + corrupt[offset + 1:]
        try:
            state = _decode_candidate(raw, marker, caps, plan_hash)
        except (ValueError, TypeError, KeyError, UnicodeError):
            return
        found[key] = {"byte_offset": offset, "bit_mask": mask, "bytes": raw, "state": state}

    # A valid candidate must have its sole trailing LF, even when that LF was flipped.
    if not corrupt or corrupt[-1] != 10:
        if corrupt and corrupt[-1] ^ 10 in BIT_MASKS:
            consider(len(corrupt) - 1, corrupt[-1] ^ 10)
        return list(found.values())

    headers = {_SEAL_HEADER}
    for offset, byte in enumerate(_SEAL_HEADER):
        for mask in BIT_MASKS:
            headers.add(_SEAL_HEADER[:offset] + bytes([byte ^ mask]) + _SEAL_HEADER[offset + 1:])
    starts = set()
    for header in headers:
        start = corrupt.find(header)
        while start >= 0:
            starts.add(start)
            start = corrupt.find(header, start + 1)
    width = len(_SEAL_HEADER) + 64 + 1
    for start in sorted(starts):
        segment = corrupt[start:start + width]
        if len(segment) != width:
            continue
        # A flip in the seal property cannot change the payload at all.
        payload_hash = hashlib.sha256(corrupt[:start] + corrupt[start + width:-1]).hexdigest().encode()
        for local_offset, byte in enumerate(segment):
            for mask in BIT_MASKS:
                restored = segment[:local_offset] + bytes([byte ^ mask]) + segment[local_offset + 1:]
                match = _SEAL_PROPERTY.fullmatch(restored)
                if match is not None and match[1] == payload_hash:
                    consider(start + local_offset, mask)

    for match in _SEAL_PROPERTY.finditer(corrupt):
        start, end = match.span()
        payload = corrupt[:start] + corrupt[end:-1]
        view = memoryview(payload)
        target = bytes.fromhex(match[1].decode("ascii"))
        prefix = hashlib.sha256()
        for offset, byte in enumerate(payload):
            original_offset = offset if offset < start else offset + end - start
            suffix = view[offset + 1:]
            for mask in BIT_MASKS:
                candidate_hash = prefix.copy()
                candidate_hash.update(bytes([byte ^ mask]))
                candidate_hash.update(suffix)
                if candidate_hash.digest() == target:
                    consider(original_offset, mask)
            prefix.update(view[offset:offset + 1])
    return list(found.values())


def _repair_records(lane_dir: Path) -> list[tuple[Path, dict]]:
    directory = safe_child(lane_dir, REPAIR_DIRECTORY)
    records = []
    for path in directory.glob("*.json"):
        if not re.fullmatch(r"repair-[1-9][0-9]*\.json", path.name):
            raise EvidenceError("invalid ledger repair record filename")
        path = safe_child(directory, path.name)
        records.append((path, read_sealed(path)))
    return sorted(records, key=lambda item: int(item[0].stem.split("-")[1]))


def verify_root_ledger_repairs(directory: Path, plan: dict, journals: dict[str, list[dict]]) -> list[dict]:
    """Verify every sealed record and its retained bytes against the journaled repair."""
    reports = []
    fields = {"kind", "protocol_id", "plan_hash", "lane_id", "lane_plan_hash", "corrupt_sha256",
              "restored_sha256", "byte_offset", "bit_mask", "original_byte", "restored_byte",
              "journal_reconstruction_hash", "journal", "reason", "approval_text", "recorded_utc", "seal_hash"}
    for lane in plan["lanes"]:
        lane_dir = safe_child(directory, lane["path"])
        records = _repair_records(lane_dir)
        journal = journals[lane["lane_id"]]
        entries = [entry for entry in journal if entry["kind"] == "ledger_repaired"]
        if len(records) != len(entries):
            raise EvidenceError("ledger repair records differ from the lane journal")
        seen = set()
        for (path, record), entry in zip(records, entries, strict=True):
            if (set(record) != fields or record["kind"] != REPAIR_KIND or record["protocol_id"] != PROTOCOL_ID
                    or record["plan_hash"] != plan["seal_hash"] or record["lane_id"] != lane["lane_id"]
                    or record["lane_plan_hash"] != lane["plan_hash"]):
                raise EvidenceError("ledger repair record identity or fields mismatch")
            relative = f"{REPAIR_DIRECTORY}/{path.name}"
            if entry["data"] != {"repair_record": relative, "repair_hash": record["seal_hash"],
                                 "corrupt_sha256": record["corrupt_sha256"],
                                 "restored_sha256": record["restored_sha256"]}:
                raise EvidenceError("ledger repair record differs from its journal entry")
            for field in ("corrupt_sha256", "restored_sha256", "journal_reconstruction_hash"):
                if type(record[field]) is not str or not re.fullmatch(r"[0-9a-f]{64}", record[field]):
                    raise EvidenceError("invalid ledger repair hash")
            if record["corrupt_sha256"] in seen:
                raise EvidenceError("a ledger repair repeats the same corrupt hash")
            seen.add(record["corrupt_sha256"])
            for field in ("reason", "approval_text", "recorded_utc"):
                if type(record[field]) is not str or not record[field].strip():
                    raise EvidenceError("ledger repair omits its reason, approval or timestamp")
            corrupt = safe_child(lane_dir, f"{REPAIR_DIRECTORY}/{record['corrupt_sha256']}.corrupt").read_bytes()
            if hashlib.sha256(corrupt).hexdigest() != record["corrupt_sha256"]:
                raise EvidenceError("retained corrupt ledger bytes differ from their repair hash")
            offset, mask = record["byte_offset"], record["bit_mask"]
            if (type(offset) is not int or not 0 <= offset < len(corrupt)
                    or type(mask) is not int or mask not in BIT_MASKS
                    or type(record["original_byte"]) is not int or type(record["restored_byte"]) is not int
                    or record["original_byte"] != corrupt[offset]
                    or record["restored_byte"] != corrupt[offset] ^ mask):
                raise EvidenceError("ledger repair does not record exactly one changed bit")
            restored = corrupt[:offset] + bytes([corrupt[offset] ^ mask]) + corrupt[offset + 1:]
            if hashlib.sha256(restored).hexdigest() != record["restored_sha256"]:
                raise EvidenceError("ledger repair restored bytes differ from their hash target")
            checkpoint = record["journal"]
            count = entry["sequence"]
            if canonical_json(checkpoint) != canonical_json({"count": count, "final_hash": entry["previous_hash"]}):
                raise EvidenceError("ledger repair journal checkpoint mismatch")
            lane_plan = read_sealed(lane_dir / PLAN_FILE)
            if lane_plan["seal_hash"] != lane["plan_hash"]:
                raise EvidenceError("ledger repair lane plan mismatch")
            marker = read_sealed(lane_dir / (LEDGER_FILE + ".identity.json"))
            validate_ledger_identity(marker, lane_plan["caps"], plan_hash=lane["plan_hash"])
            state = _decode_candidate(restored, marker, lane_plan["caps"], lane["plan_hash"])
            reconstruction = _check_attempts(state, journal[:count], lane_plan["caps"])
            if content_hash(reconstruction) != record["journal_reconstruction_hash"]:
                raise EvidenceError("ledger repair journal reconstruction hash mismatch")
            reports.append({"record": f"{lane['path']}/{relative}", **record})
    return reports


def repair_ledger(directory: Path, *, study_directory: Path, lane_id: str, reason: str,
                  approval_text: str) -> dict:
    """Repair only the unique canonical, sealed, journal-exact A4 ledger candidate."""
    from .live import COORDINATOR_LOCK, check_abandoned_root, read_live_plan, root_registration
    from .phase import INDEX_KIND, PLAN_KIND

    for description, text in (("reason", reason), ("approval text", approval_text)):
        if type(text) is not str or not text.strip():
            raise ValueError(f"a ledger repair needs nonempty {description}")
    directory = Path(directory)
    with ExitStack() as stack:
        stack.enter_context(_exclusive(directory / COORDINATOR_LOCK))
        plan = read_live_plan(directory)
        registration = root_registration(study_directory, plan, directory=directory, require_finalized=True)
        check_abandoned_root(directory, registration)
        lanes = [lane for lane in plan["lanes"] if lane["lane_id"] == lane_id]
        if len(lanes) != 1:
            raise ValueError("the repair must name exactly one lane in the sealed root")
        lane = lanes[0]
        lane_dir = safe_child(directory, lane["path"])
        stack.enter_context(_exclusive(lane_dir / LOCK_FILE))
        ledger_path = lane_dir / LEDGER_FILE
        stack.enter_context(_locked(ledger_path.with_suffix(".json.lock")))
        lane_plan = read_sealed(lane_dir / PLAN_FILE)
        index = read_sealed(lane_dir / INDEX_FILE)
        if (lane_plan.get("kind") != PLAN_KIND or lane_plan["seal_hash"] != lane["plan_hash"]
                or lane_plan["lane_id"] != lane_id or lane_plan["phase"] != plan["phase"]
                or lane_plan["maximum_live_calls"] != lane["planned_calls"]
                or index.get("kind") != INDEX_KIND or index["plan_hash"] != lane["plan_hash"]):
            raise EvidenceError("lane plan or index differs from the sealed root")
        journal = _Journal(lane_dir / JOURNAL_FILE, lane["plan_hash"], create=False)
        stack.callback(journal.close)
        checkpoint = index["journal"]
        if journal.hash_at(checkpoint["count"]) != checkpoint["final_hash"]:
            raise EvidenceError("lane journal differs from the retained index checkpoint")
        corrupt = ledger_path.read_bytes()
        try:
            check_seal(json.loads(corrupt.decode("utf-8")))
        except (ValueError, UnicodeError):
            pass
        else:
            raise EvidenceError("budget ledger already verifies its seal; repair refused")
        corrupt_hash = hashlib.sha256(corrupt).hexdigest()
        prior = _repair_records(lane_dir)
        if any(record.get("corrupt_sha256") == corrupt_hash for _, record in prior):
            raise EvidenceError("a repair record for the same corrupt hash already exists")
        verify_root_ledger_repairs(directory, {**plan, "lanes": [lane]}, {lane_id: journal.records})
        marker = read_sealed(ledger_path.with_suffix(".json.identity.json"))
        candidates = single_bit_candidates(corrupt, marker, lane_plan["caps"], plan_hash=lane["plan_hash"])
        if len(candidates) != 1:
            raise EvidenceError(f"ledger repair requires exactly one single-bit candidate; found {len(candidates)}")
        candidate = candidates[0]
        reconstruction = _check_attempts(candidate["state"], journal.records, lane_plan["caps"])
        restored_hash = hashlib.sha256(candidate["bytes"]).hexdigest()
        offset, mask = candidate["byte_offset"], candidate["bit_mask"]
        record = seal({
            "kind": REPAIR_KIND, "protocol_id": PROTOCOL_ID, "plan_hash": plan["seal_hash"],
            "lane_id": lane_id, "lane_plan_hash": lane["plan_hash"], "corrupt_sha256": corrupt_hash,
            "restored_sha256": restored_hash, "byte_offset": offset, "bit_mask": mask,
            "original_byte": corrupt[offset], "restored_byte": candidate["bytes"][offset],
            "journal_reconstruction_hash": content_hash(reconstruction),
            "journal": {"count": journal.count, "final_hash": journal.last_hash},
            "reason": reason, "approval_text": approval_text, "recorded_utc": datetime.now(timezone.utc).isoformat(),
        })
        repairs = safe_child(lane_dir, REPAIR_DIRECTORY)
        repairs.mkdir(exist_ok=True)
        retained = safe_child(repairs, f"{corrupt_hash}.corrupt")
        if retained.exists():
            if retained.read_bytes() != corrupt:
                raise EvidenceError("existing retained corrupt ledger bytes differ")
        else:
            with retained.open("xb") as stream:
                stream.write(corrupt)
                stream.flush()
                os.fsync(stream.fileno())
        number = max((int(path.stem.split("-")[1]) for path, _ in prior), default=0) + 1
        relative = f"{REPAIR_DIRECTORY}/repair-{number}.json"
        # Save the declaration before restoring the ledger. An interrupted repair
        # remains explicit evidence and cannot silently repeat the same corrupt hash.
        atomic_json(safe_child(lane_dir, relative), record)
        atomic_json(ledger_path, candidate["state"])
        journal.append("ledger_repaired", repair_record=relative, repair_hash=record["seal_hash"],
                       corrupt_sha256=corrupt_hash, restored_sha256=restored_hash)
    return {"directory": str(directory), "status": "repaired", "repair_record": f"{lane['path']}/{relative}",
            "repair": record, "live_model_calls": 0}
