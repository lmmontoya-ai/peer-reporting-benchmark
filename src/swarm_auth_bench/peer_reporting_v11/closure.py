"""Shared Level P closure selection and owner delivery policy."""

from collections.abc import Iterable

P_DELIVERY_POLICY = "closure_texts_only_to_task_owner_commentary_has_no_recipient"


def closure_outputs(outputs: Iterable[dict]) -> list[dict]:
    """Select final/phase-absent assistant items and finish summaries, complete or not."""
    return [output for output in outputs
            if output.get("source_kind") == "finish_summary"
            or output.get("source_kind") == "final_response"
            and output.get("phase") in (None, "final_answer")]


def record_delivery(outputs: list[dict], *, pressure: bool) -> None:
    selected = {id(output) for output in closure_outputs(outputs)}
    for output in outputs:
        output["recipient"] = "simulated_task_owner" if not pressure or id(output) in selected else None
        output["delivery_policy"] = (P_DELIVERY_POLICY if pressure else
                                     "all_visible_assistant_text_and_finish_summaries_to_task_owner")
