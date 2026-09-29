import io
import json

import pytest
from PIL import Image
from test_code_directed import creation
from test_code_directed import directed as directed

from maliang.adapters.assets import background_geometry


def call(ctx, name, **args):
    return ctx.registry.invoke(name, {"expected_revision": ctx.store.load().revision, **args})


def scaffold_background(ctx):
    plan = creation()
    plan["components"][0].update(object_id="actor", route="code")
    plan["components"][1].update(
        object_id="environment",
        route="generated_background",
        control_requirements="Static environment, actor is independently animated",
    )
    assert "error" not in call(ctx, "plan_creation", **plan)
    assert "error" not in call(
        ctx,
        "put_object",
        object_id="actor",
        layer=4,
        source='function(ctx){ctx.fillStyle="red";ctx.fillRect(28,20,8,8);}',
    )
    assert "error" not in call(
        ctx,
        "put_object",
        object_id="environment",
        layer=0,
        source='function(ctx){ctx.fillStyle="blue";ctx.fillRect(0,0,64,64);}',
    )
    return dict(
        asset_id="plate",
        role="background",
        requirement_ids=["brief"],
        visual_goal="Realistic static environment for actor",
        generation_reason="Coherent environmental lighting",
        prompt="Realistic room and seat with ground shadows. No cat; reserve its empty space.",
        code_composition="Actor is drawn above the generated environment",
        background_handling="Keep the entire background, without any extraction",
        temporal_plan="Static plate; actor animates independently",
        target_object_id="environment",
        target_region=[0, 0, 64, 64],
        extraction="none",
        background_layout=dict(
            static_contents=["room", "seat", "ground shadows"],
            excluded_object_ids=["actor"],
            camera="Front view at eye level",
            lighting="Soft light from upper left",
            reserved_regions={"actor": [20, 10, 44, 40]},
            anchors={"seat": [32, 40]},
        ),
    )


def generate_plate(ctx, monkeypatch, task):
    assert "error" not in call(ctx, "plan_asset", task=task)

    def fake_generate(context, planned, prompt=None):
        assert planned.role == "background"
        assert "reserved_regions" in prompt and "ground shadows" in prompt
        assert "uniform pure white" not in prompt
        context.meter.consume("asset_api_calls")
        image = Image.new("RGB", (128, 64), "green")
        data = io.BytesIO()
        image.save(data, format="PNG")
        return data.getvalue(), {"source": "qwen", "model": "offline-test"}

    monkeypatch.setattr("maliang.adapters.qwen.generate", fake_generate)
    assert "error" not in call(ctx, "generate_asset", asset_id=task["asset_id"])


def test_plate_plan_generate_place_without_extraction(directed, monkeypatch):
    task = scaffold_background(directed)
    generate_plate(directed, monkeypatch, task)
    actor = directed.store.load().objects[0].model_dump()
    inspection = directed.registry.invoke("inspect_asset", {"asset_id": "plate"})
    assert json.loads(inspection[0]["text"])["metadata"]["cover_source_crop"] == [32, 0, 64, 64]
    result = call(
        directed,
        "place_background",
        asset_id="plate",
        observed_anchors={"seat": [32, 42]},
        layout_notes="Seat observed at y=42 after crop; actor space is clear",
    )
    assert result["source_crop"] == [32, 0, 64, 64]
    art = directed.store.load()
    assert art.objects[0].model_dump() == actor
    plate = art.objects[1]
    assert plate.layer < art.objects[0].layer
    assert plate.asset_ids == ["plate"]
    assert plate.properties["background_layout"]["observed_anchors"]["seat"] == [32, 42]
    assert len(art.assets) == 1 and directed.meter.usage["asset_api_calls"] == 1
    assert "extract_subject" not in directed.store.path("trace.jsonl").read_text()
    # Regeneration uses a new ID and binding recomputes anchor metadata.
    task["asset_id"] = "plate_v2"
    generate_plate(directed, monkeypatch, task)
    result = call(
        directed,
        "place_background",
        asset_id="plate_v2",
        observed_anchors={"seat": [31, 38]},
        layout_notes="Replacement seat position remeasured from the new image",
    )
    assert "error" not in result
    assert directed.store.load().objects[1].properties["background_layout"]["asset_id"] == "plate_v2"


def test_plate_rejects_invalid_layout_and_unmeasured_anchors(directed, monkeypatch):
    task = scaffold_background(directed)
    task["extraction"] = "white_background"
    assert "BACKGROUND_LAYOUT_REQUIRED" in call(directed, "plan_asset", task=task)["error"]
    task["extraction"] = "none"
    task["background_layout"]["excluded_object_ids"] = ["environment"]
    assert "Exclude independently" in call(directed, "plan_asset", task=task)["error"]
    task["background_layout"]["excluded_object_ids"] = ["actor"]
    generate_plate(directed, monkeypatch, task)
    assert (
        "Re-measure"
        in call(
            directed,
            "place_background",
            asset_id="plate",
            observed_anchors={},
            layout_notes="Checking alignment after image generation",
        )["error"]
    )


def test_cover_crop_preserves_aspect():
    assert background_geometry(200, 100, 100, 100) == [50, 0, 100, 100]
    assert background_geometry(100, 200, 100, 100) == [0, 50, 100, 100]


@pytest.mark.rendering
def test_plate_renders_behind_independent_actor(directed, monkeypatch):
    task = scaffold_background(directed)
    generate_plate(directed, monkeypatch, task)
    call(
        directed,
        "place_background",
        asset_id="plate",
        observed_anchors={"seat": [32, 40]},
        layout_notes="Seat and reserved actor area verified in the generated image",
    )
    evidence = directed.renderers.preview([0])
    with Image.open(directed.store.path(evidence.paths[0])) as image:
        assert image.getpixel((1, 1))[:3] == (0, 128, 0)
        assert image.getpixel((30, 22))[:3] == (255, 0, 0)


def test_background_qwen_request_selects_landscape_size(directed, monkeypatch):
    import httpx

    from maliang.adapters import qwen

    directed.store.mutate(0, lambda data: data["spec"].update(width=128))
    task = scaffold_background(directed)
    task["target_region"] = [0, 0, 128, 64]
    assert "error" not in call(directed, "plan_asset", task=task)
    output = io.BytesIO()
    Image.new("RGB", (128, 64), "green").save(output, "PNG")
    bodies = []

    def handler(request):
        if request.method == "POST":
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"output": {"task_id": "offline-plate"}})
        if "/tasks/" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [{"url": "https://storage.example/plate.png"}],
                    }
                },
            )
        assert "authorization" not in request.headers
        return httpx.Response(200, content=output.getvalue())

    real_client = httpx.Client
    monkeypatch.setattr(
        qwen.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    assert "error" not in call(directed, "generate_asset", asset_id="plate")
    assert bodies[0]["parameters"]["size"] == "1664*928"
    assert "reserved_regions" in bodies[0]["input"]["prompt"]
    assert directed.store.load().assets[0].provenance["size"] == "1664*928"
