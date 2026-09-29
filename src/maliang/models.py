from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class OutputSpec(StrictModel):
    width: int = Field(default=640, ge=32, le=4096)
    height: int = Field(default=480, ge=32, le=4096)
    duration: float = Field(default=3, gt=0, le=120)
    fps: int = Field(default=12, ge=1, le=30)
    format: Literal["png", "mp4"] = "mp4"
    seed: int = 42

    @model_validator(mode="after")
    def limits(self):
        if self.format == "mp4" and max(self.width, self.height) > 1920:
            raise ValueError("MP4 dimensions must not exceed 1920 pixels per side")
        if self.format == "mp4" and (self.width % 2 or self.height % 2):
            raise ValueError("MP4 dimensions must be even")
        if self.duration * self.fps > 3600:
            raise ValueError("Maximum 3600 frames per export")
        return self


class ObservationSpec(StrictModel):
    object_id: str | None = None
    region: list[int] | None = Field(default=None, min_length=4, max_length=4)
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=None, gt=0)
    samples: int = Field(default=1, ge=1, le=12)

    @model_validator(mode="after")
    def valid(self):
        if self.end is not None and self.end <= self.start:
            raise ValueError("Observation end must be after start")
        if self.region and not (
            0 <= self.region[0] < self.region[2] and 0 <= self.region[1] < self.region[3]
        ):
            raise ValueError("Invalid observation rectangle")
        return self


class Requirement(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=2000)
    kind: Literal["visual", "style", "temporal", "object_exists", "event_order"] = "visual"
    hard: bool = True
    required_capability: Literal["audio_track"] | None = None
    targets: list[str] = Field(default_factory=list)
    observation: ObservationSpec = Field(default_factory=ObservationSpec)


