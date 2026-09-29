import json

import pytest
from langchain_core.messages import ToolMessage
from test_backgrounds import generate_plate, scaffold_background
from test_code_directed import directed as directed
from test_guided import guided as guided
from test_guided import plan

from maliang.agent import make_context, prepare_image_messages
from maliang.generation_policy import validate_generation_plan
from maliang.models import Review
from maliang.settings import ImageGenerationSettings


def test_quote_wrappers_do_not_require_regeneration_but_invention_still_fails():
    settings = ImageGenerationSettings(conservative_planning=True)
    component = dict(
        route="generated_background",
        brief_evidence="“逼真的石头”",
        code_limitation="Natural mineral detail",
        minimal_scope="Stone and supporting ground",
    )
    validate_generation_plan(settings, "在一个逼真的石头上面，有孙悟空", [component])
    with pytest.raises(ValueError, match="immutable user brief"):
        validate_generation_plan(settings, "卡通石头", [component])


def test_request_image_dedup_preserves_distinct_images_and_all_text():
    full = {"type": "image_url", "image_url": {"url": "data:image/png;base64,FULL"}}
    crop = {"type": "image_url", "image_url": {"url": "data:image/png;base64,CROP"}}
    original = [
        ToolMessage(content=[{"type": "text", "text": "requirement A"}, full, crop], tool_call_id="a"),
        ToolMessage(content=[{"type": "text", "text": "requirement B"}, full], tool_call_id="b"),
    ]
    prepared = prepare_image_messages(original, 4)
    blocks = [block for message in prepared for block in message.content]
    assert sum(block == full for block in blocks) == 1
    assert sum(block == crop for block in blocks) == 1
    assert {"type": "text", "text": "requirement A"} in blocks
    assert {"type": "text", "text": "requirement B"} in blocks
    assert original[0].content == [{"type": "text", "text": "requirement A"}, full, crop]


def test_resume_keeps_unexecuted_code_and_never_repeats_success(directed, monkeypatch):
    task = scaffold_background(directed)
    generate_plate(directed, monkeypatch, task)
    source = 'function(ctx){ctx.fillStyle="orange";ctx.fillRect(20,10,12,12);}'
    result = directed.registry.invoke(
        "execute_steps",
        dict(
            expected_revision=5,
            steps=[
                {"tool": "put_object", "args": {"object_id": "marker", "source": "function(){}"}},
                {
                    "tool": "place_background",
                    "args": {
                        "asset_id": "plate",
                        "observed_anchors": {},
                        "layout_notes": "Layout visually checked after cover crop",
                    },
                },
                {"tool": "put_object", "args": {"object_id": "actor", "source": source, "layer": 4}},
            ],
        ),
    )
    paused = json.loads(result[0]["text"])
    assert paused["executed"] == 2 and paused["remaining_steps"] == 2
    assert directed.store.load().revision == 6
    # Resume with a newly constructed runtime: the saved source lives on disk.
    resumed = make_context(
        directed.store.root, resume=True, plugins=False, image_generation=directed.image_generation
    )
    request = dict(
        expected_revision=6,
        batch_id=paused["resume_batch_id"],
        replacement_args={"observed_anchors": {"seat": [32, 40], "extra_contact": [33, 41]}},
    )
    completed = json.loads(resumed.registry.invoke("resume_steps", request)[0]["text"])
    assert completed["resume_batch_id"] is None
    assert resumed.store.load().revision == 8
    actor = next(o for o in resumed.store.load().objects if o.id == "actor")
    assert resumed.store.path(actor.draw.path).read_text() == source
    assert resumed.meter.usage["asset_api_calls"] == 1
    assert "already been resumed" in resumed.registry.invoke("resume_steps", request)["error"]


@pytest.mark.rendering
def test_bulk_observation_keeps_crops_temporal_samples_and_review_gates(guided):
    plan(guided, temporal=True)
    guided.registry.invoke(
        "put_object",
        dict(
            expected_revision=1,
            object_id="sun",
            source='function(ctx,t){ctx.fillStyle="orange";ctx.fillRect(10+t*10,10,8,8);}',
        ),
    )
    before = guided.store.load().model_dump()
    result = guided.registry.invoke("observe_requirements", {"requirement_ids": []})
    observations = json.loads(result[0]["text"])["observations"]
    mapped = {item["requirement_id"]: item["evidence"] for item in observations}
    assert set(mapped) == {"brief", "composition", "motion"}
    assert mapped["composition"]["metadata"]["object_id"] == "sun"
    assert len(set(mapped["motion"]["timestamps"])) >= 3
    images = [json.dumps(b, sort_keys=True) for b in result[1:]]
    assert len(images) == len(set(images))
    assert guided.store.load().model_dump() == before
    with pytest.raises(ValueError, match="evidence tied"):
        guided.store.add_review(
            Review(
                requirement_id="motion",
                revision=2,
                evidence_ids=[mapped["brief"]["id"]],
                verdict="pass",
                explanation="Wrong evidence is rejected",
            )
        )
    for requirement_id, evidence in mapped.items():
        guided.store.add_review(
            Review(
                requirement_id=requirement_id,
                revision=2,
                evidence_ids=[evidence["id"]],
                verdict="pass",
                explanation="Offline test attribution only",
            )
        )
    guided.registry.invoke("put_object", dict(expected_revision=2, object_id="sun", source="function(){}"))
    with pytest.raises(ValueError, match="STALE_EVIDENCE"):
        guided.store.add_review(
            Review(
                requirement_id="brief",
                revision=3,
                evidence_ids=[mapped["brief"]["id"]],
                verdict="pass",
                explanation="Old version must not pass",
            )
        )
