"""A4's sealed, offline single-bit repair of budget-ledger.json only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from ..events import canonical_json, content_hash
from ..long_events import GENESIS_HASH, _iter_event_lines, iter_events
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
BINDING_KIND = "peer_reporting_v11_ledger_repair_binding"
BINDING_FIELDS = {"kind", "protocol_id", "study", "root", "lane_id", "plan_hash", "corrupt_sha256",
                  "restored_sha256", "byte_offset", "bit_mask", "identity_seal_hash", "journal", "commit",
                  "approval_text", "seal_hash"}
REPAIR_FIELDS = {"kind", "protocol_id", "plan_hash", "lane_id", "lane_plan_hash", "corrupt_sha256",
                 "restored_sha256", "byte_offset", "bit_mask", "original_byte", "restored_byte",
                 "journal_reconstruction_hash", "journal", "reason", "approval_text", "recorded_utc",
                 "binding_hash", "status", "seal_hash"}
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


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require_corrupt(raw: bytes) -> None:
    try:
        check_seal(json.loads(raw.decode("utf-8")))
    except (ValueError, UnicodeError):
        return
    raise EvidenceError("budget ledger already verifies its seal; repair refused")


def _unique_candidate(corrupt: bytes, marker: dict, caps: dict) -> dict:
    _require_corrupt(corrupt)
    candidates = single_bit_candidates(corrupt, marker, caps, plan_hash=marker["plan_hash"])
    if len(candidates) != 1:
        raise EvidenceError(f"ledger repair requires exactly one single-bit candidate; found {len(candidates)}")
    return candidates[0]


def _evidence_caps(corrupt: bytes, marker: dict) -> dict:
    """Recover the flat caps object against its independently sealed identity hash.

    A damaged caps key, brace, or value must not require the whole ledger to parse.
    Enumerate header and object variants, then let the exhaustive ledger search
    establish the unique bit. Caps validation still runs in that search.
    """
    header = b'"caps":{'
    headers = {header}
    for offset, byte in enumerate(header):
        for mask in BIT_MASKS:
            headers.add(header[:offset] + bytes([byte ^ mask]) + header[offset + 1:])
    for variant in headers:
        start = corrupt.find(variant)
        if start < 0:
            continue
        start += len(header) - 1
        # Either the closing brace or the next property's delimiter is intact.
        ends = {corrupt.find(b'}', start) + 1, corrupt.find(b',"identity_marker_hash"', start)}
        for end in sorted(ends):
            if end <= start:
                continue
            raw = corrupt[start:end]
            variants = [raw]
            variants.extend(raw[:offset] + bytes([byte ^ mask]) + raw[offset + 1:]
                            for offset, byte in enumerate(raw) for mask in BIT_MASKS)
            for value in variants:
                try:
                    caps = json.loads(value)
                    if type(caps) is dict and content_hash(caps) == marker["caps_hash"]:
                        return caps
                except (ValueError, UnicodeError):
                    continue
    raise EvidenceError("cannot recover caps matching the sealed ledger identity")


def _committed_evidence(directory: Path, commit: str) -> tuple[str, dict[str, bytes]]:
    """Read the named Git snapshot and reject dirty or uncommitted evidence."""
    def git(*args: str) -> bytes:
        result = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, check=False)
        if result.returncode:
            raise EvidenceError("ledger repair binding requires evidence in the named Git commit")
        return result.stdout

    repository = Path(os.fsdecode(git("rev-parse", "--show-toplevel").strip())).resolve()
    relative = directory.resolve().relative_to(repository).as_posix()
    resolved = git("rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}").decode().strip()
    evidence = {}
    for name in (LEDGER_FILE, LEDGER_FILE + ".identity.json", JOURNAL_FILE):
        path = safe_child(directory, name)
        raw = path.read_bytes()
        git_path = name if relative == "." else f"{relative}/{name}"
        if raw != git("show", f"{resolved}:{git_path}"):
            raise EvidenceError(f"binding evidence {name} differs from the named Git commit")
        evidence[name] = raw
    return resolved, evidence


def build_ledger_repair_binding(evidence_directory: Path, *, study: str, root: str, lane_id: str,
                                plan_hash: str, commit: str, approval_text: str) -> dict:
    """Build the approved offline binding from the exact committed incident bytes."""
    directory = Path(evidence_directory)
    resolved, evidence = _committed_evidence(directory, commit)
    marker = json.loads(evidence[LEDGER_FILE + ".identity.json"])
    check_seal(marker)
    corrupt = evidence[LEDGER_FILE]
    caps = _evidence_caps(corrupt, marker)
    candidate = _unique_candidate(corrupt, marker, caps)
    records = list(iter_events(safe_child(directory, JOURNAL_FILE)))
    if safe_child(directory, JOURNAL_FILE).read_bytes() != evidence[JOURNAL_FILE]:
        raise EvidenceError("binding journal changed while reading committed evidence")
    if not records or records[0]["run_id"] != marker["plan_hash"]:
        raise EvidenceError("binding journal belongs to another lane plan")
    _check_attempts(candidate["state"], records, caps)
    binding = seal({
        "kind": BINDING_KIND, "protocol_id": PROTOCOL_ID, "study": study, "root": root,
        "lane_id": lane_id, "plan_hash": plan_hash, "commit": resolved, "approval_text": approval_text,
        "corrupt_sha256": _sha256(corrupt), "restored_sha256": _sha256(candidate["bytes"]),
        "byte_offset": candidate["byte_offset"], "bit_mask": candidate["bit_mask"],
        "identity_seal_hash": marker["seal_hash"],
        "journal": {"count": len(records), "final_hash": records[-1]["hash"],
                    "sha256": _sha256(evidence[JOURNAL_FILE])},
    })
    _validate_binding(binding)
    return binding


def _validate_binding(binding: dict) -> None:
    check_seal(binding)
    if (set(binding) != BINDING_FIELDS or binding["kind"] != BINDING_KIND
            or binding["protocol_id"] != PROTOCOL_ID):
        raise EvidenceError("invalid ledger repair binding fields or kind")
    for field in ("study", "root", "lane_id"):
        _identifier(binding[field], f"binding {field}")
        if Path(binding[field]).name != binding[field] or "/" in binding[field] or "\\" in binding[field]:
            raise EvidenceError("binding study, root and lane must be names")
    for field in ("plan_hash", "corrupt_sha256", "restored_sha256", "identity_seal_hash"):
        if type(binding[field]) is not str or not re.fullmatch(r"[0-9a-f]{64}", binding[field]):
            raise EvidenceError(f"invalid binding {field}")
    checkpoint = binding["journal"]
    if (type(checkpoint) is not dict or set(checkpoint) != {"count", "final_hash", "sha256"}
            or type(checkpoint["count"]) is not int or checkpoint["count"] < 1
            or any(type(checkpoint[field]) is not str or not re.fullmatch(r"[0-9a-f]{64}", checkpoint[field])
                   for field in ("final_hash", "sha256"))):
        raise EvidenceError("invalid binding journal checkpoint")
    if (type(binding["byte_offset"]) is not int or binding["byte_offset"] < 0
            or type(binding["bit_mask"]) is not int or binding["bit_mask"] not in BIT_MASKS
            or type(binding["commit"]) is not str or not re.fullmatch(r"[0-9a-f]{40}", binding["commit"])
            or type(binding["approval_text"]) is not str or not binding["approval_text"].strip()):
        raise EvidenceError("invalid binding bit, commit or approval")


def _check_binding_context(binding: dict, directory: Path, plan: dict, lane: dict, marker: dict) -> None:
    _validate_binding(binding)
    expected = {"study": directory.resolve().parent.parent.name, "root": directory.resolve().name,
                "plan_hash": plan["seal_hash"], "lane_id": lane["lane_id"],
                "identity_seal_hash": marker["seal_hash"]}
    if any(binding[field] != value for field, value in expected.items()):
        raise EvidenceError("ledger repair binding differs from live root identity")


def _check_journal_binding(binding: dict, path: Path, records: list[dict], *, tail: list[dict] | None = None) -> None:
    checkpoint = binding["journal"]
    count = checkpoint["count"]
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    final_hash = records[count - 1]["hash"] if 0 < count <= len(records) else GENESIS_HASH
    if (count > len(records) or final_hash != checkpoint["final_hash"]
            or _sha256(b"".join(lines[:count])) != checkpoint["sha256"]):
        raise EvidenceError("lane journal differs from the approved binding checkpoint")
    if tail is not None and records[count:] != tail:
        raise EvidenceError("lane journal changed after the approved binding checkpoint")


def _repair_records(lane_dir: Path) -> list[tuple[Path, dict]]:
    directory = safe_child(lane_dir, REPAIR_DIRECTORY)
    records = []
    for path in directory.glob("*.json"):
        if re.fullmatch(r"binding-[0-9a-f]{64}\.json", path.name):
            continue
        if not re.fullmatch(r"repair-[1-9][0-9]*\.json", path.name):
            raise EvidenceError("invalid ledger repair record filename")
        records.append((safe_child(directory, path.name), read_sealed(path)))
    return sorted(records, key=lambda item: int(item[0].stem.split("-")[1]))


def _binding_path(lane_dir: Path, binding_hash: str) -> Path:
    if type(binding_hash) is not str or not re.fullmatch(r"[0-9a-f]{64}", binding_hash):
        raise EvidenceError("invalid ledger repair binding hash")
    return safe_child(lane_dir, f"{REPAIR_DIRECTORY}/binding-{binding_hash}.json")


def _completion(record: dict) -> dict:
    # The declaration bytes belong only to the prepared envelope. Excluding them
    # avoids a circular hash and preserves the completed record's existing format.
    return seal({**{key: value for key, value in record.items()
                   if key not in {"seal_hash", "declaration_bytes"}}, "status": "complete"})


def _declaration(relative: str, complete: dict) -> dict:
    return {"repair_record": relative, "repair_hash": complete["seal_hash"],
            "corrupt_sha256": complete["corrupt_sha256"], "restored_sha256": complete["restored_sha256"]}


def _prepared_declaration(record: dict, relative: str) -> tuple[bytes, dict]:
    """Decode the sealed exact line and check its declaration and hash-chain fields."""
    encoded = record.get("declaration_bytes")
    if type(encoded) is not str or not re.fullmatch(r"(?:[0-9a-f]{2})+", encoded):
        raise EvidenceError("prepared repair has invalid declaration bytes")
    raw = bytes.fromhex(encoded)
    try:
        event = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise EvidenceError("prepared repair has invalid declaration bytes") from error
    fields = {"schema_version", "run_id", "sequence", "kind", "agent_id", "wall_time",
              "elapsed_seconds", "logical_time", "data", "previous_hash", "hash"}
    expected = {"schema_version": 2, "run_id": record["lane_plan_hash"],
                "sequence": record["journal"]["count"], "kind": "ledger_repaired", "agent_id": None,
                "logical_time": None, "previous_hash": record["journal"]["final_hash"],
                "data": _declaration(relative, _completion(record))}
    if (type(event) is not dict or set(event) != fields
            or raw != (canonical_json(event) + "\n").encode("utf-8")
            or any(canonical_json(event[key]) != canonical_json(value) for key, value in expected.items())
            or event["hash"] != content_hash({key: value for key, value in event.items() if key != "hash"})):
        raise EvidenceError("prepared repair declaration differs from its record or checkpoint")
    return raw, event


def _prepared_journal(binding: dict, path: Path, intended: bytes) -> tuple[list[dict], int, bytes]:
    """Inspect the approved prefix without opening the full journal's strict reader."""
    count = binding["journal"]["count"]
    raw = path.read_bytes()
    parts = raw.split(b"\n", count)
    if len(parts) != count + 1:
        raise EvidenceError("lane journal differs from the approved binding checkpoint")
    prefix = b"\n".join(parts[:count]) + b"\n"
    if _sha256(prefix) != binding["journal"]["sha256"]:
        raise EvidenceError("lane journal differs from the approved binding checkpoint")
    tail = parts[count]
    if tail and tail != intended and (b"\n" in tail or not intended.startswith(tail)):
        raise EvidenceError("lane journal changed after the approved binding checkpoint")
    # Validate only the approved complete lines; the ordinary reader must still
    # reject an unterminated tail for every caller other than this repair rerun.
    records = list(_iter_event_lines(((line + b"\n").decode("utf-8") for line in parts[:count]),
                                    expected_count=count, expected_hash=binding["journal"]["final_hash"]))
    _check_journal_binding(binding, path, records)
    return records, len(prefix), tail


