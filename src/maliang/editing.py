"""Version browsing and isolated, auditable code-edit sessions."""

from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4

from filelock import FileLock
from pydantic import Field

from .models import Artwork, Budget, Evidence, ObservationSpec, Requirement, StrictModel
from .store import ProjectStore, atomic_json, digest


def snapshot(store, revision):
    if type(revision) is not int or revision < 0:
        raise ValueError("版本号必须为非负整数")
    path = store.path(f"versions/{revision:06d}.json")
    if not path.is_file():
        raise ValueError("所选历史版本不存在")
    art = Artwork.model_validate_json(path.read_text())
    if art.revision != revision:
        raise ValueError("Version snapshot mismatch")
    return art


def copy_content(source, target, art):
    data = art.model_dump()
    for entry in [
        *data["assets"],
        *(o["draw"] for o in data["objects"] if o["draw"]),
        *([data["program"]] if data["program"] else []),
    ]:
        content = source.path(entry["path"]).read_bytes()
        if digest(content) != entry["sha256"]:
            raise ValueError("Source content integrity check failed")
        entry["path"], entry["sha256"] = target.blob(content, Path(entry["path"]).suffix)
    return data


def existing_media(store, revision):
    """Only full-composition evidence; object/region crops are not version thumbnails."""
    frames, videos = [], []
    for path in sorted(store.root.glob("evidence/*.json")):
        ev = Evidence.model_validate_json(path.read_text())
        if ev.revision != revision:
            continue
        meta = ev.metadata
        if ev.kind not in {"frames", "export", "clip"} or any(
            key in meta for key in ("requirement_id", "omitted_object_id", "box")
        ):
            continue
        for index, relative in enumerate(ev.paths):
            if not store.path(relative).is_file():
                continue
            item = {
                "path": relative,
                "time": ev.timestamps[index] if index < len(ev.timestamps) else None,
                "kind": ev.kind,
            }
            details = meta.get("frame_details", [])
            if index < len(details) and details[index].get("pathtrace"):
                item["pathtrace"] = {**details[index]["pathtrace"], "export_version": meta.get("image_export_version", 0)}
            if relative.endswith(".png"):
                frames.append(item)
            elif relative.endswith(".mp4"):
                videos.append(item)
    # Prefer complete exports over preview clips.
    videos.sort(key=lambda x: x["kind"] != "export")
    if any("pathtrace" in frame for frame in frames):
        frames.sort(key=lambda frame: (
            frame.get("pathtrace", {}).get("quality") == "final",
            frame.get("pathtrace", {}).get("export_version", 0),
            frame.get("pathtrace", {}).get("samples", 0),
        ), reverse=True)
    return videos[:1] + frames[:12]


def history_preview(store, revision):
    art = snapshot(store, revision)
    if not art.program:
        raise ValueError("该版本尚无绘制程序，可以从它开始编辑，但暂时无法预览")
    # Sample a fixed time grid for comparisons across revisions.
    folder = store.path(f".history/{revision:06d}")
    folder.mkdir(parents=True, exist_ok=True)
    with FileLock(str(folder / ".preview.lock"), timeout=0):
        manifest = folder / "preview.json"
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            if all(store.path(p["path"]).is_file() for p in saved["media"]):
                return saved
        cache = ProjectStore(folder)
        if not cache.path("artwork.json").exists():
            cache.create(Artwork.model_validate(copy_content(store, cache, art)))
        from .agent import make_context
        from .settings import ImageGenerationSettings

        ctx = make_context(
            folder,
            Budget(max_frames=12, max_seconds=300),
            plugins=False,
            image_generation=ImageGenerationSettings(enabled=False),
        )
        end = max(0, art.spec.duration - 1 / art.spec.fps)
        times = [0, end / 2, end] if art.spec.format == "mp4" else [0]
        evidence = ctx.renderers.preview(times)
        result = {
            "revision": revision,
            "media": [
                {"path": str(cache.path(p).relative_to(store.root)), "time": t, "kind": "frames"}
                for p, t in zip(evidence.paths, times)
            ],
        }
        atomic_json(manifest, result)
        return result


def version_index(store):
    # Scan evidence once, rather than once per version.
    evidence = {}
    for path in store.root.glob("evidence/*.json"):
        ev = json.loads(path.read_text())
        if ev.get("kind") not in {"frames", "export", "clip"} or any(
            key in ev.get("metadata", {}) for key in ("requirement_id", "omitted_object_id", "box")
        ):
            continue
        for p in ev.get("paths", []):
            if p.endswith((".png", ".mp4")) and store.path(p).is_file():
                evidence.setdefault(ev["revision"], []).append(p)
    versions = []
    previous = None
    for path in sorted(store.root.glob("versions/[0-9]*.json")):
        art = Artwork.model_validate_json(path.read_text())
        changes = []
        if previous:
            if art.program != previous.program:
                changes.append("修改绘制程序")
            old_objects = {o.id: o for o in previous.objects}
            new_objects = {o.id: o for o in art.objects}
            changed = [
                key
                for key in old_objects.keys() | new_objects.keys()
                if old_objects.get(key) != new_objects.get(key)
            ]
            if changed:
                changes.append("调整对象 " + ", ".join(sorted(changed)[:3]))
            if art.requirements != previous.requirements:
                changes.append("更新验收要求")
            if art.assets != previous.assets:
                changes.append("更新素材")
            if (
                art.plan != previous.plan
                or art.video_plan != previous.video_plan
                or art.components != previous.components
            ):
                changes.append("更新创作计划")
        summary = "；".join(changes) or ("初始作品" if previous is None else "更新作品状态")
        previous = art
        media = evidence.get(art.revision, [])
        manifest = store.path(f".history/{art.revision:06d}/preview.json")
        if manifest.exists():
            media = [
                p["path"]
                for p in json.loads(manifest.read_text())["media"]
                if store.path(p["path"]).is_file()
            ] + media
        thumb = next((p for p in media if p.endswith(".png")), None)
        versions.append(
            {
                "revision": art.revision,
                "created_at": path.stat().st_mtime,
                "summary": summary,
                "backend": art.program.backend if art.program else art.planned_backend,
                "objects": len(art.objects),
                "renderable": art.program is not None,
                "thumbnail": thumb,
                "has_video": any(p.endswith(".mp4") for p in media),
            }
        )
    return list(reversed(versions))