class Event(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    description: str = ""
    object_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("Event end must be after start")
        return self


class Program(StrictModel):
    backend: str
    path: str
    sha256: str


class Transform(StrictModel):
    x: float = 0
    y: float = 0
    scale_x: float = Field(default=1, gt=0, le=100)
    scale_y: float = Field(default=1, gt=0, le=100)
    rotation: float = 0  # degrees clockwise in canvas coordinates
    opacity: float = Field(default=1, ge=0, le=1)


class Keyframe(StrictModel):
    time: float = Field(ge=0)
    value: float


class MotionTrack(StrictModel):
    property: Literal["x", "y", "scale_x", "scale_y", "rotation", "opacity"]
    interpolation: Literal["linear", "smooth", "step"] = "linear"
    keyframes: list[Keyframe] = Field(min_length=2, max_length=200)

    @model_validator(mode="after")
    def ordered(self):
        times = [k.time for k in self.keyframes]
        if any(a >= b for a, b in zip(times, times[1:])):
            raise ValueError("Keyframe times must be strictly increasing")
        for k in self.keyframes:
            if self.property == "opacity" and not 0 <= k.value <= 1:
                raise ValueError("Opacity keyframes must be in [0,1]")
            if self.property in {"scale_x", "scale_y"} and not 0 < k.value <= 100:
                raise ValueError("Scale keyframes must be in (0,100]")
        return self


class ArtworkObject(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    layer: int = 0
    properties: dict[str, Any] = Field(default_factory=dict)
    asset_ids: list[str] = Field(default_factory=list)
    draw: Program | None = None
    transform: Transform = Field(default_factory=Transform)
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=None, gt=0)
    motion: list[MotionTrack] = Field(default_factory=list)


class Checkpoint(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    description: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)


class Asset(StrictModel):
    id: str
    path: str
    sha256: str
    media_type: str
    provenance: dict[str, Any] = Field(default_factory=dict)


class BackgroundLayout(StrictModel):
    static_contents: list[str] = Field(min_length=1)
    excluded_object_ids: list[str] = Field(min_length=1)
    camera: str = Field(min_length=10)
    lighting: str = Field(min_length=10)
    reserved_regions: dict[str, list[int]] = Field(min_length=1)
    anchors: dict[str, list[float]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def coordinates(self):
        for box in self.reserved_regions.values():
            if len(box) != 4 or not (0 <= box[0] < box[2] and 0 <= box[1] < box[3]):
                raise ValueError("Reserved regions use [left, top, right, bottom] canvas pixels")
        for point in self.anchors.values():
            if len(point) != 2 or any(v < 0 for v in point):
                raise ValueError("Anchors use [x, y] canvas pixels")
        return self


class AssetTask(StrictModel):
    asset_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    role: Literal["subject", "background", "texture", "reference"]
    requirement_ids: list[str] = Field(min_length=1)
    visual_goal: str = Field(min_length=10, max_length=2000)
    generation_reason: str = Field(min_length=10, max_length=2000)
    prompt: str = Field(min_length=10, max_length=10000)
    code_composition: str = Field(min_length=10, max_length=2000)
    background_handling: str = Field(min_length=10, max_length=2000)
    temporal_plan: str = Field(min_length=10, max_length=2000)
    repair_evidence_id: str | None = None
    target_object_id: str | None = None
    target_region: list[int] | None = Field(default=None, min_length=4, max_length=4)
    extraction: Literal["none", "white_background"] = "none"
    background_layout: BackgroundLayout | None = None


class CreativeComponent(StrictModel):
    object_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    route: Literal["code", "generated_subject", "generated_texture", "generated_background"]
    reason: str = Field(min_length=10, max_length=2000)
    code_strategy: str = Field(min_length=10, max_length=2000)
    integration: str = Field(min_length=10, max_length=2000)
    requirement_ids: list[str] = Field(min_length=1)
    control_requirements: str = Field(default="", max_length=2000)
    brief_evidence: str = Field(default="", max_length=2000)
    code_limitation: str = Field(default="", max_length=2000)
    minimal_scope: str = Field(default="", max_length=2000)


class VideoShot(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    action: str = Field(min_length=1, max_length=2000)
    camera: str = Field(default="fixed", max_length=1000)


class VideoPlan(StrictModel):
    mode: Literal["procedural", "character", "image_composite", "mixed"]
    loop: bool = False
    continuity: str = Field(min_length=1, max_length=2000)
    shots: list[VideoShot] = Field(min_length=1, max_length=64)


class Artwork(StrictModel):
    revision: int = 0
    workflow_policy: Literal["legacy", "guided"] = "legacy"
    capability_assessment: str = ""
    planned_backend: str | None = None
    components: list[CreativeComponent] = Field(default_factory=list)
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    prompt: str
    spec: OutputSpec = Field(default_factory=OutputSpec)
    allowed_backends: list[str] = Field(default_factory=lambda: ["scene2d", "canvas", "svg"])
    allow_generated_assets: bool = False
    requirements: list[Requirement] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    video_plan: VideoPlan | None = None
    objects: list[ArtworkObject] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    assets: list[Asset] = Field(default_factory=list)
    asset_tasks: list[AssetTask] = Field(default_factory=list)
    program: Program | None = None

    @model_validator(mode="after")
    def references(self):
        if self.video_plan:
            if self.spec.format != "mp4":
                raise ValueError("Video plans require MP4 output")
            end = 0.0
            ids = set()
            for shot in self.video_plan.shots:
                if shot.id in ids or abs(shot.start - end) > 1e-6 or shot.end <= shot.start:
                    raise ValueError(
                        "Video shots need unique IDs and contiguous increasing intervals starting at zero"
                    )
                ids.add(shot.id)
                end = shot.end
            if abs(end - self.spec.duration) > 1e-6:
                raise ValueError("Video shots must cover the entire output duration")
        for values in (self.requirements, self.objects, self.events, self.assets, self.checkpoints):
            ids = [v.id for v in values]
            if len(ids) != len(set(ids)):
                raise ValueError("IDs must be unique within each collection")
        asset_ids = {a.id for a in self.assets}
        object_ids = {o.id for o in self.objects}
        requirements = {r.id for r in self.requirements}
        if len({c.object_id for c in self.components}) != len(self.components):
            raise ValueError("Component object IDs must be unique")
        if any(not set(c.requirement_ids) <= requirements for c in self.components):
            raise ValueError("Component references unknown requirements")
        if len({t.asset_id for t in self.asset_tasks}) != len(self.asset_tasks):
            raise ValueError("Asset task IDs must be unique")
        for task in self.asset_tasks:
            if not set(task.requirement_ids) <= requirements:
                raise ValueError("Asset task references unknown requirements")
        for checkpoint in self.checkpoints:
            if not set(checkpoint.requirement_ids) <= requirements:
                raise ValueError("Unknown checkpoint requirement")
        for req in self.requirements:
            obs = req.observation
            if obs.start > self.spec.duration or (obs.end is not None and obs.end > self.spec.duration):
                raise ValueError("Observation outside output duration")
            if obs.region and (obs.region[2] > self.spec.width or obs.region[3] > self.spec.height):
                raise ValueError("Observation outside output dimensions")
        if self.planned_backend and self.planned_backend not in self.allowed_backends:
            raise ValueError("Planned backend is forbidden")
        for obj in self.objects:
            if obj.start >= self.spec.duration or (
                obj.end is not None and not obj.start < obj.end <= self.spec.duration
            ):
                raise ValueError("Object lifetime outside output duration")
            if obj.draw and obj.draw.backend != "canvas":
                raise ValueError("scene2d object draw functions use the canvas contract")
            if len({t.property for t in obj.motion}) != len(obj.motion):
                raise ValueError("Only one motion track per property")
            if any(k.time > self.spec.duration for track in obj.motion for k in track.keyframes):
                raise ValueError("Keyframe exceeds duration")
            if not set(obj.asset_ids) <= asset_ids:
                raise ValueError(f"Unknown asset reference in {obj.id}")
        for event in self.events:
            if event.end > self.spec.duration:
                raise ValueError(f"Event {event.id} exceeds duration")
            if not set(event.object_ids) <= object_ids:
                raise ValueError(f"Unknown object reference in {event.id}")
        if self.program and self.program.backend not in self.allowed_backends:
            raise ValueError("Program backend is not permitted by the brief")
        return self


class Evidence(StrictModel):
    id: str
    revision: int
    kind: Literal["frames", "crop", "clip", "export", "comparison"]
    paths: list[str]
    timestamps: list[float] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Review(StrictModel):
    requirement_id: str
    revision: int
    evidence_ids: list[str] = Field(min_length=1)
    verdict: Literal["pass", "fail", "uncertain"]
    explanation: str = Field(min_length=1)
    source: Literal["model", "human"] = "model"


class Budget(StrictModel):
    max_asset_api_calls: int = Field(default=4, ge=0, le=100)
    max_model_calls: int = Field(default=24, ge=1, le=200)
    max_tool_calls: int = Field(default=80, ge=1, le=1000)
    max_frames: int = Field(default=2000, ge=1, le=20000)
    max_seconds: float = Field(default=1800, gt=0)
    max_input_tokens: int = Field(default=300000, ge=1)
    max_output_tokens: int = Field(default=60000, ge=1)
