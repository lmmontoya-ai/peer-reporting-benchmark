"""The v1.1 modules that the live layer binds: tools, world, fixtures, and instructions.

The live layer never imports these modules at import time. ``load_bundle`` imports
them when a plan is built, verified, replayed, or run. Tests inject fake bundles.
``require_v11_tools`` refuses the P1 tool manifest and any other tool set, so a
live plan cannot bind the wrong schemas by accident.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from ..events import content_hash
from . import TOOL_SCHEMA_VERSION

TOOL_NAMES = ("read_channel", "send_message", "read_record", "report_incident", "submit_task", "agent_finish")
MAX_OPERATION_IDS = 32
READ_CHANNEL_MAX_LIMIT = 128


@dataclass(frozen=True, eq=False)
class ProtocolBundle:
    """Exact v1.1 interfaces from spec section 13.1, injected as one object."""

    schema_version: str
    tool_descriptors: list[dict[str, Any]]
    input_schemas: dict[str, Any]
    output_schemas: dict[str, Any]
    validate_input: Callable[[str, object], None]
    schema_error: type[Exception]
    world_factory: Callable[..., Any]
    audit_state: Callable[..., dict[str, Any]]
    load_template: Callable[[str], dict[str, Any]]
    build_fixture: Callable[..., dict[str, Any]]
    verify_fixture: Callable[[dict, dict], list[str]]
    build_instructions: Callable[..., str]

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(descriptor.get("name") for descriptor in self.tool_descriptors if type(descriptor) is dict)

    def tool_manifest(self) -> dict[str, Any]:
        return {"version": self.schema_version, "input_schemas": deepcopy(self.input_schemas),
                "output_schemas": deepcopy(self.output_schemas), "descriptors": deepcopy(self.tool_descriptors)}

    @property
    def tool_manifest_hash(self) -> str:
        return content_hash(self.tool_manifest())

    @property
    def tool_descriptors_hash(self) -> str:
        return content_hash(self.tool_descriptors)

    def wire_tool_specs(self) -> list[dict[str, Any]]:
        """Exactly the six frozen tools, in the app-server descriptor shape used by P1."""
        return [{"type": "function", "name": descriptor["name"], "description": descriptor["description"],
                 "inputSchema": deepcopy(descriptor["inputSchema"])} for descriptor in self.tool_descriptors]

    @property
    def wire_tool_specs_hash(self) -> str:
        return content_hash(self.wire_tool_specs())

    @property
    def attested_tools(self) -> list[str]:
        return sorted(f"function:{name}" for name in self.tool_names)


def require_v11_tools(bundle: ProtocolBundle) -> ProtocolBundle:
    """Refuse any tool set except the six v1.1 tools of spec section 7."""
    failures = []
    if bundle.schema_version != TOOL_SCHEMA_VERSION:
        failures.append(f"tool schema version {bundle.schema_version!r} is not {TOOL_SCHEMA_VERSION!r}")
    names = bundle.tool_names
    if len(names) != len(TOOL_NAMES) or set(names) != set(TOOL_NAMES):
        failures.append("tool descriptors are not exactly the six v1.1 tools")
    if set(bundle.input_schemas) != set(TOOL_NAMES) or set(bundle.output_schemas) != set(TOOL_NAMES):
        failures.append("input or output schemas do not cover exactly the six tools")
    for descriptor in bundle.tool_descriptors:
        name = descriptor.get("name") if type(descriptor) is dict else None
        if (name is None or descriptor.get("inputSchema") != bundle.input_schemas.get(name)
                or type(descriptor.get("description")) is not str):
            failures.append(f"descriptor {name!r} differs from its input schema")
    report = bundle.input_schemas.get("report_incident") or {}
    operation_ids = (report.get("properties") or {}).get("operation_ids") or {}
    if ("operation_ids" not in (report.get("required") or []) or operation_ids.get("type") != "array"
            or operation_ids.get("maxItems") != MAX_OPERATION_IDS or operation_ids.get("uniqueItems") is not True):
        failures.append("report_incident lacks the required unique operation_ids array of at most 32 IDs")
    limit = ((bundle.input_schemas.get("read_channel") or {}).get("properties") or {}).get("limit") or {}
    if limit.get("maximum") != READ_CHANNEL_MAX_LIMIT:
        failures.append("read_channel limit maximum is not 128")
    if failures:
        raise ValueError("v1.1 tool manifest binding failed: " + "; ".join(failures))
    return bundle


def load_bundle() -> ProtocolBundle:
    """Import the v1.1 schemas, world, templates, ladder, and prompts, then check the tools."""
    from . import incidents, ladder, prompts, schemas, world

    return require_v11_tools(ProtocolBundle(
        schema_version=schemas.SCHEMA_VERSION, tool_descriptors=schemas.TOOL_DESCRIPTORS,
        input_schemas=schemas.INPUT_SCHEMAS, output_schemas=schemas.OUTPUT_SCHEMAS,
        validate_input=schemas.validate_input, schema_error=schemas.V11SchemaError,
        world_factory=world.V11World, audit_state=world.audit_state,
        load_template=incidents.load_template, build_fixture=ladder.build_fixture,
        verify_fixture=ladder.verify_fixture, build_instructions=prompts.build_instructions,
    ))
