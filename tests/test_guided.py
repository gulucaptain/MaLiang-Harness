import json

import pytest
from PIL import Image
from pydantic import ValidationError

from maliang.agent import make_context
from maliang.guidance import runtime_capabilities
from maliang.models import Artwork, Evidence, MotionTrack, OutputSpec, Review, Transform
from maliang.store import ProjectStore


@pytest.fixture
def guided(tmp_path):
    store = ProjectStore(tmp_path)
    store.create(
        Artwork(
            prompt="Orange sun moves across a blue sky",
            workflow_policy="guided",
            spec=OutputSpec(width=64, height=64, duration=2, fps=6, format="png"),
            requirements=[{"id": "brief", "description": "Satisfy original brief"}],
        )
    )
    return make_context(tmp_path, plugins=False)


def plan(ctx, temporal=False):
    requirements = [{"id": "composition", "description": "Sun visible", "observation": {"object_id": "sun"}}]
    if temporal:
        requirements.append(
            {
                "id": "motion",
                "kind": "temporal",
                "description": "Sun moves smoothly",
                "observation": {"start": 0, "end": 1.8, "samples": 3},
            }
        )
    return ctx.registry.invoke(
        "plan_creation",
        {
            "expected_revision": ctx.store.load().revision,
            "backend": "scene2d",
            "capability_assessment": "Procedural 2D scene is sufficient; image/video APIs are not needed.",
            "plan": ["Draw objects", "Observe and refine", "Check motion and export"],
            "requirements": requirements,
            "checkpoints": [
                {
                    "id": "delivery",
                    "description": "Verify all requirements",
                    "requirement_ids": ["brief", *[r["id"] for r in requirements]],
                }
            ],
        },
    )


def test_capability_report_separates_configuration_and_permission(guided, monkeypatch):
    monkeypatch.delenv("MALIANG_IMAGE_MODEL", raising=False)
    report = runtime_capabilities(guided)
    assert report["creation_mode"] == "code_only"
    assert report["image_generation"]["availability"] == "not_configured"
    assert not report["image_generation"]["callable"]
    assert report["video"]["procedural_mp4"]
    monkeypatch.setenv("MALIANG_IMAGE_MODEL", "configured-but-forbidden")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    # Provider configuration is snapshotted when constructing a run context.
    guided = make_context(guided.store.root, plugins=False)
    report = runtime_capabilities(guided)
    assert report["image_generation"]["configured"]
    assert not report["image_generation"]["allowed_for_task"]
    assert not report["image_generation"]["callable"]
    assert "test-placeholder" not in json.dumps(report)


def test_guided_requires_plan_before_drawing(guided):
    result = guided.registry.invoke(
        "put_object", {"expected_revision": 0, "object_id": "sun", "source": "function(){}"}
    )
    assert "PLAN_REQUIRED" in result["error"]
    assert guided.store.load().revision == 0
    assert plan(guided)["revision"] == 1
    result = guided.registry.invoke(
        "put_object", {"expected_revision": 1, "object_id": "sun", "source": "function(){}"}
    )
    assert result["revision"] == 2
    assert guided.store.load().objects[0].draw is not None


def test_plan_cannot_weaken_user_requirement(guided):
    result = guided.registry.invoke(
        "plan_creation",
        {
            "expected_revision": 0,
            "backend": "scene2d",
            "capability_assessment": "No external APIs needed",
            "plan": ["Draw"],
            "requirements": [{"id": "brief", "description": "Something easier", "hard": False}],
            "checkpoints": [{"id": "c", "description": "Check", "requirement_ids": ["brief"]}],
        },
    )
    assert result["status"] == "error"
    assert guided.store.load().requirements[0].hard
    assert guided.store.load().revision == 0


def test_guided_review_rejects_untargeted_evidence(guided):
    plan(guided)
    path = guided.store.path("preview.png")
    Image.new("RGB", (64, 64)).save(path)
    ev = Evidence(id=guided.store.new_id(), revision=1, kind="frames", paths=["preview.png"], timestamps=[0])
    guided.store.add_evidence(ev)
    with pytest.raises(ValueError, match="observe_requirement"):
        guided.store.add_review(
            Review(
                requirement_id="brief",
                revision=1,
                evidence_ids=[ev.id],
                verdict="pass",
                explanation="Looks good",
            )
        )


def test_temporal_pass_requires_multiple_times(guided):
    plan(guided, temporal=True)
    Image.new("RGB", (64, 64)).save(guided.store.path("preview.png"))
    ev = Evidence(
        id=guided.store.new_id(),
        revision=1,
        kind="frames",
        paths=["preview.png"],
        timestamps=[0],
        metadata={"requirement_id": "motion"},
    )
    guided.store.add_evidence(ev)
    with pytest.raises(ValueError, match="three distinct"):
        guided.store.add_review(
            Review(
                requirement_id="motion",
                revision=1,
                evidence_ids=[ev.id],
                verdict="pass",
                explanation="One image",
            )
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"property": "x", "keyframes": [{"time": 1, "value": 0}, {"time": 0, "value": 20}]},
        {"property": "opacity", "keyframes": [{"time": 0, "value": 0}, {"time": 1, "value": 2}]},
    ],
)
def test_invalid_animation_rejected(payload):
    with pytest.raises(ValidationError):
        MotionTrack.model_validate(payload)


