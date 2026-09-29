"""Runtime capability disclosure and compact, evidence-based workflow guidance."""

from __future__ import annotations

import json
import os
from collections import deque


def runtime_capabilities(context):
    art = context.store.load()
    cfg = context.image_generation
    image_configured = bool(cfg and cfg.enabled and os.environ.get(cfg.api_key_env))
    image_callable = "generate_asset" in context.registry.capabilities
    paint_status = None
    if "paint" in art.allowed_backends:
        from .paint_native import native_status

        paint_status = native_status()
    from .pathtrace import engine_status

    return {
        "paint": paint_status,
        "pathtrace": engine_status() if "pathtrace" in art.allowed_backends else None,
        "creation_mode": "code_with_generated_assets" if art.allow_generated_assets else "code_only",
        "llm": {
            "api_key_configured": bool(os.environ.get("OPENAI_API_KEY")),
            "model_configured": bool(os.environ.get("MALIANG_MODEL")),
            "availability": "unverified",
        },
        "image_generation": {
            "adapter_implemented": True,
            "provider": cfg.provider if cfg else None,
            "model": cfg.model if cfg else None,
            "workflow_policy": cfg.workflow if cfg else None,
            "conservative_planning": cfg.conservative_planning if cfg else False,
            "max_generated_components": cfg.max_generated_components
            if cfg and cfg.conservative_planning
            else None,
            "max_repair_area_fraction": cfg.max_repair_area_fraction if cfg else None,
            "generation_size": cfg.size if cfg and cfg.provider == "qwen" else None,
            "prompt_extend": cfg.prompt_extend if cfg and cfg.provider == "qwen" else None,
            "asset_api_calls_remaining": max(
                0, context.meter.budget.max_asset_api_calls - context.meter.usage.get("asset_api_calls", 0)
            ),
            "workflow": (
                "plan control needs -> code layout -> planned environment plate / subject / texture -> generation+inspection -> place_background / extraction / texture composition -> align actors -> scene observation+review+delivery"
                if cfg and cfg.workflow == "code_directed"
                else "plan_creation -> code draft -> observe_requirement -> failed review -> local plan_asset -> "
                "generate_asset -> inspect_asset -> choose extraction -> inspect_asset -> "
                "preview_cutout -> place_cutout -> compare_versions -> final reviews"
                if cfg and cfg.workflow == "code_first_repair"
                else "plan_creation -> plan_asset -> generate_asset -> inspect_asset -> code composition -> final reviews"
            ),
            "limitations": "Text-to-image only; local masks require visual inspection; no articulated video generation",
            "configured": image_configured,
            "allowed_for_task": art.allow_generated_assets,
            "callable": image_callable,
            "availability": "unverified" if image_configured else "not_configured",
            "background_plates": bool(cfg and cfg.workflow == "code_directed"),
            "scope": "environment plates excluding code actors, plus local subjects/textures"
            if cfg and cfg.workflow == "code_directed"
            else "local visual repairs to a code-drawn scene"
            if cfg and cfg.workflow == "code_first_repair"
            else "imported image assets; code composes the final artwork",
        },
        "video": {
            "procedural_mp4": True,
            "video_generation_api_implemented": False,
            "audio_tracks_implemented": False,
            "method": "LLM writes object appearance; renderer samples program/keyframes and encodes MP4",
        },
        "backends": context.renderers.available(),
        "coordinates": "pathtrace: world units, Y-up, XYZ rotations in degrees; 2D: pixels, top-left origin, clockwise degrees, absolute seconds",
        "limitations": [
            "Local three.js backend supports procedural 3D; no external loaders or guaranteed photorealism.",
            "Object control is bound automatically only in scene2d. Legacy canvas/SVG source may ignore metadata.",
            "Model sees sampled images; these do not certify continuous video quality.",
            "Configured API credentials are not evidence of a successful provider request.",
        ],
    }


def workflow_progress(store):
    from .asset_workflow import asset_progress

    art = store.load()
    reviews = store.reviews()
    evidence_index = {}
    exports = []
    for path in store.root.glob("evidence/*.json"):
        item = json.loads(path.read_text())
        if item.get("revision") == art.revision and item.get("kind") == "export" and all(store.path(p).is_file() for p in item["paths"]):
            exports.append({"id": item["id"], "paths": item["paths"]})
        requirement_id = item.get("metadata", {}).get("requirement_id")
        if item.get("revision") == art.revision and item.get("kind") in {"frames", "crop"} and requirement_id:
            evidence_index.setdefault(requirement_id, []).append(
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    "timestamps": item.get("timestamps", []),
                }
            )
    observations = []
    for r in art.requirements:
        review = reviews.get(r.id)
        if (
            not r.required_capability
            and r.kind in {"visual", "style", "temporal"}
            and (review is None or review.verdict != "pass")
        ):
            observations.append(
                {
                    "requirement_id": r.id,
                    "verdict": review.verdict if review else "unreviewed",
                    "observation": r.observation.model_dump(),
                    "available_evidence": evidence_index.get(r.id, []),
                    "reason": review.explanation if review else "Needs current rendered evidence",
                }
            )
    steps = []
    for c in art.checkpoints:
        path = store.path(f"checkpoints/{c.id}.json")
        record = json.loads(path.read_text()) if path.exists() else {}
        steps.append(
            {
                "id": c.id,
                "description": c.description,
                "current": record.get("revision") == art.revision,
                "requirement_ids": c.requirement_ids,
            }
        )
    recent = deque(maxlen=6)
    trace = store.path("trace.jsonl")
    if trace.exists():
        with trace.open() as stream:
            for line in stream:
                event = json.loads(line)
                if event.get("event") in {"visual_operation", "observation", "checkpoint", "recovery"}:
                    recent.append(event)
    pending_batches = []
    for path in store.root.glob("batches/*.json"):
        batch = json.loads(path.read_text())
        if batch["status"] == "paused" and batch["revision"] == art.revision:
            pending_batches.append(
                {
                    "batch_id": batch["id"],
                    "failed_tool": batch["steps"][batch["next_index"]]["tool"],
                    "remaining_steps": len(batch["steps"]) - batch["next_index"],
                }
            )
    return {
        "policy": art.workflow_policy,
        "audio_policy": "ignore_audio_requests_deliver_silent_video" if art.spec.format == "mp4" else "unsupported",
        "planned": bool(art.capability_assessment and art.checkpoints),
        "checkpoint_status": steps,
        "pending_observations": observations,
        "capability_gaps": [
            {
                "requirement_id": r.id,
                "capability": r.required_capability,
                "next": "Export the useful visual draft, then finish_draft; do not retry unsupported work.",
            }
            for r in art.requirements
            if r.required_capability == "audio_track" and art.spec.format != "mp4"
        ],
        "recent_operations": list(recent),
        "resumable_batches": pending_batches,
        "current_exports": exports,
        "asset_workflow": asset_progress(store),
    }
