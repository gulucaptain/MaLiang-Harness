from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from pydantic import Field

from .adapters.renderers import RenderService
from .models import ArtworkObject, Event, Evidence, Requirement, Review, StrictModel
from .runtime import Capability, Meter, Registry
from .store import ProjectStore, atomic_json
from .verification import latest_export, verify


@dataclass
class Context:
    store: ProjectStore
    meter: Meter
    renderers: RenderService
    registry: Registry
    mode: str = "maliang"
    image_generation: object = None
    efficiency: object = None


class Empty(StrictModel):
    pass


class Update(StrictModel):
    expected_revision: int = Field(ge=0)
    plan: list[str]
    objects: list[ArtworkObject]
    events: list[Event]


class AddRequirements(StrictModel):
    expected_revision: int = Field(ge=0)
    requirements: list[Requirement] = Field(min_length=1)


class SaveProgram(StrictModel):
    expected_revision: int = Field(ge=0)
    backend: str
    source: str = Field(min_length=1, max_length=250000)


class PatchProgram(StrictModel):
    expected_revision: int = Field(ge=0)
    old_text: str = Field(min_length=1)
    new_text: str


class RenderFrames(StrictModel):
    timestamps: list[float] = Field(min_length=1, max_length=12)


class Clip(StrictModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)


class InspectVideo(StrictModel):
    evidence_id: str
    samples: int = Field(default=6, ge=1, le=12)


class Crop(StrictModel):
    evidence_id: str
    frame_index: int = Field(ge=0)
    box: list[int] = Field(min_length=4, max_length=4)


class Inspect(StrictModel):
    evidence_id: str


class RecordReview(StrictModel):
    requirement_id: str
    evidence_ids: list[str] = Field(min_length=1)
    verdict: str = Field(pattern="^(pass|fail|uncertain)$")
    explanation: str = Field(min_length=1)


class Restore(StrictModel):
    expected_revision: int = Field(ge=0)
    revision: int = Field(ge=0)


class Finalize(StrictModel):
    export_id: str


def image_result(store: ProjectStore, evidence: Evidence):
    """LangChain tool message content includes actual images, not inaccessible local paths."""
    content = [{"type": "text", "text": json.dumps(evidence.model_dump(), ensure_ascii=False)}]
    for path in evidence.paths[:12]:
        if path.endswith(".png"):
            data = base64.b64encode(store.path(path).read_bytes()).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}})
    return content