def test_nonfinite_transform_rejected():
    with pytest.raises(ValidationError):
        Transform(x=float("nan"))


def test_motion_override_cannot_silently_ignore_edits(guided):
    plan(guided)
    guided.registry.invoke(
        "put_object", {"expected_revision": 1, "object_id": "sun", "source": "function(){}"}
    )
    guided.registry.invoke(
        "animate_object",
        {
            "expected_revision": 2,
            "object_id": "sun",
            "reason": "Move sun",
            "tracks": [{"property": "x", "keyframes": [{"time": 0, "value": 5}, {"time": 1, "value": 20}]}],
        },
    )
    result = guided.registry.invoke(
        "edit_object",
        {"expected_revision": 3, "object_id": "sun", "reason": "Move sun again", "transform": {"x": 30}},
    )
    assert "Motion overrides" in result["error"]
    assert guided.store.load().revision == 3


def test_compare_detects_changes_in_protected_region(guided):
    for n, color in enumerate(("white", "black")):
        Image.new("RGB", (64, 64), color).save(guided.store.path(f"{n}.png"))
    before = Evidence(id=guided.store.new_id(), revision=0, kind="frames", paths=["0.png"], timestamps=[0])
    guided.store.add_evidence(before)
    plan(guided)
    after = Evidence(id=guided.store.new_id(), revision=1, kind="frames", paths=["1.png"], timestamps=[0])
    guided.store.add_evidence(after)
    result = guided.registry.invoke(
        "compare_versions",
        {"before_evidence_id": before.id, "after_evidence_id": after.id, "preserve_region": [0, 0, 20, 20]},
    )
    data = json.loads(result[0]["text"])
    assert data["metadata"]["metrics"][0]["changed_pixels"] == 4096
    assert not data["metadata"]["metrics"][0]["preserve_region_unchanged"]


def test_old_run_loads_without_migration():
    art = Artwork.model_validate(
        {"prompt": "Old task", "objects": [{"id": "sun"}], "allowed_backends": ["canvas"]}
    )
    assert art.workflow_policy == "legacy"
    assert art.objects[0].draw is None


def test_checkpoint_and_reviews_invalidated_by_new_revision(guided):
    plan(guided)
    guided.registry.invoke(
        "put_object", {"expected_revision": 1, "object_id": "sun", "source": "function(){}"}
    )
    Image.new("RGB", (64, 64), "orange").save(guided.store.path("preview.png"))
    for req in ("brief", "composition"):
        ev = Evidence(
            id=guided.store.new_id(),
            revision=2,
            kind="frames",
            paths=["preview.png"],
            timestamps=[0],
            metadata={"requirement_id": req},
        )
        guided.store.add_evidence(ev)
        guided.store.add_review(
            Review(
                requirement_id=req, revision=2, evidence_ids=[ev.id], verdict="pass", explanation="Reviewed"
            )
        )
    assert guided.registry.invoke("complete_checkpoint", {"checkpoint_id": "delivery"})["status"] == "passed"
    from maliang.verification import verify

    assert next(c for c in verify(guided.store)["technical_checks"] if c["check"] == "checkpoint:delivery")[
        "passed"
    ]
    guided.registry.invoke(
        "edit_object",
        {"expected_revision": 2, "object_id": "sun", "transform": {"x": 3}, "reason": "Changed composition"},
    )
    report = verify(guided.store)
    assert not next(c for c in report["technical_checks"] if c["check"] == "checkpoint:delivery")["passed"]
    assert all(r["verdict"] == "unreviewed" for r in report["requirements"])


def test_unbound_scene_object_cannot_be_rendered(guided):
    plan(guided)
    guided.registry.invoke(
        "update_artwork", {"expected_revision": 1, "plan": ["Draw"], "objects": [{"id": "sun"}], "events": []}
    )
    guided.registry.invoke(
        "write_program", {"expected_revision": 2, "backend": "scene2d", "source": '{"version":1}'}
    )
    result = guided.registry.invoke("render_frames", {"timestamps": [0]})
    assert "Unbound scene object" in result["error"]


def test_long_video_batches_rendering_without_system_ffmpeg(tmp_path):
    from maliang.verification import verify

    class Solid:
        name, suffix, supports_animation, description = "solid", ".json", True, "Test animation"

        def validate_source(self, source):
            pass

        def frames(self, artwork, source, times, output, assets):
            assert len(times) <= 60
            for i, t in enumerate(times):
                Image.new("RGB", (32, 32), (int(t * 20) % 255, 0, 0)).save(output / f"{i:06d}.png")

    store = ProjectStore(tmp_path)
    store.create(
        Artwork(
            prompt="Batch test",
            spec=OutputSpec(width=32, height=32, duration=11, fps=6, format="mp4"),
            allowed_backends=["solid"],
        )
    )
    ctx = make_context(tmp_path, plugins=False)
    ctx.renderers.register(Solid())
    ctx.registry.invoke("write_program", {"expected_revision": 0, "backend": "solid", "source": "{}"})
    export = ctx.renderers.export()
    assert export.metadata["frames"] == 66
    assert ctx.meter.usage["frames"] == 66
    assert verify(store, export.id)["eligible_to_finalize"]
