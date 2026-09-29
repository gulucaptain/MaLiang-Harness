import json

import pytest
from PIL import Image

from maliang.agent import make_context, user_content
from maliang.editing import create_edit, history_preview, snapshot, version_index
from maliang.models import Artwork, Evidence, OutputSpec, Requirement, Review
from maliang.store import ProjectStore


@pytest.fixture
def source(tmp_path):
    store = ProjectStore(tmp_path / "source")
    store.create(
        Artwork(
            prompt="A red square on a white background",
            spec=OutputSpec(width=64, height=64, format="png"),
            requirements=[Requirement(id="red", description="Square is red")],
        )
    )
    ctx = make_context(store.root, plugins=False)
    ctx.registry.invoke(
        "write_program",
        {
            "expected_revision": 0,
            "backend": "canvas",
            "source": "function(ctx){ctx.fillStyle='white';ctx.fillRect(0,0,64,64);ctx.fillStyle='red';ctx.fillRect(10,10,20,20)}",
        },
    )
    ctx.registry.invoke("patch_program", {"expected_revision": 1, "old_text": "'red'", "new_text": "'green'"})
    return store


def test_branch_uses_selected_snapshot_and_keeps_source_intact(source, tmp_path):
    before = source.path("artwork.json").read_bytes()
    target = ProjectStore(tmp_path / "edit")
    art = create_edit(source, target, 1, "Make the square blue", preview=False)
    assert art.revision == 0 and art.workflow_policy == "guided"
    assert "'red'" in target.path(art.program.path).read_text()
    assert "'green'" in source.path(source.load().program.path).read_text()
    assert source.path("artwork.json").read_bytes() == before
    record = json.loads(target.path("edit.json").read_text())
    assert record["source_revision"] == 1
    assert not target.path("usage.json").exists()
    assert not target.path("checkpoints.sqlite").exists()
    assert target.reviews() == {} and len(art.requirements) == 2
    with pytest.raises(ValueError):
        create_edit(source, target, 1, "again", preview=False)
    for revision in (-1, True, 99):
        with pytest.raises(ValueError):
            snapshot(source, revision)


def test_edit_reconciles_only_inherited_checks_and_records_reason(source, tmp_path):
    target = ProjectStore(tmp_path / "edit")
    create_edit(source, target, 1, "Make the square blue", preview=False)
    ctx = make_context(target.root, plugins=False)
    assert "generate_asset" not in ctx.registry.capabilities
    args = {
        "expected_revision": 0,
        "requirement_id": "red",
        "description": "Square is blue",
        "instruction_quote": "square blue",
        "reason": "User changes the square color",
    }
    assert ctx.registry.invoke("revise_edit_requirement", args)["revision"] == 1
    assert target.load().requirements[0].description == "Square is blue"
    assert "edit_requirement_revised" in target.path("trace.jsonl").read_text()
    new_id = target.load().requirements[-1].id
    assert "error" in ctx.registry.invoke(
        "revise_edit_requirement", {**args, "expected_revision": 1, "requirement_id": new_id}
    )
    assert "error" in ctx.registry.invoke(
        "revise_edit_requirement", {**args, "expected_revision": 1, "instruction_quote": "unrelated change"}
    )
    assert "error" in ctx.registry.invoke("revise_edit_requirement", args)
    assert target.load().revision == 1
    assert "revise_edit_requirement" not in make_context(source.root, plugins=False).registry.capabilities


def test_corrupt_content_is_rejected(source, tmp_path):
    original = snapshot(source, 1)
    source.path(original.program.path).write_text("corrupted")
    with pytest.raises(ValueError, match="integrity"):
        create_edit(source, ProjectStore(tmp_path / "edit"), 1, "change", preview=False)


@pytest.mark.rendering
def test_historical_preview_is_isolated_cached_and_visually_correct(source, tmp_path):
    original = source.path("artwork.json").read_bytes()
    usage = source.path("usage.json").read_bytes()
    result = history_preview(source, 1)
    with Image.open(source.path(result["media"][0]["path"])) as im:
        assert im.getpixel((15, 15))[:3] == (255, 0, 0)
    assert history_preview(source, 1) == result
    assert source.path("artwork.json").read_bytes() == original
    assert source.path("usage.json").read_bytes() == usage
    assert version_index(source)[1]["thumbnail"] == result["media"][0]["path"]
    target = ProjectStore(tmp_path / "edit")
    create_edit(source, target, 1, "Make the square blue")
    assert any(item["type"] == "image_url" for item in user_content(target))
    ctx = make_context(target.root, plugins=False)
    assert ctx.registry.invoke("inspect_edit_source", {})[1]["type"] == "image_url"
    assert not target.path("evidence/edit-source.json").exists()
    requirements = target.load().requirements
    ctx.registry.invoke(
        "revise_edit_requirement",
        {
            "expected_revision": 0,
            "requirement_id": "red",
            "description": "Square is blue",
            "instruction_quote": "square blue",
            "reason": "Requested color change",
        },
    )
    planned = ctx.registry.invoke(
        "plan_creation",
        {
            "expected_revision": 1,
            "backend": "canvas",
            "capability_assessment": "Existing canvas source can change its fill color without changing layout.",
            "plan": ["Change red to blue", "Compare with source and inspect"],
            "requirements": [r.model_dump() for r in target.load().requirements],
            "checkpoints": [
                {
                    "id": "delivery",
                    "description": "Check edit and preservation",
                    "requirement_ids": [r.id for r in requirements],
                }
            ],
        },
    )
    assert planned["revision"] == 2
    assert (
        ctx.registry.invoke(
            "patch_program", {"expected_revision": 2, "old_text": "'red'", "new_text": "'blue'"}
        )["revision"]
        == 3
    )
    evidence = ctx.renderers.preview([0])
    with Image.open(target.path(evidence.paths[0])) as im:
        assert im.getpixel((15, 15))[:3] == (0, 0, 255)
        assert im.getpixel((0, 0))[:3] == (255, 255, 255)
    target.restore(0, 3)
    with pytest.raises(ValueError, match="STALE_EVIDENCE"):
        target.add_review(
            Review(
                requirement_id="red",
                revision=4,
                evidence_ids=[evidence.id],
                verdict="pass",
                explanation="old",
            )
        )


