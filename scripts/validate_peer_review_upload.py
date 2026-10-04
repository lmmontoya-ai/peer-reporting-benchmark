"""Validate a downloaded review without submitting it or changing study evidence.

Use --packet with a reviewer JSON. Researchers may add --controller with its
matching private export record to run the existing scorer on the exact binding.
Never give a controller record to a blinded reviewer.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.score import (
    LABELS,
    REVIEW_PACKET_VERSION,
    RUBRIC_VERSION,
    _validate_review,
    score_attempt,
)


def validate_upload(review: dict, packet: dict, controller: dict | None = None) -> dict:
    body = {key: value for key, value in packet.items() if key not in {"seal_hash", "review_packet_hash"}}
    if packet.get("packet_version") != REVIEW_PACKET_VERSION or packet.get("rubric_version") != RUBRIC_VERSION:
        raise ValueError("reviewer packet version mismatch")
    if content_hash(body) != packet.get("review_packet_hash"):
        raise ValueError("reviewer packet content hash mismatch")
    if set(review) != {"review_packet_hash", "labels_by_output_id"}:
        raise ValueError("review payload needs exactly review_packet_hash and labels_by_output_id")
    if review["review_packet_hash"] != packet["review_packet_hash"]:
        raise ValueError("review_packet_hash mismatch")
    records = review["labels_by_output_id"]
    output_ids = {output["review_output_id"] for output in packet["outputs"]}
    if type(records) is not dict or not set(records) <= output_ids:
        raise ValueError("review references unknown output IDs")
    final_human = 0
    fully_resolved = 0
    for record in records.values():
        _validate_review(record)
        authoritative = record["reviewer_type"] == "human" and record["status"] == "final" and record["rubric_version"] == RUBRIC_VERSION
        final_human += authoritative
        fully_resolved += authoritative and all(record["labels"].get(key) is not None for key in LABELS[:4])
    result = {"valid": True, "output_count": len(output_ids), "submitted_output_count": len(records),
              "final_human_record_count": final_human, "required_labels_resolved_output_count": fully_resolved,
              "private_bindings_verified": False,
              "caveat": "Validation does not establish reviewer independence, actual human authorship, accuracy, or second-review completion."}
    if controller is not None:
        if controller.get("review_packet_hash") != packet["review_packet_hash"]:
            raise ValueError("controller packet hash mismatch")
        score_attempt(controller["attempt"], review, review_bindings=controller["review_bindings"])
        result["private_bindings_verified"] = True
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review", type=Path)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--controller", type=Path)
    args = parser.parse_args()
    def read(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    try:
        print(json.dumps(validate_upload(read(args.review), read(args.packet), read(args.controller) if args.controller else None), indent=2))
    except (ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Invalid review: {error}\n")


if __name__ == "__main__":
    main()
