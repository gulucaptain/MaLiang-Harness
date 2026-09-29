import json

import pytest
from PIL import Image
from test_code_first_repair import mock_white_cat

from maliang.agent import make_context
from maliang.models import Artwork, OutputSpec
from maliang.settings import ImageGenerationSettings
from maliang.store import ProjectStore


@pytest.fixture
def directed(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-test")
    ProjectStore(tmp_path).create(
        Artwork(
            prompt="A realistic cat in a code drawn room",
            workflow_policy="guided",
            allow_generated_assets=True,
            spec=OutputSpec(width=64, height=64),
            requirements=[{"id": "brief", "description": "Realistic cat in room"}],
        )
    )
    return make_context(
        tmp_path,
        image_generation=ImageGenerationSettings(enabled=True, workflow="code_directed"),
        plugins=False,
    )


def creation():
    return dict(
        backend="scene2d",
        capability_assessment="Canvas draws room; image model supplies only fine cat fur.",
        plan=["Draw room and placeholder", "Prepare cat component", "Compose and review"],
        requirements=[{"id": "brief", "description": "Realistic cat in room"}],
        checkpoints=[{"id": "final", "description": "Review complete scene", "requirement_ids": ["brief"]}],
        components=[
            dict(
                object_id=key,
                route=route,
                reason="Procedural room or detailed local fur",
                code_strategy="Canvas controls location, shape and shading",
                integration="Place in bounded layer; preserve code background",
                requirement_ids=["brief"],
            )
            for key, route in [("room", "code"), ("cat", "generated_subject")]
        ],
    )


def asset_task():
    return dict(
        asset_id="cat_raw",
        role="subject",
        requirement_ids=["brief"],
        visual_goal="Realistic isolated cat with fine fur",
        generation_reason="Fine natural fur benefits from a local asset",
        prompt="A single orange and white cat, isolated",
        code_composition="Canvas draws the room and positions the cat",
        background_handling="Extract the plain white background after inspection",
        temporal_plan="Still image; any camera motion is code controlled",
        target_object_id="cat",
        target_region=[16, 8, 48, 56],
        extraction="white_background",
    )


def scaffold(ctx):
    result = ctx.registry.invoke(
        "execute_steps",
        dict(
            expected_revision=0,
            steps=[
                {"tool": "plan_creation", "args": creation()},
                {
                    "tool": "put_object",
                    "args": dict(
                        object_id="room",
                        source='function(ctx){ctx.fillStyle="blue";ctx.fillRect(0,0,64,64);}',
                        layer=0,
                    ),
                },
                {
                    "tool": "put_object",
                    "args": dict(
                        object_id="cat",
                        source='function(ctx){ctx.fillStyle="orange";ctx.fillRect(16,8,32,48);}',
                        layer=1,
                    ),
                },
                {"tool": "plan_asset", "args": {"task": asset_task()}},
            ],
        ),
    )
    assert json.loads(result[0]["text"])["executed"] == 4
    assert ctx.store.load().revision == 4


def test_initial_routing_allows_local_asset_without_failed_draft(directed):
    bad = creation()
    bad["components"] = []
    assert (
        "CODE_PLAN_REQUIRED"
        in directed.registry.invoke("plan_creation", dict(expected_revision=0, **bad))["error"]
    )
    scaffold(directed)
    assert directed.store.reviews() == {}
    assert directed.meter.usage["asset_api_calls"] == 0
    task = asset_task()
    task.update(asset_id="entire_scene", target_region=[0, 0, 64, 64])
    assert "too much" in directed.registry.invoke("plan_asset", dict(expected_revision=4, task=task))["error"]
    task.update(asset_id="background", role="background")
    assert (
        "COMPONENT_PLAN_REQUIRED"
        in directed.registry.invoke("plan_asset", dict(expected_revision=4, task=task))["error"]
    )


def test_batch_stops_after_error_without_executing_later_paid_steps(directed):
    scaffold(directed)
    result = directed.registry.invoke(
        "execute_steps",
        dict(
            expected_revision=4,
            steps=[
                {
                    "tool": "edit_object",
                    "args": {"object_id": "missing", "reason": "Must fail before generation"},
                },
                {"tool": "generate_asset", "args": {"asset_id": "cat_raw"}},
            ],
        ),
    )
    assert json.loads(result[0]["text"])["executed"] == 1
    assert directed.meter.usage["asset_api_calls"] == 0


@pytest.mark.rendering
def test_custom_asset_composition_preserves_code_background(directed, monkeypatch):
    calls = mock_white_cat(monkeypatch)
    scaffold(directed)
    result = directed.registry.invoke(
        "execute_steps",
        dict(
            expected_revision=4,
            steps=[
                {"tool": "generate_asset", "args": {"asset_id": "cat_raw"}},
                {
                    "tool": "extract_subject",
                    "args": {"source_asset_id": "cat_raw", "asset_id": "cat_alpha", "method": "border_color"},
                },
                {
                    "tool": "compose_asset",
                    "args": {
                        "source_asset_id": "cat_raw",
                        "asset_id": "cat_alpha",
                        "source": "function(ctx,t,obj,assets){ctx.drawImage(assets.cat_alpha,16,8,32,48);}",
                    },
                },
            ],
        ),
    )
    assert json.loads(result[0]["text"])["executed"] == 3
    art = directed.store.load()
    assert art.revision == 7
    assert next(o for o in art.objects if o.id == "cat").asset_ids == ["cat_alpha"]
    ev = directed.renderers.preview([0])
    with Image.open(directed.store.path(ev.paths[0])) as im:
        assert im.getpixel((2, 2))[:3] == (0, 0, 255)
        assert im.getpixel((32, 32))[:3] != (0, 0, 255)
    assert len([call for call in calls if call.method == "POST"]) == 1


def test_one_model_decision_executes_a_group_of_code_writes(directed):
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from test_agent import TestModel

    from maliang.agent import run_agent

    class GroupModel(TestModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if any(isinstance(m, AIMessage) and m.tool_calls for m in messages):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Draft ready"))])
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "group",
                        "name": "execute_steps",
                        "args": {
                            "expected_revision": 0,
                            "steps": [
                                {"tool": "plan_creation", "args": creation()},
                                {
                                    "tool": "put_object",
                                    "args": {
                                        "object_id": "room",
                                        "source": "function(ctx){ctx.fillRect(0,0,64,64);}",
                                    },
                                },
                                {
                                    "tool": "put_object",
                                    "args": {
                                        "object_id": "cat",
                                        "source": "function(ctx){ctx.fillRect(16,8,32,48);}",
                                    },
                                },
                            ],
                        },
                    }
                ],
            )
            return ChatResult(generations=[ChatGeneration(message=message)])

    status = run_agent(directed, GroupModel())
    assert status["status"] == "incomplete"  # Prose still cannot bypass the delivery gate.
    assert directed.store.load().revision == 3
    assert directed.meter.usage["model_calls"] == 2
    assert directed.meter.usage["tool_calls"] == 4  # One group and three metered operations.


def test_conservative_policy_is_enforced_at_plan_and_before_paid_generation(directed):
    directed.image_generation.conservative_planning = True
    result = directed.registry.invoke("plan_creation", dict(expected_revision=0, **creation()))
    assert "GENERATION_JUSTIFICATION_REQUIRED" in result["error"]
    assert directed.store.load().revision == 0
    plan = creation()
    plan["components"][1].update(
        brief_evidence="realistic cat",
        code_limitation="Detailed natural fur exceeds the chosen procedural drawing approach.",
        minimal_scope="Only the isolated cat; room and layout remain code.",
    )
    result = directed.registry.invoke("plan_creation", dict(expected_revision=0, **plan))
    assert result["revision"] == 1
    assert directed.meter.usage["asset_api_calls"] == 0


def test_generation_rechecks_conservative_policy_for_existing_plan(directed):
    scaffold(directed)
    directed.image_generation.conservative_planning = True
    result = directed.registry.invoke("generate_asset", dict(expected_revision=4, asset_id="cat_raw"))
    assert "GENERATION_JUSTIFICATION_REQUIRED" in result["error"]
    assert directed.meter.usage["asset_api_calls"] == 0