def _check_record(record: dict, binding: dict, plan: dict, lane: dict, corrupt: bytes,
                  candidate: dict, reconstruction: dict) -> None:
    check_seal(record)
    fields = REPAIR_FIELDS | {"declaration_bytes"} if record.get("status") == "prepared" else REPAIR_FIELDS
    if (set(record) != fields or record["kind"] != REPAIR_KIND
            or record["protocol_id"] != PROTOCOL_ID or record["status"] not in {"prepared", "complete"}):
        raise EvidenceError("ledger repair record identity or fields mismatch")
    expected = {"plan_hash": plan["seal_hash"], "lane_id": lane["lane_id"], "lane_plan_hash": lane["plan_hash"],
                "binding_hash": binding["seal_hash"], "approval_text": binding["approval_text"],
                "journal": binding["journal"], "journal_reconstruction_hash": content_hash(reconstruction),
                "corrupt_sha256": _sha256(corrupt), "restored_sha256": _sha256(candidate["bytes"]),
                "byte_offset": candidate["byte_offset"], "bit_mask": candidate["bit_mask"],
                "original_byte": corrupt[candidate["byte_offset"]],
                "restored_byte": candidate["bytes"][candidate["byte_offset"]]}
    if any(canonical_json(record[field]) != canonical_json(value) for field, value in expected.items()):
        raise EvidenceError("ledger repair record differs from binding, bytes or journal reconstruction")
    if any(type(record[field]) is not str or not record[field].strip() for field in ("reason", "recorded_utc")):
        raise EvidenceError("ledger repair omits its reason or timestamp")


