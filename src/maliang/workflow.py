"""Requirement-driven observation, checkpoints and before/after evidence."""

from __future__ import annotations

import json

from PIL import Image, ImageChops
from pydantic import Field

from .capabilities import Empty, image_result
from .guidance import runtime_capabilities, workflow_progress
from .models import Checkpoint, CreativeComponent, Evidence, Requirement, StrictModel, VideoPlan
from .runtime import Capability
from .store import atomic_json
from .verification import latest_export, verify


class PlanCreation(StrictModel):
    expected_revision: int = Field(ge=0)
    backend: str
    capability_assessment: str = Field(min_length=10, max_length=4000)
    plan: list[str] = Field(min_length=1)
    requirements: list[Requirement] = Field(min_length=1)
    checkpoints: list[Checkpoint] = Field(min_length=1)
    components: list[CreativeComponent] = Field(default_factory=list)
    video_plan: VideoPlan | None = None


class ObserveRequirement(StrictModel):
    requirement_id: str


class ObserveRequirements(StrictModel):
    requirement_ids: list[str] = Field(default_factory=list, max_length=64)


class CompleteCheckpoint(StrictModel):
    checkpoint_id: str


class Compare(StrictModel):
    before_evidence_id: str
    after_evidence_id: str
    preserve_region: list[int] | None = Field(default=None, min_length=4, max_length=4)


class FinishDraft(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)


