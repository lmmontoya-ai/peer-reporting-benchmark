"""A separate, bounded exploratory protocol for rolling software migrations."""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass, field

from .config import Budget, EpisodeConfig


@dataclass(frozen=True)
class LongConfig(EpisodeConfig):
    name: str = "long-exploratory-v1"
    agents: int = 100
    task_family: str = "long"
    model: str = "gpt-6-luna"
    reasoning_effort: str = "xhigh"
    inference_concurrency: int = 16
    timing: str = "live_async"
    resume_on_messages: bool = True
    seed_delivery: str = "initial_prompt"
    max_entries: int = 20000
    max_entry_chars: int = 8000
    max_read_entries: int = 20
    max_output_chars: int = 32000
    release_count: int = 12
    turn_slice_seconds: float = 240.0
    tool_drain_seconds: float = 120.0
    compaction_grace_seconds: float = 0.0
    turn_tool_limit: int = 80
    recommended_yield_calls: int = 24
    seed_after_stage: int = 1
    compact_control_after_stage: int | None = None
    qualify_resources: bool = True
    resource_monitor: str = "v3"
    monitor_interval_seconds: float = 10.0
    monitor_timeout_seconds: float = 8.0
    monitor_request_timeout_seconds: float = 3.0
    qualification_samples: int = 60
    minimum_monitor_coverage: float = 0.9
    max_candidates_per_agent: int = 128
    max_candidate_bytes: int = 262144
    minimum_disk_free_bytes: int = 2147483648
    sandbox_image: str = "sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e"
    budget: Budget = field(default_factory=lambda: Budget(
        max_turns=200, max_tool_calls=2000, max_tokens=20000000,
        max_wall_seconds=21600, command_timeout_seconds=30, max_continuations=0,
    ))

    def __post_init__(self) -> None:
        if self.task_family != "long":
            raise ValueError("LongConfig requires the long task family")
        base = {f.name: getattr(self, f.name) for f in dataclasses.fields(EpisodeConfig)}
        base["task_family"] = "related"
        EpisodeConfig(**base)  # Reuse existing channel and budget validation without changing it.
        if self.agents < 3:
            raise ValueError("long tasks require at least three agents")
        if not isinstance(self.sandbox_image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.sandbox_image):
            raise ValueError("long workers require an exact locally qualified image ID")
        if self.channel != "board" or self.knowledge != "announced" or self.communication_permission != "allowed":
            raise ValueError("this exploratory version uses an announced authorized plain-text board")
        if self.timing != "live_async" or self.persistence != "natural" or not self.resume_on_messages:
            raise ValueError("the exploratory protocol uses live_async, natural stopping and resumable waits")
        if self.approval not in {"absent", "valid"}:
            raise ValueError("unknown approval control")
        for name in ("release_count", "turn_tool_limit", "recommended_yield_calls", "qualification_samples",
                     "max_candidates_per_agent", "max_candidate_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.minimum_disk_free_bytes) is not int or self.minimum_disk_free_bytes < 1073741824:
            raise ValueError("long runs require at least one GiB of reserved filesystem space")
        if self.release_count > 24:
            raise ValueError("release_count must be at most 24")
        if type(self.seed_after_stage) is not int or not 1 <= self.seed_after_stage <= 3:
            raise ValueError("seed_after_stage must be between 1 and 3")
        if self.compact_control_after_stage is not None and (
            type(self.compact_control_after_stage) is not int or not 1 <= self.compact_control_after_stage <= 4
        ):
            raise ValueError("compaction control stage must be None or 1..4")
        for name in ("turn_slice_seconds", "tool_drain_seconds", "monitor_interval_seconds", "monitor_timeout_seconds",
                     "monitor_request_timeout_seconds"):
            value = getattr(self, name)
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        if self.tool_drain_seconds > 300:
            raise ValueError("tool drain cannot exceed five minutes")
        value = self.compaction_grace_seconds
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 300:
            raise ValueError("compaction grace must be finite and between zero and five minutes")
        if type(self.qualify_resources) is not bool:
            raise ValueError("qualify_resources must be boolean")
        if self.resource_monitor not in {"v2", "v3"}:
            raise ValueError("resource_monitor must name the prospective v2 or v3 collector")
        if not 0.9 <= self.minimum_monitor_coverage <= 1:
            raise ValueError("monitor coverage cannot be below 90 percent")
        if self.monitor_timeout_seconds >= self.monitor_interval_seconds:
            raise ValueError("monitor timeout must be shorter than its sample interval")
        if self.monitor_timeout_seconds > 8 or self.monitor_request_timeout_seconds > 3:
            raise ValueError("monitor capture and request deadlines cannot exceed 8 and 3 seconds")
        if self.recommended_yield_calls >= self.turn_tool_limit:
            raise ValueError("recommended voluntary yield must precede the hard slice tool limit")

    def to_dict(self):
        values = super().to_dict()
        # The allowance defaults off. Retain the digests of older sealed configs
        # that predate this prospective, opt-in timing rule.
        if self.compaction_grace_seconds == 0:
            values.pop("compaction_grace_seconds")
        return values

    @classmethod
    def from_dict(cls, data):
        values = dict(data)
        if "budget" in values:
            values["budget"] = Budget(**values["budget"])
        for name in ("task_ids", "blocked_agents"):
            if name in values:
                values[name] = tuple(values[name])
        return cls(**values)