def _bound_candidate(corrupt: bytes, binding: dict, marker: dict, caps: dict) -> dict:
    _require_corrupt(corrupt)
    if _sha256(corrupt) != binding["corrupt_sha256"]:
        raise EvidenceError("corrupt ledger bytes differ from approved binding")
    candidate = _unique_candidate(corrupt, marker, caps)
    for field, value in (("restored_sha256", _sha256(candidate["bytes"])),
                         ("byte_offset", candidate["byte_offset"]), ("bit_mask", candidate["bit_mask"])):
        if binding[field] != value:
            raise EvidenceError(f"ledger repair binding {field} differs from computed candidate")
    return candidate


def verify_root_ledger_repairs(directory: Path, plan: dict, journals: dict[str, list[dict]], *,
                               _pending_path: Path | None = None) -> list[dict]:
    """Check completed records, approved bindings, retained bytes and journal prefixes."""
    reports = []
    for lane in plan["lanes"]:
        lane_dir = safe_child(directory, lane["path"])
        records = [(path, record) for path, record in _repair_records(lane_dir) if path != _pending_path]
        journal = journals[lane["lane_id"]]
        entries = [entry for entry in journal if entry["kind"] == "ledger_repaired"]
        if any(record.get("status") != "complete" for _, record in records):
            raise EvidenceError("ledger repair is prepared but not completed")
        if len(records) != len(entries):
            raise EvidenceError("ledger repair records differ from the lane journal")
        seen = set()
        for (path, record), entry in zip(records, entries, strict=True):
            binding = read_sealed(_binding_path(lane_dir, record.get("binding_hash")))
            if binding["seal_hash"] != record["binding_hash"]:
                raise EvidenceError("retained ledger repair binding hash mismatch")
            lane_plan = read_sealed(lane_dir / PLAN_FILE)
            if lane_plan["seal_hash"] != lane["plan_hash"]:
                raise EvidenceError("ledger repair lane plan mismatch")
            marker = read_sealed(lane_dir / (LEDGER_FILE + ".identity.json"))
            _check_binding_context(binding, directory, plan, lane, marker)
            _check_journal_binding(binding, lane_dir / JOURNAL_FILE, journal)
            count = binding["journal"]["count"]
            if entry["sequence"] != count or entry["previous_hash"] != binding["journal"]["final_hash"]:
                raise EvidenceError("ledger repair journal checkpoint mismatch")
            corrupt = safe_child(lane_dir, f"{REPAIR_DIRECTORY}/{binding['corrupt_sha256']}.corrupt").read_bytes()
            candidate = _bound_candidate(corrupt, binding, marker, lane_plan["caps"])
            reconstruction = _check_attempts(candidate["state"], journal[:count], lane_plan["caps"])
            _check_record(record, binding, plan, lane, corrupt, candidate, reconstruction)
            relative = f"{REPAIR_DIRECTORY}/{path.name}"
            if entry["data"] != _declaration(relative, record):
                raise EvidenceError("ledger repair record differs from its journal entry")
            if record["corrupt_sha256"] in seen:
                raise EvidenceError("a ledger repair repeats the same corrupt hash")
            seen.add(record["corrupt_sha256"])
            reports.append({"record": f"{lane['path']}/{relative}", **record})
    return reports