def install(context):
    store, render, registry = context.store, context.renderers, context.registry

    def plan(
        expected_revision,
        backend,
        capability_assessment,
        plan,
        requirements,
        checkpoints,
        components,
        video_plan=None,
    ):
        if backend not in {b["id"] for b in render.available()}:
            raise ValueError("Choose an available and permitted backend")
        if store.load().spec.format == "mp4" and not render.backends[backend].supports_animation:
            raise ValueError("Selected backend cannot produce procedural video")
        cfg = context.image_generation
        if cfg and cfg.workflow == "code_directed":
            if not components or not any(c["route"] == "code" for c in components):
                raise ValueError(
                    "CODE_PLAN_REQUIRED: decompose elements and retain substantive code-created content"
                )
            generated = [c for c in components if c["route"] != "code"]
            from .generation_policy import validate_generation_plan

            validate_generation_plan(cfg, store.load().prompt, components)
            if generated and (backend != "scene2d" or "generate_asset" not in registry.capabilities):
                raise ValueError("Generated components require scene2d and an available image API")

        def change(data):
            existing = {r["id"]: r for r in data["requirements"]}
            for requirement in requirements:
                if requirement["id"] in existing:
                    if existing[requirement["id"]] != requirement:
                        raise ValueError("Cannot alter existing requirements; add new checks")
                else:
                    data["requirements"].append(requirement)
            covered = {r for c in checkpoints for r in c["requirement_ids"]}
            hard = {r["id"] for r in data["requirements"] if r["hard"] and not (data["spec"]["format"] == "mp4" and r.get("required_capability") == "audio_track")}
            if not hard <= covered:
                raise ValueError(
                    f"Checkpoints must cover all hard requirements, including: {sorted(hard - covered)}"
                )
            data.update(
                plan=plan,
                planned_backend=backend,
                capability_assessment=capability_assessment,
                checkpoints=checkpoints,
                components=components,
                video_plan=video_plan,
            )

        art = store.mutate(expected_revision, change)
        store.log(
            "visual_operation", {"action": "plan_creation", "revision": art.revision, "backend": backend}
        )
        return {"revision": art.revision, "workflow": workflow_progress(store)}

    def observe(requirement_id):
        art = store.load()
        req = next((r for r in art.requirements if r.id == requirement_id), None)
        if req is None:
            raise ValueError("Unknown requirement")
        if req.required_capability == "audio_track":
            raise ValueError(
                "Audio output is unavailable; visual observations cannot verify audio. Export the useful visual draft and call finish_draft."
            )
        final_id = latest_export(store) if art.program and art.program.backend == "pathtrace" else None
        cache_scope = {"pathtrace_export": final_id} if art.program and art.program.backend == "pathtrace" else {}
        cached = store.reusable_evidence("frames", requirement_id=requirement_id, **cache_scope)
        if cached:
            return image_result(store, cached)
        spec = req.observation
        count = max(spec.samples, 3 if req.kind == "temporal" else 1)
        end = spec.end if spec.end is not None else max(0, art.spec.duration - 1 / art.spec.fps)
        if count > 1 and end <= spec.start:
            raise ValueError("Observation interval needs at least two distinct times")
        times = (
            [spec.start]
            if count == 1
            else [spec.start + (end - spec.start) * i / (count - 1) for i in range(count)]
        )
        ev = store.evidence(final_id) if final_id else render.preview(times)
        paths, measured = [], []
        for i, path in enumerate(ev.paths):
            box = spec.region
            if spec.object_id:
                if spec.object_id not in {o.id for o in art.objects}:
                    raise ValueError("Observation object is not declared")
                if art.program.backend != "scene2d":
                    raise ValueError(
                        "Automatic object regions require scene2d; specify a pixel region for legacy backends"
                    )
                box = ev.metadata["frame_details"][i].get("object_bounds", {}).get(spec.object_id)
            measured.append(box)
            if box:
                paths.append(render.crop(ev.id, i, box).paths[0])
            else:
                paths.append(path)  # Blank/off-screen object: keep the full frame as failure evidence.
        result = Evidence(
            id=store.new_id(),
            revision=art.revision,
            kind="frames",
            paths=paths,
            timestamps=times,
            metadata={
                "requirement_id": requirement_id,
                **cache_scope,
                "frame_details": ev.metadata.get("frame_details", []),
                "parent": ev.id,
                "object_id": spec.object_id,
                "regions": measured,
                "note": "Object crop uses layer bounds before occlusion; full frame is available via parent. Samples cannot prove continuous motion.",
            },
        )
        store.add_evidence(result)
        store.log(
            "observation",
            {
                "requirement_id": requirement_id,
                "revision": art.revision,
                "evidence_id": result.id,
                "timestamps": times,
            },
        )
        return image_result(store, result)

    def observe_many(requirement_ids):
        ids = list(
            dict.fromkeys(
                requirement_ids or [r.id for r in store.load().requirements if not r.required_capability]
            )
        )
        requirements = {r.id: r for r in store.load().requirements}
        if not set(ids) <= requirements.keys():
            raise ValueError("Unknown requirement; no observations were executed")
        evidence, images, seen = [], [], set()
        for requirement_id in ids:
            result = observe(requirement_id)
            record = json.loads(result[0]["text"])
            evidence.append(
                {
                    "requirement_id": requirement_id,
                    "description": requirements[requirement_id].description,
                    "evidence": record,
                }
            )
            for block in result[1:]:
                key = json.dumps(block, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    images.append(block)
        return [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "revision": store.load().revision,
                        "observations": evidence,
                        "note": "Review each requirement separately using its mapped evidence ID. Identical image payloads shown once; all crops and temporal samples retained.",
                    },
                    ensure_ascii=False,
                ),
            },
            *images,
        ]

    def checkpoint(checkpoint_id):
        art = store.load()
        cp = next((c for c in art.checkpoints if c.id == checkpoint_id), None)
        if cp is None:
            raise ValueError("Unknown checkpoint")
        verdicts = {r["id"]: r["verdict"] for r in verify(store)["requirements"]}
        pending = [r for r in cp.requirement_ids if verdicts.get(r) not in {"pass", "ignored"}]
        if pending:
            return {"status": "needs_review", "requirements": pending, "revision": art.revision}
        record = {"checkpoint_id": cp.id, "revision": art.revision, "requirement_ids": cp.requirement_ids}
        atomic_json(store.path(f"checkpoints/{cp.id}.json"), record)
        store.log("checkpoint", record)
        return {"status": "passed", **record}

    def compare(before_evidence_id, after_evidence_id, preserve_region):
        before, after = store.evidence(before_evidence_id), store.evidence(after_evidence_id)
        if after.revision != store.load().revision or before.revision >= after.revision:
            raise ValueError("Compare an older revision against the current revision")
        if before.kind != "frames" or after.kind != "frames":
            raise ValueError("Compare matching full-frame previews")
        if (
            before.timestamps != after.timestamps
            or len(before.paths) != len(after.paths)
            or len(after.paths) > 6
        ):
            raise ValueError("Use identical timestamp lists with at most six frames")
        paths, metrics = [], []
        for old_path, new_path in zip(before.paths, after.paths):
            with Image.open(store.path(old_path)) as old, Image.open(store.path(new_path)) as new:
                if old.size != new.size or old.size != (store.load().spec.width, store.load().spec.height):
                    raise ValueError("Comparison requires matching full output dimensions")
                diff = ImageChops.difference(old.convert("RGB"), new.convert("RGB"))
                channels = diff.split()
                mask = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2]).point(
                    lambda v: 255 if v else 0
                )
                metric = {"changed_pixels": mask.histogram()[255], "changed_bounds": mask.getbbox()}
                if preserve_region:
                    left, top, right, bottom = preserve_region
                    if not (0 <= left < right <= old.width and 0 <= top < bottom <= old.height):
                        raise ValueError("Preserve region outside image")
                    metric["preserve_region_unchanged"] = mask.crop(preserve_region).getbbox() is None
                metrics.append(metric)
            paths.extend([old_path, new_path])
        result = Evidence(
            id=store.new_id(),
            revision=after.revision,
            kind="comparison",
            paths=paths,
            timestamps=after.timestamps,
            metadata={
                "before": before.id,
                "after": after.id,
                "order": "before,after per timestamp",
                "metrics": metrics,
                "note": "Pixel differences do not measure aesthetic improvement",
            },
        )
        store.add_evidence(result)
        return image_result(store, result)

    def finish(reason):
        from .verification import latest_export

        export_id = latest_export(store)
        report = verify(store, export_id)
        atomic_json(store.path("validation.json"), report)
        status = {
            "status": "draft",
            "revision": store.load().revision,
            "export_id": export_id,
            "reason": reason,
            "pending_requirements": [r["id"] for r in report["requirements"] if r["verdict"] not in {"pass", "ignored"}],
        }
        atomic_json(store.path("status.json"), status)
        store.log("run_draft", status)
        return status

    specs = [
        (
            "describe_environment",
            Empty,
            lambda: runtime_capabilities(context),
            "Explain configured, permitted and callable capabilities without credentials; video is procedural, not a generation API.",
            False,
        ),
        (
            "plan_creation",
            PlanCreation,
            plan,
            "Before drawing, assess task/capability fit, select backend, append concrete visual/temporal requirements and define checkpoints covering ALL hard requirements including brief_fulfillment. Existing requirements cannot be weakened.",
            True,
        ),
        (
            "observe_requirements",
            ObserveRequirements,
            observe_many,
            "Observe multiple requirements in one call (empty list means all). Returns each requirement's own evidence ID and every distinct image. Preserves object crops, regions and temporal sampling. Review each requirement individually after viewing these images.",
            False,
        ),
        (
            "observe_requirement",
            ObserveRequirement,
            observe,
            "Render requirement-specific full frames, pixel region or scene2d object crops. Temporal requirements receive at least three time samples. Returns image content and evidence ID.",
            False,
        ),
        (
            "complete_checkpoint",
            CompleteCheckpoint,
            checkpoint,
            "Confirm that all requirements of a checkpoint pass on the current revision. Edits invalidate checkpoints; re-review affected and preserved requirements.",
            False,
        ),
        (
            "compare_versions",
            Compare,
            compare,
            "Return paired before/after full previews and pixel change bounds; optionally verify unchanged pixels in a protected region. Does not judge artistic quality.",
            False,
        ),
        (
            "finish_draft",
            FinishDraft,
            finish,
            "Stop honestly with a specific capability/quality blocker and retain current export. Does not claim completion.",
            False,
        ),
    ]
    for name, schema, handler, description, mutates in specs:
        registry.register(Capability(name, description, schema, handler, "workflow", mutates))
