"""Sequential tool groups: one model decision, ordinary metered domain operations."""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field, ValidationError

from .models import StrictModel
from .runtime import BudgetExceeded, Capability
from .store import atomic_json
from .verification import latest_export


class Step(StrictModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ExecuteSteps(StrictModel):
    expected_revision: int = Field(ge=0)
    steps: list[Step] = Field(min_length=1, max_length=16)


class ResumeSteps(StrictModel):
    expected_revision: int = Field(ge=0)
    batch_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    replacement_args: dict[str, Any] = Field(default_factory=dict)


def install(context):
    registry, store = context.registry, context.store
    allowed = {
        "plan_creation",
        "put_object",
        "edit_object",
        "animate_object",
        "write_program",
        "init_painting",
        "set_pathtrace_scene",
        "edit_pathtrace_scene",
        "paint_strokes",
        "set_paint_layer",
        "remove_paint_strokes",
        "patch_program",
        "save_program",
        "observe_requirement",
        "observe_requirements",
        "record_review",
        "complete_checkpoint",
        "plan_asset",
        "generate_asset",
        "inspect_asset",
        "inspect_assets",
        "extract_subject",
        "extract_white_background",
        "preview_cutout",
        "place_cutout",
        "place_background",
        "compose_asset",
        "render_frames",
        "render_clip",
        "inspect_video",
        "export_artifact",
        "finalize_artwork",
        "finish_draft",
        "compare_versions",
    } & set(registry.capabilities)

    def execute(expected_revision, steps):
        if len(steps) > context.efficiency.max_batch_steps:
            raise ValueError("Batch exceeds efficiency.max_batch_steps")
        if store.load().revision != expected_revision:
            raise ValueError("STALE_STATE: read current revision before starting a group")
        if any(step["tool"] not in allowed for step in steps):
            raise ValueError("Unsupported grouped tool; use an ordinary domain tool")
        batch_id = store.new_id()
        batch_path = store.path(f"batches/{batch_id}.json")
        record = {
            "id": batch_id,
            "steps": steps,
            "next_index": 0,
            "revision": expected_revision,
            "status": "running",
        }
        atomic_json(batch_path, record)
        store.log("batch_saved", {"batch_id": batch_id, "steps": len(steps)})
        pending = None
        results, images, seen = [], [], set()
        for index, step in enumerate(steps):
            name, args = step["tool"], dict(step["args"])
            if "expected_revision" in registry.capabilities[name].schema.model_fields:
                args["expected_revision"] = store.load().revision
            if name == "finalize_artwork" and args.get("export_id") == "$latest_export":
                args["export_id"] = latest_export(store)
            if name == "inspect_video" and args.get("evidence_id") == "$latest_export":
                args["evidence_id"] = latest_export(store)
            try:
                result = registry.invoke(name, args)
            except BudgetExceeded:
                record.update(next_index=index, revision=store.load().revision, status="paused")
                atomic_json(batch_path, record)
                raise
            except ValidationError as exc:
                result = {"status": "error", "tool": name, "error": str(exc)}
            if isinstance(result, list):
                text_blocks = []
                for block in result:
                    if block.get("type") == "image_url":
                        identity = json.dumps(block, sort_keys=True)
                        if identity not in seen:
                            seen.add(identity)
                            images.append(block)
                    else:
                        text_blocks.append(block)
                results.append({"tool": name, "result": text_blocks})
            else:
                if (
                    isinstance(result, dict)
                    and "objects" in result
                    and "revision" in result
                    and name != "read_artwork"
                ):
                    result = {"revision": result["revision"]}
                results.append({"tool": name, "result": result})
                if isinstance(result, dict) and (
                    result.get("error")
                    or result.get("status")
                    in {"error", "needs_review", "needs_revision", "completed", "draft"}
                ):
                    if result.get("error") or result.get("status") not in {"completed", "draft"}:
                        pending = batch_id
                        record.update(next_index=index, revision=store.load().revision, status="paused")
                    else:
                        record.update(next_index=index + 1, revision=store.load().revision, status="finished")
                    atomic_json(batch_path, record)
                    break
            record.update(next_index=index + 1, revision=store.load().revision)
            atomic_json(batch_path, record)
        if pending is None:
            record.update(status="finished")
            atomic_json(batch_path, record)
        return [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "revision": store.load().revision,
                        "results": results,
                        "executed": len(results),
                        "requested": len(steps),
                        "resume_batch_id": pending,
                        "remaining_steps": len(steps) - record["next_index"] if pending else 0,
                        "note": "Sequential, not atomic. On error, use resume_steps with resume_batch_id and corrected replacement_args for the failed step. Unexecuted source code is saved; do not rewrite it. Earlier successful steps will not repeat.",
                    },
                    ensure_ascii=False,
                ),
            },
            *images,
        ]

    def resume(expected_revision, batch_id, replacement_args):
        path = store.path(f"batches/{batch_id}.json")
        if not path.is_file():
            raise ValueError("Unknown saved batch")
        record = json.loads(path.read_text())
        if record["status"] != "paused":
            raise ValueError("Batch is not paused or has already been resumed")
        if expected_revision != store.load().revision or record["revision"] != expected_revision:
            raise ValueError(
                "STALE_BATCH: scene changed since failure; replan before executing saved operations"
            )
        steps = record["steps"][record["next_index"] :]
        steps[0]["args"] = {**steps[0]["args"], **replacement_args}
        if len(steps) > context.efficiency.max_batch_steps:
            raise ValueError("Saved batch exceeds current efficiency.max_batch_steps")
        record["status"] = "resuming"
        atomic_json(path, record)
        result = execute(expected_revision, steps)
        record["status"] = "resumed"
        atomic_json(path, record)
        return result

    registry.register(
        Capability(
            "resume_steps",
            "Resume a paused execute_steps batch after correcting its failed operation. "
            "Pass batch_id from resume_batch_id, current expected_revision, and only changed top-level arguments "
            "in replacement_args. Reuses saved unexecuted JS verbatim; never repeats successful steps. "
            "Only use when the scene revision has not changed and the remaining decisions are still valid.",
            ResumeSteps,
            resume,
            "workflow",
        )
    )

    registry.register(
        Capability(
            "execute_steps",
            f"Execute up to {context.efficiency.max_batch_steps} already-planned domain operations sequentially in ONE model turn. "
            "Omit expected_revision in step args; the harness inserts each current revision. "
            "Group object writes, observations, extraction+inspection, or reviews+checkpoint+export+finalize. "
            'Use export_id="$latest_export" for finalization or evidence_id="$latest_export" for inspect_video. Stops at the first domain error or delivery result. '
            "Do not group decisions that require seeing an intermediate image first. Each child tool is metered. "
            "Supported tools: " + ", ".join(sorted(allowed)),
            ExecuteSteps,
            execute,
            "workflow",
        )
    )
