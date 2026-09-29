"""Concrete object operations for the retained scene2d backend."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .models import ArtworkObject, MotionTrack, StrictModel, Transform
from .runtime import Capability


class PutObject(StrictModel):
    expected_revision: int = Field(ge=0)
    object_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    source: str = Field(min_length=1, max_length=100000)
    properties: dict[str, Any] = Field(default_factory=dict)
    transform: Transform = Field(default_factory=Transform)
    layer: int = 0
    asset_ids: list[str] = Field(default_factory=list)
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=None, gt=0)


class EditObject(StrictModel):
    expected_revision: int = Field(ge=0)
    object_id: str
    transform: dict[str, float] = Field(default_factory=dict)
    properties: dict[str, Any] = Field(default_factory=dict)
    layer: int | None = None
    reason: str = Field(min_length=1)


class AnimateObject(StrictModel):
    expected_revision: int = Field(ge=0)
    object_id: str
    tracks: list[MotionTrack] = Field(max_length=6)
    reason: str = Field(min_length=1)


class ReadObject(StrictModel):
    object_id: str


def install(context):
    store = context.store

    def ensure_scene():
        art = store.load()
        if "scene2d" not in art.allowed_backends:
            raise ValueError("scene2d forbidden by this task")
        if art.program and art.program.backend != "scene2d":
            raise ValueError(
                "Existing artwork uses a different backend; explicitly write scene2d manifest first"
            )
        return art

    def find(data, object_id):
        for obj in data["objects"]:
            if obj["id"] == object_id:
                return obj
        raise ValueError(f"Unknown object: {object_id}")

    def put(expected_revision, object_id, source, properties, transform, layer, asset_ids, start, end):
        ensure_scene()
        cfg = context.image_generation
        if cfg and cfg.workflow in {"code_first_repair", "code_directed"} and asset_ids:
            for asset_id in asset_ids:
                asset = next((a for a in store.load().assets if a.id == asset_id), None)
                if asset and asset.provenance.get("source") in {
                    "qwen",
                    "openai_images",
                    "white_background_cutout",
                    "subject_cutout",
                }:
                    raise ValueError("LOCAL_REPAIR_REQUIRED: use place_cutout for generated subject assets")
        path, sha = store.blob(source.encode(), ".js")
        obj = ArtworkObject(
            id=object_id,
            properties=properties,
            transform=transform,
            layer=layer,
            asset_ids=asset_ids,
            start=start,
            end=end,
            draw={"backend": "canvas", "path": path, "sha256": sha},
        )
        manifest, manifest_sha = store.blob(b'{"version":1}', ".json")

        def change(data):
            existing = next((o for o in data["objects"] if o["id"] == object_id), None)
            value = obj.model_dump()
            if existing:
                value["motion"] = existing.get("motion", [])
            data["objects"] = [o for o in data["objects"] if o["id"] != object_id] + [value]
            data["program"] = {"backend": "scene2d", "path": manifest, "sha256": manifest_sha}

        art = store.mutate(expected_revision, change)
        store.log(
            "visual_operation", {"action": "put_object", "object_id": object_id, "revision": art.revision}
        )
        return {
            "revision": art.revision,
            "object_id": object_id,
            "next": "Observe affected requirements after the edit group",
        }

    def edit(expected_revision, object_id, transform, properties, layer, reason):
        ensure_scene()
        if not set(transform) <= set(Transform.model_fields):
            raise ValueError("Transform keys: x,y,scale_x,scale_y,rotation,opacity")

        def change(data):
            obj = find(data, object_id)
            if not obj.get("draw"):
                raise ValueError("Object has no drawing; use put_object")
            overridden = {t["property"] for t in obj.get("motion", [])} & set(transform)
            if overridden:
                raise ValueError(
                    f"Motion overrides these transforms: {sorted(overridden)}; edit the tracks instead"
                )
            obj["transform"].update(transform)
            obj["properties"].update(properties)
            if layer is not None:
                obj["layer"] = layer

        art = store.mutate(expected_revision, change)
        store.log(
            "visual_operation",
            {
                "action": "edit_object",
                "object_id": object_id,
                "revision": art.revision,
                "transform": transform,
                "property_keys": list(properties),
                "reason": reason,
            },
        )
        return {
            "revision": art.revision,
            "object_id": object_id,
            "effect": "Transforms applied by renderer. Custom appearance properties must be consumed by object code.",
        }

    def animate(expected_revision, object_id, tracks, reason):
        ensure_scene()

        def change(data):
            obj = find(data, object_id)
            if not obj.get("draw"):
                raise ValueError("Object has no drawing")
            obj["motion"] = tracks

        art = store.mutate(expected_revision, change)
        store.log(
            "visual_operation",
            {
                "action": "animate_object",
                "object_id": object_id,
                "revision": art.revision,
                "properties": [t["property"] for t in tracks],
                "reason": reason,
            },
        )
        return {
            "revision": art.revision,
            "object_id": object_id,
            "next": "Inspect endpoints and intervals; review temporal requirements",
        }

    def read(object_id):
        obj = find(store.load().model_dump(), object_id)
        if obj.get("draw"):
            obj["source"] = store.path(obj["draw"]["path"]).read_text()
        return obj

    for name, schema, handler, description, mutates in (
        (
            "put_object",
            PutObject,
            put,
            "Create/replace a scene2d object with custom function(ctx,t,object,assets,random). Coordinates are local pixels. Harness applies transforms/layers/motion. Preserve existing tracks.",
            True,
        ),
        (
            "edit_object",
            EditObject,
            edit,
            "Modify selected object transforms or properties without editing other objects. Supply reason. Keyframes take precedence; conflicting base edits are rejected.",
            True,
        ),
        (
            "animate_object",
            AnimateObject,
            animate,
            "Replace object's absolute-second transform keyframe tracks. Supports linear/smooth/step; [] clears motion. Render intervals before judging continuity.",
            True,
        ),
        (
            "read_object",
            ReadObject,
            read,
            "Read one object's bound source, properties and motion, avoiding the full scene code.",
            False,
        ),
    ):
        context.registry.register(Capability(name, description, schema, handler, "scene", mutates))