@pytest.mark.rendering
def test_video_branch_preserves_timeline_and_preview_timestamps(tmp_path):
    source = ProjectStore(tmp_path / "video")
    source.create(
        Artwork(prompt="moving square", spec=OutputSpec(width=64, height=64, duration=2, fps=6, format="mp4"))
    )
    ctx = make_context(source.root, plugins=False)
    ctx.registry.invoke(
        "write_program",
        {
            "expected_revision": 0,
            "backend": "canvas",
            "source": "function(ctx,t){ctx.fillStyle='red';ctx.fillRect(t*20,10,10,10)}",
        },
    )
    target = ProjectStore(tmp_path / "edit")
    art = create_edit(source, target, 1, "Make movement slower")
    assert art.spec == source.load().spec
    baseline = json.loads(target.path("edit.json").read_text())["baseline"]
    assert [p["time"] for p in baseline] == [0, (2 - 1 / 6) / 2, 2 - 1 / 6]
    assert art.requirements[-1].kind == "temporal"
    assert len({target.path(p["path"]).read_bytes() for p in baseline}) == 3


def test_version_index_ignores_crop_evidence(source):
    path, _ = source.blob(b"fake crop", ".png")
    source.add_evidence(
        Evidence(id="crop", revision=1, kind="frames", paths=[path], metadata={"requirement_id": "red"})
    )
    assert version_index(source)[1]["thumbnail"] is None


@pytest.mark.rendering
def test_scene_branch_keeps_draw_functions_and_can_edit_motion(tmp_path):
    source = ProjectStore(tmp_path / "scene")
    source.create(
        Artwork(prompt="moving sun", spec=OutputSpec(width=64, height=64, duration=2, fps=6, format="mp4"))
    )
    ctx = make_context(source.root, plugins=False)
    ctx.registry.invoke(
        "put_object",
        {
            "expected_revision": 0,
            "object_id": "sun",
            "source": "function(ctx){ctx.fillStyle='red';ctx.fillRect(0,0,10,10)}",
            "transform": {"x": 10, "y": 10},
        },
    )
    ctx.registry.invoke(
        "animate_object",
        {
            "expected_revision": 1,
            "object_id": "sun",
            "reason": "moving sun",
            "tracks": [{"property": "x", "keyframes": [{"time": 0, "value": 10}, {"time": 2, "value": 40}]}],
        },
    )
    target = ProjectStore(tmp_path / "edit")
    create_edit(source, target, 2, "Keep the sun stationary at x=10")
    child = make_context(target.root, plugins=False)
    reqs = target.load().requirements
    result = child.registry.invoke(
        "plan_creation",
        {
            "expected_revision": 0,
            "backend": "scene2d",
            "capability_assessment": "Change existing transform track, retaining the sun draw function.",
            "plan": ["Replace the x motion track", "Inspect frames"],
            "requirements": [r.model_dump() for r in reqs],
            "checkpoints": [
                {"id": "delivery", "description": "Verify edit", "requirement_ids": [r.id for r in reqs]}
            ],
        },
    )
    assert result["revision"] == 1
    result = child.registry.invoke(
        "animate_object",
        {
            "expected_revision": 1,
            "object_id": "sun",
            "reason": "User requests stationary sun",
            "tracks": [{"property": "x", "keyframes": [{"time": 0, "value": 10}, {"time": 2, "value": 10}]}],
        },
    )
    assert result["revision"] == 2
    assert source.load().objects[0].motion[0].keyframes[-1].value == 40
    export = child.renderers.video(0, 2, final=True)
    assert target.path(export.paths[0]).is_file()
    ev = child.renderers.preview([0, 1.8])
    assert target.path(ev.paths[0]).read_bytes() == target.path(ev.paths[1]).read_bytes()
    resumed = make_context(target.root, resume=True, plugins=False)
    assert "revise_edit_requirement" in resumed.registry.capabilities
    assert "generate_asset" not in resumed.registry.capabilities
    assert resumed.meter.usage["tool_calls"] == child.meter.usage["tool_calls"]


def test_edit_identity_survives_interrupted_baseline_render(source, tmp_path, monkeypatch):
    import maliang.editing as editing

    def interrupted(*args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(editing, "history_preview", interrupted)
    target = ProjectStore(tmp_path / "interrupted")
    with pytest.raises(KeyboardInterrupt):
        create_edit(source, target, 1, "Make blue")
    assert target.load().revision == 0
    assert json.loads(target.path("edit.json").read_text())["instruction"] == "Make blue"
    assert "generate_asset" not in make_context(target.root, resume=True, plugins=False).registry.capabilities
