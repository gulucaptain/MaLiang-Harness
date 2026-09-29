"""Code-first local repair with a mocked Qwen provider."""

import io
import json

import httpx
import pytest
from PIL import Image, ImageDraw

from maliang.adapters import qwen
from maliang.agent import make_context
from maliang.models import Artwork, Evidence, OutputSpec, Review
from maliang.settings import ImageGenerationSettings, load_harness_settings
from maliang.store import ProjectStore


@pytest.fixture
def repair(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "local-fake-key")
    store = ProjectStore(tmp_path)
    store.create(
        Artwork(
            prompt="A cat with realistic fur in a code-drawn room",
            workflow_policy="guided",
            spec=OutputSpec(width=64, height=64, format="png"),
            allow_generated_assets=True,
            requirements=[{"id": "fur", "description": "Cat fur and anatomy look realistic"}],
        )
    )
    cfg = ImageGenerationSettings(enabled=True, workflow="code_first_repair")
    ctx = make_context(tmp_path, image_generation=cfg, plugins=False)
    ctx.registry.invoke(
        "plan_creation",
        dict(
            expected_revision=0,
            backend="scene2d",
            capability_assessment="Draw complete cat and room with code first, then inspect visual weaknesses",
            plan=["Draw coded scene", "Observe actual pixels", "Repair only weak local detail"],
            requirements=[{"id": "fur", "description": "Cat fur and anatomy look realistic"}],
            checkpoints=[{"id": "final", "description": "Check result", "requirement_ids": ["fur"]}],
        ),
    )
    return ctx


def task(ctx, **overrides):
    value = dict(
        asset_id="cat_raw",
        role="subject",
        requirement_ids=["fur"],
        visual_goal="Improve only the cat shape, eyes, and fine hair in the existing room",
        generation_reason="The current code cat looks rigid and its fur is not convincing",
        prompt="Single orange and white cat with natural fur and anatomically correct paws",
        code_composition="Retain code room and window; replace only the cat object in the local box",
        background_handling="Generate only the cat on white; extract the background to transparent alpha",
        temporal_plan="Static cutout only; code controls any later camera movement",
        repair_evidence_id=None,
        target_object_id="cat",
        target_region=[16, 8, 48, 56],
        extraction="white_background",
    )
    value.update(overrides)
    return ctx.registry.invoke("plan_asset", dict(expected_revision=ctx.store.load().revision, task=value))


def coded_scene(ctx):
    ctx.registry.invoke(
        "put_object",
        dict(
            expected_revision=1,
            object_id="room",
            layer=0,
            source='function(ctx,t,object,assets,random){ctx.fillStyle="rgb(35,80,140)";ctx.fillRect(0,0,64,64);}',
            asset_ids=[],
        ),
    )
    ctx.registry.invoke(
        "put_object",
        dict(
            expected_revision=2,
            object_id="cat",
            layer=1,
            source='function(ctx,t,object,assets,random){ctx.fillStyle="orange";ctx.fillRect(16,8,32,48);}',
            asset_ids=[],
        ),
    )


def mock_white_cat(monkeypatch, background="white"):
    im = Image.new("RGB", (64, 64), background)
    draw = ImageDraw.Draw(im)
    draw.ellipse((16, 8, 48, 55), fill=(235, 125, 38), outline=(100, 55, 20), width=2)
    draw.ellipse((25, 25, 39, 48), fill="white", outline=(100, 55, 20), width=2)
    data = io.BytesIO()
    im.save(data, "PNG")
    calls = []
    real = httpx.Client

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            prompt = json.loads(request.content)["input"]["prompt"]
            assert "uniform pure white" in prompt
            assert "window" not in prompt
            return httpx.Response(200, json={"output": {"task_id": "job-1"}})
        if "/tasks/" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [{"url": "https://example.test/cat.png"}],
                    }
                },
            )
        assert "authorization" not in request.headers
        return httpx.Response(200, content=data.getvalue())

    monkeypatch.setattr(
        qwen.httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs)
    )
    return calls


