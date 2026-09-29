"""Versioned, replayable brush documents and domain tools (no generated code)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .models import StrictModel
from .runtime import Capability
from .store import digest

PRESETS = {
    "round": {"hardness": 0.85, "description": "实心圆笔，铺色与清晰边缘"},
    "soft": {"hardness": 0.15, "description": "柔边圆笔，薄涂与明暗过渡"},
    "ink": {"hardness": 1.0, "description": "硬边细笔，压力控制细节"},
    "smudge": {"hardness": 0.4, "description": "涂抹同一图层已有颜色；空白层不会取到下层颜色"},
    "eraser": {"hardness": 0.7, "description": "擦除当前图层，露出下层或纸色"},
}


class PaintPoint(StrictModel):
    x: float = Field(ge=-4096, le=8192)
    y: float = Field(ge=-4096, le=8192)
    pressure: float = Field(default=0.7, ge=0, le=1)
    xtilt: float = Field(default=0, ge=-1, le=1)
    ytilt: float = Field(default=0, ge=-1, le=1)
    dt: float = Field(default=0.016, ge=0.001, le=0.25)


class PaintStroke(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    brush: Literal["round", "soft", "ink", "smudge", "eraser"] = "round"
    color: str = Field(default="#202020", pattern=r"^#[0-9a-fA-F]{6}$")
    size: float = Field(default=20, ge=0.5, le=512, description="Brush diameter in native pixels")
    opacity: float = Field(default=1, ge=0, le=1)
    points: list[PaintPoint] = Field(min_length=2, max_length=2048)


class PaintLayer(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    opacity: float = Field(default=1, ge=0, le=1)
    visible: bool = True
    strokes: list[PaintStroke] = Field(default_factory=list, max_length=20000)


class PaintDocument(StrictModel):
    version: Literal[1] = 1
    engine: Literal["libmypaint-1.6"] = "libmypaint-1.6"
    background: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    layers: list[PaintLayer] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def limits(self):
        if len({layer.id for layer in self.layers}) != len(self.layers):
            raise ValueError("Duplicate paint layer ID")
        ids = [stroke.id for layer in self.layers for stroke in layer.strokes]
        if len(set(ids)) != len(ids):
            raise ValueError("Stroke IDs must be unique across the painting")
        if (
            len(ids) > 20000
            or sum(len(s.points) for layer_item in self.layers for s in layer_item.strokes) > 200000
        ):
            raise ValueError("Painting exceeds 20000 strokes / 200000 points")
        return self


def parse_document(source):
    if len(source.encode()) > 32_000_000:
        raise ValueError("Paint document exceeds 32 MB")
    return PaintDocument.model_validate_json(source)


class PaintInit(StrictModel):
    expected_revision: int = Field(ge=0)
    background: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    layer_id: str = Field(default="painting", pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


class PaintBatch(StrictModel):
    expected_revision: int = Field(ge=0)
    layer_id: str
    strokes: list[PaintStroke] = Field(min_length=1, max_length=256)


class PaintLayerEdit(StrictModel):
    expected_revision: int = Field(ge=0)
    layer_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    opacity: float = Field(default=1, ge=0, le=1)
    visible: bool = True


class PaintRemove(StrictModel):
    expected_revision: int = Field(ge=0)
    stroke_ids: list[str] = Field(min_length=1, max_length=256)


def install(context):
    store, registry = context.store, context.registry
    if "paint" not in store.load().allowed_backends:
        return

    def read():
        art = store.load()
        if not art.program or art.program.backend != "paint":
            raise ValueError("Use init_painting first")
        source = store.path(art.program.path).read_bytes()
        if digest(source) != art.program.sha256:
            raise ValueError("Paint source integrity failed")
        return parse_document(source.decode()).model_dump()

    def save(expected_revision, doc):
        source = PaintDocument.model_validate(doc).model_dump_json()
        context.renderers.validate_program("paint", source)
        path, sha = store.blob(source.encode(), ".json")
        art = store.mutate(
            expected_revision, lambda d: d.update(program={"backend": "paint", "path": path, "sha256": sha})
        )
        return {
            "revision": art.revision,
            "layers": len(doc["layers"]),
            "strokes": sum(len(layer_item["strokes"]) for layer_item in doc["layers"]),
            "next": "Render/observe current painting; restore_version can undo this batch.",
        }

    def init(expected_revision, background, layer_id):
        if store.load().program:
            raise ValueError("Painting already has a program; edit it or restore an earlier version")
        from .paint_native import require_library

        require_library()
        return save(
            expected_revision,
            PaintDocument(background=background, layers=[PaintLayer(id=layer_id)]).model_dump(),
        )

    def strokes(expected_revision, layer_id, strokes):
        doc = read()
        layer = next((layer_item for layer_item in doc["layers"] if layer_item["id"] == layer_id), None)
        if layer is None:
            raise ValueError("Unknown layer; create it with set_paint_layer")
        layer["strokes"].extend(strokes)
        return save(expected_revision, doc)

    def layer(expected_revision, layer_id, opacity, visible):
        doc = read()
        target = next((layer_item for layer_item in doc["layers"] if layer_item["id"] == layer_id), None)
        if target is None:
            target = PaintLayer(id=layer_id).model_dump()
            doc["layers"].append(target)
        target.update(opacity=opacity, visible=visible)
        return save(expected_revision, doc)

    def remove(expected_revision, stroke_ids):
        doc = read()
        known = {s["id"] for layer_item in doc["layers"] for s in layer_item["strokes"]}
        if not set(stroke_ids) <= known:
            raise ValueError("Unknown stroke ID")
        for layer_item in doc["layers"]:
            layer_item["strokes"] = [s for s in layer_item["strokes"] if s["id"] not in stroke_ids]
        return save(expected_revision, doc)

    for name, description, schema, fn in [
        (
            "init_painting",
            "Create a blank libmypaint painting. No image generation. Plan backend=paint first.",
            PaintInit,
            init,
        ),
        (
            "paint_strokes",
            "Append up to 256 native brush strokes. Coordinates/size are pixels; pressure 0..1. "
            "Brushes: round (fill), soft (shading), ink (details), smudge (same layer), eraser. "
            "Use unique IDs, gradual pressure, and enough points for curved contours. Observe after major batches.",
            PaintBatch,
            strokes,
        ),
        (
            "set_paint_layer",
            "Create a transparent top layer or change visibility/opacity of an existing layer.",
            PaintLayerEdit,
            layer,
        ),
        (
            "remove_paint_strokes",
            "Remove strokes by ID and replay the remaining painting; later smudges may change.",
            PaintRemove,
            remove,
        ),
    ]:
        registry.register(Capability(name, description, schema, fn, "creation", True))