def create_edit(source, target, revision, instruction, *, preview=True):
    if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 20000:
        raise ValueError("请输入 1–20000 字的修改要求")
    original = snapshot(source, revision)
    if target.root == source.root or target.path("artwork.json").exists():
        raise ValueError("编辑必须使用新的任务目录")
    data = copy_content(source, target, original)
    request_id = f"edit_{uuid4().hex[:12]}"
    goal = Requirement(
        id=request_id,
        kind="temporal" if original.spec.format == "mp4" else "visual",
        observation=ObservationSpec(samples=3) if original.spec.format == "mp4" else ObservationSpec(),
        description="Apply the latest user edit in the effective brief; preserve unrelated composition, objects, style and motion. Compare against the source version and inspect the final result.",
    )
    data.update(
        revision=0,
        workflow_policy="guided",
        capability_assessment="",
        checkpoints=[],
        prompt=f"SOURCE BRIEF (historical context):\n{original.prompt}\n\nLATEST USER EDIT (takes precedence only where it changes the source brief):\n{instruction.strip()}\n\nPreserve all unrelated content. Edit the inherited code/objects/motion; reuse existing assets. Do not generate or semantically repaint bitmap assets. Output dimensions, duration and fps are unchanged.",
    )
    data["requirements"].append(goal.model_dump())
    record = {
        "source_project": str(source.root),
        "source_run": source.root.name,
        "source_revision": revision,
        "instruction": instruction.strip(),
        "created_at": time.time(),
        "source_prompt": original.prompt,
        "inherited_requirements": [r.model_dump() for r in original.requirements],
        "request_requirement_id": request_id,
        "baseline": [],
    }
    artwork = Artwork.model_validate(data)
    atomic_json(target.path("edit.json"), record)
    target.create(artwork)
    if preview and original.program:
        try:
            for item in history_preview(source, revision)["media"]:
                content = source.path(item["path"]).read_bytes()
                path, _ = target.blob(content, ".png")
                record["baseline"].append({"path": path, "time": item["time"]})
        except Exception as exc:
            record["baseline_error"] = str(exc)[:1000]
    atomic_json(target.path("edit.json"), record)
    target.log("edit_created", {k: record[k] for k in ("source_run", "source_revision", "instruction")})
    return target.load()


class ReviseRequirement(StrictModel):
    expected_revision: int = Field(ge=0)
    requirement_id: str
    description: str = Field(min_length=1, max_length=2000)
    instruction_quote: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2000)
    kind: str = Field(default="visual", pattern="^(visual|style|temporal)$")
    observation: ObservationSpec = Field(default_factory=ObservationSpec)


def install(context):
    store = context.store
    path = store.path("edit.json")
    if not path.exists():
        return
    record = json.loads(path.read_text())
    inherited = {r["id"] for r in record["inherited_requirements"]}
    from .capabilities import Empty
    from .runtime import Capability

    def revise(expected_revision, requirement_id, description, instruction_quote, reason, kind, observation):
        if (
            requirement_id not in inherited
            or not instruction_quote.strip()
            or instruction_quote.strip() not in record["instruction"]
        ):
            raise ValueError(
                "Only inherited requirements explicitly changed by a quote from the user edit may be revised"
            )
        before = next(r for r in store.load().requirements if r.id == requirement_id)
        replacement = Requirement(
            id=requirement_id,
            description=description,
            hard=before.hard,
            kind=kind,
            observation=observation,
            required_capability=before.required_capability,
        )

        def change(data):
            data["requirements"] = [
                replacement.model_dump() if r["id"] == requirement_id else r for r in data["requirements"]
            ]

        art = store.mutate(expected_revision, change)
        store.log(
            "edit_requirement_revised",
            {
                "revision": art.revision,
                "before": before.model_dump(),
                "after": replacement.model_dump(),
                "instruction_quote": instruction_quote,
                "reason": reason,
            },
        )
        return {"revision": art.revision, "requirement": replacement.model_dump()}

    def inspect_source():
        from .capabilities import image_result

        if not record["baseline"]:
            return {
                "source": record,
                "note": "No source preview available; inspect inherited code and render to diagnose.",
            }
        evidence = Evidence(
            id="edit-source",
            revision=0,
            kind="frames",
            paths=[p["path"] for p in record["baseline"]],
            timestamps=[p["time"] for p in record["baseline"]],
            metadata={"reference_only": True},
        )
        return image_result(store, evidence)

    context.registry.register(
        Capability(
            "revise_edit_requirement",
            "Only for inherited requirements conflicting with the latest user edit. Quote that edit, explain the conflict and preserve unaffected clauses. Never weaken unrelated requirements. The new edit acceptance requirement cannot be changed.",
            ReviseRequirement,
            revise,
            "editing",
            True,
        )
    )
    context.registry.register(
        Capability(
            "inspect_edit_source",
            "View source-version images for before/after comparison; reference only, not current-version acceptance evidence.",
            Empty,
            inspect_source,
            "editing",
        )
    )