def install(context: Context):
    store, registry, render = context.store, context.registry, context.renderers

    def register(name, description, schema, handler, family="artwork", mutates=False):
        registry.register(Capability(name, description, schema, handler, family, mutates))

    def read_artwork():
        result = store.load().model_dump()
        if store.load().program:
            result["source"] = store.path(store.load().program.path).read_text()
        result["render_backends"] = render.available()
        if context.mode == "maliang":
            result["validation"] = verify(store)
        return result

    def update(expected_revision, plan, objects, events):
        def change(data):
            data.update(plan=plan, objects=objects, events=events)

        art = store.mutate(expected_revision, change)
        return {"revision": art.revision, "objects": [o.id for o in art.objects], "plan": art.plan}

    def add_requirements(expected_revision, requirements):
        # Append only: existing user requirements cannot be weakened or deleted by the agent.
        return store.mutate(
            expected_revision, lambda data: data["requirements"].extend(requirements)
        ).model_dump()

    def save(expected_revision, backend, source):
        render.validate_program(backend, source)
        path, sha = store.blob(source.encode(), render.backends[backend].suffix)
        art = store.mutate(
            expected_revision,
            lambda data: data.update(program={"backend": backend, "path": path, "sha256": sha}),
        )
        return {
            "revision": art.revision,
            "program": art.program.model_dump(),
            "next": "Render current state to inspect the effect",
        }

    def patch(expected_revision, old_text, new_text):
        art = store.load()
        if not art.program:
            raise ValueError("No program")
        source = store.path(art.program.path).read_text()
        if source.count(old_text) != 1:
            raise ValueError("Patch must match exactly once")
        return save(expected_revision, art.program.backend, source.replace(old_text, new_text, 1))

    def frames(timestamps):
        return image_result(store, render.preview(timestamps))

    def clip(start, end):
        ev = render.video(start, end)
        # Portable image feedback: ordered contact frames, not an unsupported raw-video model input.
        count = min(8, max(2, round((end - start) * 2)))
        preview = render.inspect_video(ev.id, count)
        content = image_result(store, preview)
        content.insert(
            0,
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "clip": ev.model_dump(),
                        "limitation": "Ordered samples do not prove continuous motion; inspect denser intervals as needed.",
                    }
                ),
            },
        )
        return content

    def crop(evidence_id, frame_index, box):
        return image_result(store, render.crop(evidence_id, frame_index, box))

    def record(requirement_id, evidence_ids, verdict, explanation):
        review = Review(
            requirement_id=requirement_id,
            revision=store.load().revision,
            evidence_ids=evidence_ids,
            verdict=verdict,
            explanation=explanation,
            source="model",
        )
        store.add_review(review)
        return {"recorded": review.model_dump(), "independent_quality_assessment": False}

    def finalize(export_id):
        report = verify(store, export_id)
        atomic_json(store.path("validation.json"), report)
        if not report["eligible_to_finalize"]:
            return {"status": "needs_revision", "validation": report}
        store.publish_video(export_id)
        status = {
            "status": "completed",
            "revision": store.load().revision,
            "export_id": export_id,
            "quality_basis": "Programmatic checks + explicitly attributed reviews",
        }
        atomic_json(store.path("status.json"), status)
        return status

    register(
        "list_capabilities",
        "Discover registered tools and backend-specific drawing contracts.",
        Empty,
        lambda: {
            "tools": [{k: v for k, v in item.items() if k != "input_schema"} for item in registry.describe()],
            "render_backends": render.available(),
        },
        "discovery",
    )
    register(
        "read_artwork",
        "Read current revision, source, objects, assets, timeline and open requirements.",
        Empty,
        read_artwork,
    )
    register(
        "update_artwork",
        "Replace plan, objects and timeline. Legacy source must explicitly read state; use scene2d object tools for bound control.",
        Update,
        update,
        mutates=True,
    )
    register(
        "add_requirements",
        "Append concrete checks derived from the user's brief; never duplicate or weaken existing checks.",
        AddRequirements,
        add_requirements,
        mutates=True,
    )
    register(
        "write_program",
        "Save source for a registered backend. Read its contract using list_capabilities first.",
        SaveProgram,
        save,
        "creation",
        True,
    )
    register(
        "patch_program",
        "Apply a unique exact-text replacement to the current program.",
        PatchProgram,
        patch,
        "creation",
        True,
    )
    register(
        "render_frames",
        "Render specified seconds and return actual image content to the model.",
        RenderFrames,
        frames,
        "observation",
    )
    register(
        "render_clip",
        "Render an MP4 interval and ordered visual samples for motion inspection.",
        Clip,
        clip,
        "observation",
    )
    register(
        "inspect_video",
        "Decode an existing MP4 export/clip into ordered images for model inspection, without rerendering.",
        InspectVideo,
        lambda evidence_id, samples: image_result(store, render.inspect_video(evidence_id, samples)),
        "observation",
    )
    register(
        "export_artifact",
        "Export current artwork at the user's full output specification; does not declare success.",
        Empty,
        lambda: render.export().model_dump(),
        "delivery",
    )
    if context.mode == "maliang":
        register(
            "inspect_region", "Crop current image evidence at original resolution.", Crop, crop, "observation"
        )
        register(
            "record_review",
            "Record a model self-review against current evidence. Use uncertain when evidence is inadequate.",
            RecordReview,
            record,
            "verification",
        )
        register(
            "verify_artwork",
            "Check integrity, structural requirements and current review coverage.",
            Empty,
            lambda: verify(store, latest_export(store)),
            "verification",
        )
        register(
            "restore_version",
            "Restore earlier content as a NEW revision; prior reviews cannot be reused.",
            Restore,
            lambda expected_revision, revision: store.restore(revision, expected_revision).model_dump(),
            "recovery",
            True,
        )
        register(
            "finalize_artwork",
            "Declare completion only after current export checks and hard requirements pass.",
            Finalize,
            finalize,
            "delivery",
        )
