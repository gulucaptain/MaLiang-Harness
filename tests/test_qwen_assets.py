import io
import json

import httpx
import pytest
from PIL import Image

from maliang.adapters import qwen
from maliang.agent import make_context
from maliang.models import Artwork, Budget
from maliang.runtime import BudgetExceeded
from maliang.settings import ImageGenerationSettings
from maliang.store import ProjectStore


@pytest.fixture
def hybrid(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-test-key")
    store = ProjectStore(tmp_path)
    store.create(
        Artwork(
            prompt="Photographic cat with code-controlled layout",
            workflow_policy="guided",
            allow_generated_assets=True,
            requirements=[{"id": "fur", "description": "Natural realistic fur"}],
        )
    )
    ctx = make_context(tmp_path, plugins=False, image_generation=ImageGenerationSettings(enabled=True))
    ctx.registry.invoke(
        "plan_creation",
        dict(
            expected_revision=0,
            backend="scene2d",
            capability_assessment="Use Qwen for fur and code for controlled composition",
            plan=["Generate subject", "Compose with code", "Review actual final image"],
            requirements=[{"id": "fur", "description": "Natural realistic fur"}],
            checkpoints=[{"id": "final", "description": "Visual check", "requirement_ids": ["fur"]}],
        ),
    )
    return ctx


def plan_asset(ctx):
    return ctx.registry.invoke(
        "plan_asset",
        {
            "expected_revision": ctx.store.load().revision,
            "task": {
                "asset_id": "cat",
                "role": "subject",
                "requirement_ids": ["fur"],
                "visual_goal": "Anatomically plausible cat with individual fur strands",
                "generation_reason": "Procedural paths cannot adequately render realistic fur",
                "prompt": "Photographic orange cat on windowsill, natural sunlight, detailed fur",
                "code_composition": "Use code to position and crop image, add controlled frame layout",
                "background_handling": "Keep coherent windowsill setting; no automatic alpha cutout",
                "temporal_plan": "Reuse static subject for camera motion only; no articulated motion",
            },
        },
    )


def mock_api(monkeypatch, poll_failure=False, failed=False):
    output = io.BytesIO()
    Image.new("RGB", (32, 32), "orange").save(output, "PNG")
    calls = []
    real_client = httpx.Client
    state = {"fail": poll_failure}

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["model"] == "qwen-image"
            assert body["parameters"]["n"] == 1
            assert "detailed fur" in body["input"]["prompt"]
            assert request.headers["x-dashscope-async"] == "enable"
            return httpx.Response(200, json={"output": {"task_id": "job-1"}})
        if "/tasks/" in str(request.url):
            if state["fail"]:
                state["fail"] = False
                raise httpx.ReadTimeout("simulated", request=request)
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "FAILED" if failed else "SUCCEEDED",
                        "results": [
                            {"url": "https://test-storage.example/image.png?secret-signature=hidden"}
                        ],
                    }
                },
            )
        assert "authorization" not in request.headers
        return httpx.Response(200, content=output.getvalue())

    monkeypatch.setattr(
        qwen.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    return calls


def test_planning_permission_and_stale_revision_block_paid_call(hybrid, monkeypatch):
    calls = mock_api(monkeypatch)
    result = hybrid.registry.invoke("generate_asset", {"expected_revision": 1, "asset_id": "cat"})
    assert "ASSET_PLAN_REQUIRED" in result["error"]
    plan_asset(hybrid)
    result = hybrid.registry.invoke("generate_asset", {"expected_revision": 1, "asset_id": "cat"})
    assert "STALE_STATE" in result["error"]
    assert not calls
    hybrid.store.mutate(2, lambda d: d.update(allow_generated_assets=False))
    result = hybrid.registry.invoke("generate_asset", {"expected_revision": 3, "asset_id": "cat"})
    assert "forbidden" in result["error"]
    assert not calls


def test_generate_persist_inspect_and_code_composition(hybrid, monkeypatch):
    calls = mock_api(monkeypatch)
    plan_asset(hybrid)
    result = hybrid.registry.invoke("generate_asset", {"expected_revision": 2, "asset_id": "cat"})
    assert result["dimensions"] == [32, 32]
    art = hybrid.store.load()
    assert art.assets[0].provenance["asset_task"]["requirement_ids"] == ["fur"]
    assert art.program is None  # The generator cannot deliver a final composition itself.
    assert hybrid.meter.usage["asset_api_calls"] == 1
    shown = hybrid.registry.invoke("inspect_asset", {"asset_id": "cat"})
    assert shown[1]["type"] == "image_url"
    assert not list(hybrid.store.root.glob("evidence/*.json"))
    result = hybrid.registry.invoke(
        "put_object",
        dict(
            expected_revision=3,
            object_id="cat_layer",
            source="function draw(ctx,t,object,assets,random){ctx.drawImage(assets.cat,0,0,512,512);}",
            asset_ids=["cat"],
        ),
    )
    assert result["revision"] == 4
    assert hybrid.store.load().objects[0].asset_ids == ["cat"]
    assert len(calls) == 3
    trace = hybrid.store.path("trace.jsonl").read_text()
    assert "fake-test-key" not in trace and "secret-signature" not in trace


def test_retry_recovers_existing_job_without_new_charge(hybrid, monkeypatch):
    calls = mock_api(monkeypatch, poll_failure=True)
    plan_asset(hybrid)
    args = {"expected_revision": 2, "asset_id": "cat"}
    assert "network" in hybrid.registry.invoke("generate_asset", args)["error"]
    # Rebuild context just as a resumed CLI run does, preserving budget and job ID.
    resumed = make_context(
        hybrid.store.root, resume=True, plugins=False, image_generation=ImageGenerationSettings(enabled=True)
    )
    assert resumed.registry.invoke("generate_asset", args)["asset_id"] == "cat"
    assert len([c for c in calls if c.method == "POST"]) == 1
    assert resumed.meter.usage["asset_api_calls"] == 1


def test_asset_budget_blocks_submission(hybrid, monkeypatch):
    calls = mock_api(monkeypatch)
    plan_asset(hybrid)
    hybrid.meter.budget = Budget(max_asset_api_calls=0)
    with pytest.raises(BudgetExceeded):
        hybrid.registry.invoke("generate_asset", {"expected_revision": 2, "asset_id": "cat"})
    assert not calls


def test_failed_provider_does_not_create_asset(hybrid, monkeypatch):
    mock_api(monkeypatch, failed=True)
    plan_asset(hybrid)
    result = hybrid.registry.invoke("generate_asset", {"expected_revision": 2, "asset_id": "cat"})
    assert "FAILED" in result["error"]
    assert not hybrid.store.load().assets


def test_explicit_disabled_provider_does_not_fallback_to_openai(hybrid, monkeypatch):
    monkeypatch.setenv("MALIANG_IMAGE_MODEL", "legacy-image")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    ctx = make_context(hybrid.store.root, plugins=False, image_generation=ImageGenerationSettings())
    assert "generate_asset" not in ctx.registry.capabilities


@pytest.mark.rendering
def test_generated_asset_renders_and_reuses_in_video(hybrid, monkeypatch):
    import av

    mock_api(monkeypatch)
    plan_asset(hybrid)
    hybrid.registry.invoke("generate_asset", {"expected_revision": 2, "asset_id": "cat"})
    hybrid.store.mutate(3, lambda d: d["spec"].update(width=64, height=64, duration=1, fps=3, format="mp4"))
    hybrid.registry.invoke(
        "put_object",
        dict(
            expected_revision=4,
            object_id="cat_layer",
            source="function(ctx,t,object,assets,random){ctx.drawImage(assets.cat,0,0,16,16);}",
            asset_ids=["cat"],
        ),
    )
    hybrid.registry.invoke(
        "animate_object",
        dict(
            expected_revision=5,
            object_id="cat_layer",
            reason="Translate same asset without regenerating any frames",
            tracks=[{"property": "x", "keyframes": [{"time": 0, "value": 0}, {"time": 1, "value": 40}]}],
        ),
    )
    frames = hybrid.renderers.preview([0, 0.9])
    with (
        Image.open(hybrid.store.path(frames.paths[0])) as first,
        Image.open(hybrid.store.path(frames.paths[1])) as last,
    ):
        assert first.getpixel((4, 4))[:3] == (255, 165, 0)
        assert last.getpixel((40, 4))[:3] == (255, 165, 0)
        assert first.tobytes() != last.tobytes()
    exported = hybrid.renderers.export()
    with av.open(str(hybrid.store.path(exported.paths[0]))) as video:
        assert len(list(video.decode(video=0))) == 3
    assert hybrid.meter.usage["asset_api_calls"] == 1
