from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

from pydantic import BaseModel

from .models import Budget
from .store import ProjectStore, atomic_json


class BudgetExceeded(RuntimeError):
    pass


class Meter:
    def __init__(self, store: ProjectStore, budget: Budget, resume: bool = False):
        self.store, self.budget = store, budget
        path = store.path("usage.json")
        self.usage = (
            json.loads(path.read_text())
            if resume and path.exists()
            else {
                "model_calls": 0,
                "tool_calls": 0,
                "frames": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "elapsed_seconds": 0.0,
                "asset_api_calls": 0,
            }
        )
        self.started = time.monotonic()
        self.previous_seconds = self.usage["elapsed_seconds"]

    def flush(self):
        self.usage["elapsed_seconds"] = self.previous_seconds + time.monotonic() - self.started
        atomic_json(self.store.path("usage.json"), self.usage)

    def remaining_seconds(self):
        self.check()
        return self.budget.max_seconds - self.usage["elapsed_seconds"]

    def consume(self, field: str, amount: int = 1):
        self.check()
        limit = getattr(self.budget, f"max_{field}", None)
        if limit is not None and self.usage[field] + amount > limit:
            raise BudgetExceeded(f"Budget exhausted: {field}")
        self.usage[field] += amount
        self.flush()

    def check(self):
        self.flush()
        if self.usage["elapsed_seconds"] >= self.budget.max_seconds:
            raise BudgetExceeded("Wall-clock budget exhausted")
        for field in ("input_tokens", "output_tokens"):
            if self.usage[field] >= getattr(self.budget, f"max_{field}"):
                raise BudgetExceeded(f"Token budget exhausted: {field}")

    def record_tokens(self, usage: dict):
        for field in ("input_tokens", "output_tokens"):
            self.usage[field] += usage.get(field, 0)
        self.flush()


@dataclass
class Capability:
    name: str
    description: str
    schema: type[BaseModel]
    handler: Callable[..., Any]
    family: str
    mutates: bool = False


class Registry:
    """Core knows capability contracts, not canvas/SVG/provider implementations."""

    def __init__(self, store: ProjectStore, meter: Meter):
        self.store, self.meter = store, meter
        self.capabilities: dict[str, Capability] = {}

    def register(self, capability: Capability):
        if capability.name in self.capabilities:
            raise ValueError(f"Duplicate capability: {capability.name}")
        self.capabilities[capability.name] = capability

    def describe(self):
        return [
            {
                "name": c.name,
                "description": c.description,
                "family": c.family,
                "mutates": c.mutates,
                "input_schema": c.schema.model_json_schema(),
            }
            for c in self.capabilities.values()
        ]

    def invoke(self, name: str, arguments: dict):
        self.meter.consume("tool_calls")
        capability = self.capabilities[name]
        checked = capability.schema.model_validate(arguments)
        start = time.monotonic()
        self.store.log(
            "tool_start",
            {"tool": name, "arguments": checked.model_dump(), "revision": self.store.load().revision},
        )
        try:
            art = self.store.load()
            if (
                art.workflow_policy == "guided"
                and capability.mutates
                and name not in {"plan_creation", "add_requirements", "revise_edit_requirement"}
                and not art.capability_assessment
            ):
                raise ValueError("PLAN_REQUIRED: call describe_environment and plan_creation before editing")
            result = capability.handler(**checked.model_dump())
            self.store.log(
                "tool_end",
                {
                    "tool": name,
                    "status": "ok",
                    "seconds": time.monotonic() - start,
                    "result": self._trace_result(result),
                },
            )
            return result
        except BudgetExceeded:
            raise
        except Exception as exc:
            from .asset_workflow import recovery_hint

            hint = recovery_hint(self.store, name, arguments, str(exc), self.capabilities)
            self.store.log(
                "tool_end",
                {
                    "tool": name,
                    "status": "error",
                    "error": str(exc)[:2000],
                    **({"recovery": hint} if hint else {}),
                },
            )
            return {
                **({"recovery": hint} if hint else {}),
                "status": "error",
                "tool": name,
                "error": str(exc)[:2000],
                "current_revision": self.store.load().revision,
                "next_action": "Correct arguments using current authoritative state. Read more state only if information is missing; a failed batch can be resumed with saved code.",
            }

    @staticmethod
    def _trace_result(value):
        """Keep tool output visible without duplicating base64 image payloads in the trace."""
        if isinstance(value, dict):
            if value.get("type") == "image_url":
                return {"type": "image_url", "note": "Image bytes saved in evidence files"}
            return {key: Registry._trace_result(item) for key, item in value.items()}
        if isinstance(value, list):
            return [Registry._trace_result(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def langchain_tools(self):
        from langchain_core.tools import StructuredTool

        tools = []
        for capability in self.capabilities.values():

            def call(_name=capability.name, **kwargs):
                return self.invoke(_name, kwargs)

            tools.append(
                StructuredTool.from_function(
                    func=call,
                    name=capability.name,
                    description=capability.description,
                    args_schema=capability.schema,
                )
            )
        return tools

    def load_plugins(self, context):
        for plugin in entry_points(group="maliang.adapters"):
            plugin.load()(context, self)
