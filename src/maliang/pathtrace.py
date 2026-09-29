"""Validated, editable static scenes for the bundled WebGL path tracer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import StrictModel
from .runtime import Capability
from .store import digest

Number = Annotated[float, Field(allow_inf_nan=False, ge=-10000, le=10000)]
Positive = Annotated[float, Field(gt=0, le=10000, allow_inf_nan=False)]
Vec3 = tuple[Number, Number, Number]
Color = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")]


class Material(StrictModel):
    id: Identifier
    preset: Literal["ceramic", "metal", "glass", "plastic", "wood", "liquid", "matte"] = "ceramic"
    color: Color | None = None
    roughness: float | None = Field(default=None, ge=0, le=1)
    metalness: float | None = Field(default=None, ge=0, le=1)
    transmission: float | None = Field(default=None, ge=0, le=1)
    ior: float | None = Field(default=None, ge=1, le=2.5)
    clearcoat: float | None = Field(default=None, ge=0, le=1)
    attenuation_color: Color = "#ffffff"
    attenuation_distance: Positive = 1000
    texture_asset: Identifier | None = None
    texture_scale: tuple[Positive, Positive] = (1, 1)


class Geometry(StrictModel):
    type: Literal[
        "sphere", "box", "rounded_box", "plane", "cylinder", "torus", "lathe", "tube", "extrude", "mesh"
    ]
    size: tuple[Positive, Positive, Positive] = (1, 1, 1)
    radius: Positive = 0.5
    radius_top: float = Field(default=0.5, ge=0, le=10000)
    height: Positive = 1
    tube_radius: Positive = 0.08
    segments: int = Field(default=48, ge=8, le=128)
    radial_segments: int = Field(default=12, ge=3, le=64)
    points: list[Vec3] = Field(default_factory=list, max_length=512)
    profile: list[tuple[Number, Number]] = Field(default_factory=list, max_length=256)
    closed: bool = False
    bevel: float = Field(default=0.04, ge=0, le=1)
    vertices: list[Vec3] = Field(default_factory=list, max_length=20000)
    indices: list[int] = Field(default_factory=list, max_length=120000)

    @model_validator(mode="after")
    def shape(self):
        if self.type == "lathe" and (len(self.profile) < 2 or any(p[0] < 0 for p in self.profile)):
            raise ValueError(
                "lathe requires >=2 [radius>=0, y] profile points; include inner wall for vessels"
            )
        if self.type == "tube" and len(self.points) < 2:
            raise ValueError("tube requires >=2 xyz points")
        if self.type == "extrude" and len(self.profile) < 3:
            raise ValueError("extrude requires >=3 [x,y] contour points")
        if self.type == "mesh":
            if len(self.vertices) < 3 or not self.indices or len(self.indices) % 3:
                raise ValueError("mesh requires vertices and triangle indices")
            if min(self.indices) < 0 or max(self.indices) >= len(self.vertices):
                raise ValueError("mesh index out of range")
        if self.type == "rounded_box" and self.bevel > min(self.size) / 2:
            raise ValueError("rounded_box bevel exceeds half its smallest dimension")
        return self


class SceneObject(StrictModel):
    id: Identifier
    geometry: Geometry
    material: Identifier
    position: Vec3 = (0, 0, 0)
    rotation: Vec3 = (0, 0, 0)
    scale: tuple[Positive, Positive, Positive] = (1, 1, 1)
    visible: bool = True


class Camera(StrictModel):
    position: Vec3 = (4, 3, 5)
    target: Vec3 = (0, 0.6, 0)
    fov: float = Field(default=40, ge=10, le=100)

    @model_validator(mode="after")
    def direction(self):
        if sum((a - b) ** 2 for a, b in zip(self.position, self.target)) < 1e-8:
            raise ValueError("Camera position and target must differ")
        return self


class Light(StrictModel):
    id: Identifier
    color: Color = "#fff4df"
    intensity: float = Field(default=10, ge=0, le=1000)
    width: Positive = 3
    height: Positive = 3
    position: Vec3 = (-3, 5, 3)
    target: Vec3 = (0, 0, 0)

    @model_validator(mode="after")
    def direction(self):
        if self.position == self.target:
            raise ValueError("Light position and target must differ")
        return self


class Environment(StrictModel):
    top: Color = "#c3d4e5"
    bottom: Color = "#767060"
    intensity: float = Field(default=0.35, ge=0, le=10)
    background: Color = "#d8d2c7"


class RenderSettings(StrictModel):
    preview_mode: Literal["pathtrace", "raster"] = "pathtrace"
    preview_samples: int = Field(default=16, ge=1, le=128)
    final_samples: int = Field(default=128, ge=1, le=1024)
    bounces: int = Field(default=6, ge=1, le=16)
    exposure: float = Field(default=1, gt=0, le=5)

    @model_validator(mode="after")
    def samples(self):
        if self.final_samples < self.preview_samples:
            raise ValueError("final_samples must be >= preview_samples")
        return self


class PathtraceScene(StrictModel):
    version: Literal[1] = 1
    camera: Camera = Field(default_factory=Camera)
    environment: Environment = Field(default_factory=Environment)
    render: RenderSettings = Field(default_factory=RenderSettings)
    materials: list[Material] = Field(default_factory=lambda: [Material(id="ceramic")], max_length=64)
    objects: list[SceneObject] = Field(default_factory=list, max_length=256)
    lights: list[Light] = Field(default_factory=lambda: [Light(id="key")], max_length=8)

    @model_validator(mode="after")
    def references(self):
        for items in (self.objects, self.materials, self.lights):
            if len({item.id for item in items}) != len(items):
                raise ValueError("Duplicate scene item ID")
        materials = {item.id for item in self.materials}
        if any(obj.material not in materials for obj in self.objects):
            raise ValueError("Object refers to unknown material")
        # Bound total mesh construction cost, in addition to per-object limits.
        cost = sum(max(len(o.geometry.indices) // 3, o.geometry.segments**2 * 2) for o in self.objects)
        if cost > 1_000_000:
            raise ValueError("Scene geometry budget exceeds one million estimated triangles")
        return self


def parse_scene(source):
    if len(source.encode()) > 4_000_000:
        raise ValueError("Scene exceeds 4 MB")
    return PathtraceScene.model_validate_json(source)


def engine_status():
    folder = Path(__file__).parent / "vendor/pathtrace"
    available = (folder / "pathtracer.module.js").is_file() and (folder / "versions.json").is_file()
    return {
        "available": available,
        "gpu": "unverified",
        "versions": json.loads((folder / "versions.json").read_text()) if available else {},
        "message": "本地渲染库已就绪；首次渲染时检查 WebGL 2"
        if available
        else "缺少写实渲染库，请在 scripts/pathtrace 执行 npm ci && npm run build",
    }


class SetScene(StrictModel):
    expected_revision: int = Field(ge=0)
    scene: PathtraceScene


class EditScene(StrictModel):
    expected_revision: int = Field(ge=0)
    section: Literal["objects", "materials", "lights", "camera", "environment", "render"]
    item_id: str | None = None
    changes: dict = Field(default_factory=dict)
    remove: bool = False


def install(context):
    store = context.store
    if "pathtrace" not in store.load().allowed_backends:
        return

    def save(expected_revision, doc):
        if store.load().spec.format != "png":
            raise ValueError("Pathtrace supports static PNG only")
        source = PathtraceScene.model_validate(doc).model_dump_json()
        context.renderers.validate_program("pathtrace", source)
        path, sha = store.blob(source.encode(), ".json")
        art = store.mutate(
            expected_revision,
            lambda d: d.update(program={"backend": "pathtrace", "path": path, "sha256": sha}),
        )
        return {
            "revision": art.revision,
            "next": "Render and observe; export uses final_samples independently of preview.",
        }

    def set_scene(expected_revision, scene):
        return save(expected_revision, scene)

    def edit(expected_revision, section, item_id, changes, remove):
        art = store.load()
        if not art.program or art.program.backend != "pathtrace":
            raise ValueError("Use set_pathtrace_scene first")
        source = store.path(art.program.path).read_bytes()
        if digest(source) != art.program.sha256:
            raise ValueError("Scene integrity failed")
        doc = parse_scene(source.decode()).model_dump()
        if section in {"camera", "environment", "render"}:
            if remove or item_id is not None:
                raise ValueError("Singleton sections cannot be removed or have item_id")
            doc[section].update(changes)
        else:
            if not item_id:
                raise ValueError("item_id required")
            target = next((v for v in doc[section] if v["id"] == item_id), None)
            if remove:
                if target is None:
                    raise ValueError("Unknown item")
                doc[section].remove(target)
            else:
                if "id" in changes and changes["id"] != item_id:
                    raise ValueError("Cannot change item identity")
                if target is None:
                    doc[section].append({**changes, "id": item_id})
                else:
                    target.update(changes)
        return save(expected_revision, doc)

    context.registry.register(
        Capability(
            "set_pathtrace_scene",
            "Create or replace a structured physically based 3D scene. Plan backend=pathtrace first. Coordinates are world units, Y up, rotations in degrees. Geometry: primitive/rounded_box/lathe/tube/extrude/mesh. Materials use PBR presets. PNG only.",
            SetScene,
            set_scene,
            "creation",
            True,
        )
    )
    context.registry.register(
        Capability(
            "edit_pathtrace_scene",
            "Edit one scene section/item without rewriting everything. For objects/materials/lights supply item_id; changes shallow-merge (geometry replaces geometry), remove deletes. For camera/environment/render omit item_id. Schema is the same as set_pathtrace_scene. Edits are revisioned; restore_version undoes them.",
            EditScene,
            edit,
            "creation",
            True,
        )
    )