def test_code_first_requires_draft_current_failed_review_and_local_box(repair):
    assert "CODE_DRAFT_REQUIRED" in task(repair)["error"]
    coded_scene(repair)
    assert "LOCAL_REPAIR_REQUIRED" in task(repair)["error"]
    path, _ = repair.store.blob(Image.new("RGB", (64, 64), "blue").tobytes(), ".bin")
    parent = Evidence(
        id=repair.store.new_id(),
        revision=3,
        kind="frames",
        paths=[path],
        timestamps=[0],
        metadata={"frame_details": [{"object_bounds": {"cat": [16, 8, 48, 56]}}]},
    )
    repair.store.add_evidence(parent)
    ev = Evidence(
        id=repair.store.new_id(),
        revision=3,
        kind="frames",
        paths=[path],
        timestamps=[0],
        metadata={"requirement_id": "fur", "parent": parent.id},
    )
    repair.store.add_evidence(ev)
    repair.store.add_review(
        Review(
            requirement_id="fur",
            revision=3,
            evidence_ids=[ev.id],
            verdict="fail",
            explanation="Code cat is a rectangle",
        )
    )
    assert "too much" in task(repair, repair_evidence_id=ev.id, target_region=[0, 0, 64, 64])["error"]
    assert task(repair, repair_evidence_id=ev.id)["revision"] == 4
    assert repair.meter.usage["asset_api_calls"] == 0


@pytest.mark.rendering
def test_local_white_cat_cutout_preserves_code_background(repair, monkeypatch):
    calls = mock_white_cat(monkeypatch)
    coded_scene(repair)
    before = repair.renderers.preview([0])
    observed = repair.registry.invoke("observe_requirement", {"requirement_id": "fur"})
    evidence_id = json.loads(observed[0]["text"])["id"]
    repair.registry.invoke(
        "record_review",
        dict(
            requirement_id="fur",
            evidence_ids=[evidence_id],
            verdict="fail",
            explanation="Code cat is a rigid orange rectangle",
        ),
    )
    assert task(repair, repair_evidence_id=evidence_id)["revision"] == 4
    assert (
        repair.registry.invoke("generate_asset", {"expected_revision": 4, "asset_id": "cat_raw"})["revision"]
        == 5
    )
    extracted = repair.registry.invoke(
        "extract_white_background",
        {"expected_revision": 5, "source_asset_id": "cat_raw", "asset_id": "cat_cutout"},
    )
    assert extracted["revision"] == 6
    with Image.open(repair.store.path(repair.store.load().assets[-1].path)) as cutout:
        assert cutout.getpixel((0, 0))[3] == 0
        assert cutout.getpixel((32, 32))[3] == 255
    assert (
        "LOCAL_REPAIR_REQUIRED"
        in repair.registry.invoke(
            "put_object",
            dict(
                expected_revision=6,
                object_id="whole_image",
                asset_ids=["cat_raw"],
                source="function(ctx,t,object,assets,random){ctx.drawImage(assets.cat_raw,0,0,64,64);}",
            ),
        )["error"]
    )
    placed = repair.registry.invoke(
        "place_cutout",
        {"expected_revision": 6, "source_asset_id": "cat_raw", "cutout_asset_id": "cat_cutout"},
    )
    assert placed["revision"] == 7
    after = repair.renderers.preview([0])
    with (
        Image.open(repair.store.path(before.paths[0])) as original,
        Image.open(repair.store.path(after.paths[0])) as final,
    ):
        assert original.getpixel((2, 2)) == final.getpixel((2, 2))
        assert original.getpixel((32, 32)) != final.getpixel((32, 32))
    assert len([call for call in calls if call.method == "POST"]) == 1
    assert repair.meter.usage["asset_api_calls"] == 1