def _retain_bytes(path: Path, raw: bytes) -> None:
    """Retain exact bytes atomically so interrupted writes can be retried."""
    if path.exists():
        if path.read_bytes() != raw:
            raise EvidenceError("existing retained corrupt ledger bytes differ")
        return
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def repair_ledger(directory: Path, *, study_directory: Path, lane_id: str, reason: str,
                  approval_text: str, binding_path: Path) -> dict:
    """Prepare or finish one approved repair under root, lane and ledger locks."""
    from .live import COORDINATOR_LOCK, check_abandoned_root, check_root_assignment_binding, read_live_plan, root_registration
    from .phase import INDEX_KIND, PLAN_KIND

    for description, text in (("reason", reason), ("approval text", approval_text)):
        if type(text) is not str or not text.strip():
            raise ValueError(f"a ledger repair needs nonempty {description}")
    binding = read_sealed(binding_path)
    _validate_binding(binding)
    if approval_text != binding["approval_text"]:
        raise EvidenceError("repair approval text differs from approved binding")
    directory = Path(directory)
    with ExitStack() as stack:
        stack.enter_context(_exclusive(directory / COORDINATOR_LOCK))
        plan = read_live_plan(directory)
        check_root_assignment_binding(directory, plan, study_directory)
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
        marker = read_sealed(ledger_path.with_suffix(".json.identity.json"))
        _check_binding_context(binding, directory, plan, lane, marker)
        prior = _repair_records(lane_dir)
        pending = [(path, record) for path, record in prior if record.get("status") == "prepared"]
        if len(pending) > 1:
            raise EvidenceError("multiple prepared ledger repairs")
        target = ledger_path.read_bytes()
        retained = safe_child(lane_dir, f"{REPAIR_DIRECTORY}/{binding['corrupt_sha256']}.corrupt")
        journal_path = lane_dir / JOURNAL_FILE
        journal = None
        if pending:
            path, record = pending[0]
            if record.get("binding_hash") != binding["seal_hash"] or record.get("reason") != reason:
                raise EvidenceError("prepared repair differs from the same binding or command")
            relative = f"{REPAIR_DIRECTORY}/{path.name}"
            intended, event = _prepared_declaration(record, relative)
            records, prefix_size, tail = _prepared_journal(binding, journal_path, intended)
            corrupt = retained.read_bytes() if retained.exists() else target
        else:
            if any(record.get("corrupt_sha256") == binding["corrupt_sha256"] for _, record in prior):
                raise EvidenceError("a repair record for the same corrupt hash already exists")
            journal = _Journal(journal_path, lane["plan_hash"], create=False)
            stack.callback(journal.close)
            records = journal.records
            _check_journal_binding(binding, journal_path, records, tail=[])
            verify_root_ledger_repairs(directory, {**plan, "lanes": [lane]}, {lane_id: records})
            corrupt = target
            tail = b""
        checkpoint = index["journal"]
        checkpoint_count = checkpoint["count"]
        if (type(checkpoint_count) is not int or not 0 <= checkpoint_count <= len(records)
                or (records[checkpoint_count - 1]["hash"] if checkpoint_count else GENESIS_HASH)
                != checkpoint["final_hash"]):
            raise EvidenceError("lane journal differs from the retained index checkpoint")
        if records and records[0]["run_id"] != lane["plan_hash"]:
            raise EvidenceError("phase journal belongs to another plan")
        candidate = _bound_candidate(corrupt, binding, marker, lane_plan["caps"])
        if target not in (corrupt, candidate["bytes"]):
            raise EvidenceError("repair target bytes differ from approved corrupt and restored bytes")
        count = binding["journal"]["count"]
        reconstruction = _check_attempts(candidate["state"], records[:count], lane_plan["caps"])
        if not pending:
            record = {
                "kind": REPAIR_KIND, "protocol_id": PROTOCOL_ID, "plan_hash": plan["seal_hash"],
                "lane_id": lane_id, "lane_plan_hash": lane["plan_hash"], "binding_hash": binding["seal_hash"],
                "corrupt_sha256": binding["corrupt_sha256"], "restored_sha256": binding["restored_sha256"],
                "byte_offset": candidate["byte_offset"], "bit_mask": candidate["bit_mask"],
                "original_byte": corrupt[candidate["byte_offset"]],
                "restored_byte": candidate["bytes"][candidate["byte_offset"]],
                "journal_reconstruction_hash": content_hash(reconstruction), "journal": binding["journal"],
                "reason": reason, "approval_text": approval_text, "recorded_utc": datetime.now(timezone.utc).isoformat(),
                "status": "prepared",
            }
            number = max((int(path.stem.split("-")[1]) for path, _ in prior), default=0) + 1
            path = safe_child(lane_dir, f"{REPAIR_DIRECTORY}/repair-{number}.json")
            relative = f"{REPAIR_DIRECTORY}/{path.name}"
            event = journal.prepare("ledger_repaired", **_declaration(relative, _completion(record)))
            intended = (canonical_json(event) + "\n").encode("utf-8")
            record = seal({**record, "declaration_bytes": intended.hex()})
        _check_record(record, binding, plan, lane, corrupt, candidate, reconstruction)
        complete = _completion(record)
        if pending:
            # Other historical repairs must remain intact while this one resumes.
            verify_root_ledger_repairs(directory, {**plan, "lanes": [lane]},
                                       {lane_id: records}, _pending_path=path)
        binding_copy = _binding_path(lane_dir, binding["seal_hash"])
        if binding_copy.exists() and read_sealed(binding_copy) != binding:
            raise EvidenceError("existing retained ledger repair binding differs")
        if tail and (not retained.exists() or target != candidate["bytes"]):
            raise EvidenceError("declared repair is missing its retained or restored bytes")
        if pending:
            # All binding, target and retained evidence checks precede this sole
            # permitted journal mutation, under the root, lane and ledger locks.
            if tail and tail != intended:
                with journal_path.open("r+b") as stream:
                    stream.truncate(prefix_size)
                    stream.flush()
                    os.fsync(stream.fileno())
            journal = _Journal(journal_path, lane["plan_hash"], create=False)
            stack.callback(journal.close)
        path.parent.mkdir(exist_ok=True)
        if not pending:
            atomic_json(path, record)
        if not binding_copy.exists():
            atomic_json(binding_copy, binding)
        _retain_bytes(retained, corrupt)
        if target == corrupt:
            atomic_json(ledger_path, candidate["state"])
        if tail != intended:
            journal.append_prepared(event)
        else:
            # A recovered full declaration may never have been synced. Make it durable before
            # completion; if the sync fails, the record stays prepared and a rerun retries.
            with journal_path.open("r+b") as stream:
                os.fsync(stream.fileno())
        atomic_json(path, complete)
    return {"directory": str(directory), "status": "repaired", "repair_record": f"{lane['path']}/{relative}",
            "repair": complete, "live_model_calls": 0}