def test_project_config_selects_code_directed():
    from pathlib import Path

    cfg = load_harness_settings(Path(__file__).parents[1] / "harness.json")
    assert cfg.image_generation.workflow == "code_directed"
    assert cfg.profile("image").output.format == "png"
    assert cfg.profile("video").output.format == "mp4"
    assert cfg.profile("video").output.width == 1280
    assert cfg.profile("video").output.height == 720


def test_hybrid_mask_can_protect_light_foreground_without_new_generation():
    from maliang.adapters.cutout import extract_subject

    image = Image.new("RGB", (64, 64), (220, 220, 225))
    draw = ImageDraw.Draw(image)
    draw.ellipse((18, 7, 46, 42), fill=(220, 110, 40), outline=(80, 40, 20), width=2)
    draw.rectangle((27, 29, 37, 55), fill=(235, 235, 238))
    raw = io.BytesIO()
    image.save(raw, "PNG")
    plain, _ = extract_subject(raw.getvalue(), method="border_color")
    hybrid, _ = extract_subject(raw.getvalue(), method="hybrid", protect_regions=[[29, 33, 35, 51]])
    with Image.open(io.BytesIO(plain)) as without, Image.open(io.BytesIO(hybrid)) as protected:
        assert without.getpixel((32, 40))[3] == 0
        assert protected.getpixel((32, 40))[3] == 255
        assert protected.getpixel((1, 1))[3] == 0


@pytest.mark.rendering
def test_gray_source_can_be_reused_for_mask_planning_and_scene_preview(repair, monkeypatch):
    calls = mock_white_cat(monkeypatch, background=(220, 220, 225))
    coded_scene(repair)
    observed = repair.registry.invoke("observe_requirement", {"requirement_id": "fur"})
    evidence_id = json.loads(observed[0]["text"])["id"]
    repair.registry.invoke(
        "record_review",
        dict(
            requirement_id="fur",
            evidence_ids=[evidence_id],
            verdict="fail",
            explanation="Cat drawing has no realistic fur",
        ),
    )
    assert task(repair, repair_evidence_id=evidence_id)["revision"] == 4
    assert (
        repair.registry.invoke(
            "generate_asset",
            {
                "expected_revision": 4,
                "asset_id": "cat_raw",
            },
        )["revision"]
        == 5
    )
    inspected = repair.registry.invoke("inspect_asset", {"asset_id": "cat_raw"})
    profile = json.loads(inspected[0]["text"])["metadata"]["background_profile"]
    assert profile["border_near_white_fraction"] == 0
    extracted = repair.registry.invoke(
        "extract_subject",
        {
            "expected_revision": 5,
            "source_asset_id": "cat_raw",
            "asset_id": "cat_gray_cutout",
            "method": "border_color",
        },
    )
    assert extracted["revision"] == 6
    assert extracted["metrics"]["background_fraction"] > 0.2
    preview = repair.registry.invoke(
        "preview_cutout",
        {
            "source_asset_id": "cat_raw",
            "cutout_asset_id": "cat_gray_cutout",
        },
    )
    preview_info = json.loads(preview[0]["text"])
    assert preview_info["metadata"]["scope"] == "composite_preview_only"
    with Image.open(repair.store.path(preview_info["paths"][0])) as image:
        assert image.getpixel((18, 12))[:3] == (35, 80, 140)
    assert (
        repair.registry.invoke(
            "place_cutout",
            {
                "expected_revision": 6,
                "source_asset_id": "cat_raw",
                "cutout_asset_id": "cat_gray_cutout",
            },
        )["revision"]
        == 7
    )
    final = repair.renderers.preview([0])
    with Image.open(repair.store.path(final.paths[0])) as image:
        assert image.getpixel((18, 12))[:3] == (35, 80, 140)
        assert image.getpixel((32, 32))[:3] != (35, 80, 140)
    assert len([call for call in calls if call.method == "POST"]) == 1
    assert repair.meter.usage["asset_api_calls"] == 1
