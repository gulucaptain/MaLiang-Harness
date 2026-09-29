"""Pixel and video checks for actual object control. No live model calls."""

import json

import av
import pytest
from PIL import Image

from maliang.agent import make_context
from maliang.models import Artwork, OutputSpec
from maliang.store import ProjectStore
from maliang.verification import verify

pytestmark = pytest.mark.rendering


def scene(tmp_path):
    store = ProjectStore(tmp_path)
    store.create(
        Artwork(
            prompt="Sun and mountain", spec=OutputSpec(width=64, height=64, duration=2, fps=6, format="mp4")
        )
    )
    ctx = make_context(tmp_path, plugins=False)

    def put(id, source, layer, transform=None):
        result = ctx.registry.invoke(
            "put_object",
            {
                "expected_revision": ctx.store.load().revision,
                "object_id": id,
                "source": source,
                "layer": layer,
                "transform": transform or {},
            },
        )
        assert result.get("status") != "error", result

    put("background", 'function(ctx){ctx.fillStyle="white";ctx.fillRect(0,0,64,64)}', 0)
    put(
        "sun",
        'function(ctx,t,o){ctx.fillStyle=o.properties.color||"orange";ctx.fillRect(0,0,8,8)}',
        1,
        {"x": 8, "y": 8},
    )
    put("mountain", 'function(ctx){ctx.fillStyle="blue";ctx.fillRect(0,48,64,16)}', 2)
    return ctx


def test_object_edit_changes_pixels_preserves_other_object_and_source(tmp_path):
    ctx = scene(tmp_path)
    before = ctx.renderers.preview([0])
    source = next(o.draw.sha256 for o in ctx.store.load().objects if o.id == "sun")
    result = ctx.registry.invoke(
        "edit_object",
        {"expected_revision": 3, "object_id": "sun", "transform": {"x": 32}, "reason": "Move sun right"},
    )
    assert result["revision"] == 4
    after = ctx.renderers.preview([0.0])
    assert source == next(o.draw.sha256 for o in ctx.store.load().objects if o.id == "sun")
    with Image.open(ctx.store.path(before.paths[0])) as a, Image.open(ctx.store.path(after.paths[0])) as b:
        assert a.getpixel((10, 10))[:3] == (255, 165, 0)
        assert b.getpixel((10, 10))[:3] == (255, 255, 255)
        assert b.getpixel((34, 10))[:3] == (255, 165, 0)
        assert a.crop((0, 48, 64, 64)).tobytes() == b.crop((0, 48, 64, 64)).tobytes()
    assert after.metadata["frame_details"][0]["object_bounds"]["sun"] == [32, 8, 40, 16]
    comparison = ctx.registry.invoke(
        "compare_versions",
        {"before_evidence_id": before.id, "after_evidence_id": after.id, "preserve_region": [0, 48, 64, 64]},
    )
    assert json.loads(comparison[0]["text"])["metadata"]["metrics"][0]["preserve_region_unchanged"]
    again = ctx.renderers.preview([0])
    assert again.metadata["cache_hit"]


def test_keyframes_change_pixels_and_video_is_decodable(tmp_path):
    ctx = scene(tmp_path)
    result = ctx.registry.invoke(
        "animate_object",
        {
            "expected_revision": 3,
            "object_id": "sun",
            "reason": "Move across sky",
            "tracks": [{"property": "x", "keyframes": [{"time": 0, "value": 8}, {"time": 2, "value": 40}]}],
        },
    )
    assert result["revision"] == 4
    frames = ctx.renderers.preview([0, 1, 2])
    assert [x["object_bounds"]["sun"][0] for x in frames.metadata["frame_details"]] == [8, 24, 40]
    export = ctx.renderers.export()
    assert verify(ctx.store, export.id)["eligible_to_finalize"]
    with av.open(str(ctx.store.path(export.paths[0]))) as video:
        decoded = list(video.decode(video=0))
        assert len(decoded) == 12
        assert decoded[0].to_image().tobytes() != decoded[-1].to_image().tobytes()


def test_object_requirement_crops_actual_bound_area(tmp_path):
    ctx = scene(tmp_path)
    ctx.registry.invoke(
        "add_requirements",
        {
            "expected_revision": 3,
            "requirements": [
                {"id": "sun_color", "description": "Orange sun", "observation": {"object_id": "sun"}}
            ],
        },
    )
    result = ctx.registry.invoke("observe_requirement", {"requirement_id": "sun_color"})
    evidence = json.loads(result[0]["text"])
    with Image.open(ctx.store.path(evidence["paths"][0])) as image:
        assert image.size == (8, 8)
        assert image.getpixel((0, 0))[:3] == (255, 165, 0)
    assert any(block["type"] == "image_url" for block in result)


def test_layer_order_and_opacity_are_applied_by_harness(tmp_path):
    ctx = scene(tmp_path)
    ctx.registry.invoke(
        "edit_object",
        {
            "expected_revision": 3,
            "object_id": "sun",
            "transform": {"y": 50, "opacity": 0.5},
            "layer": 3,
            "reason": "Overlap mountain",
        },
    )
    frame = ctx.renderers.preview([0])
    with Image.open(ctx.store.path(frame.paths[0])) as image:
        r, g, b = image.getpixel((10, 52))[:3]
        assert 125 <= r <= 130 and 80 <= g <= 85 and 125 <= b <= 130


def test_single_shot_scene2d_accepts_object_programs(tmp_path):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from maliang.agent import run_single_shot

    class OneShot(BaseChatModel):
        @property
        def _llm_type(self):
            return "scene-single-shot-fixture"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content=json.dumps(
                                {
                                    "backend": "scene2d",
                                    "source": '{"version":1}',
                                    "objects": [
                                        {
                                            "object_id": "background",
                                            "source": 'function(ctx){ctx.fillStyle="red";ctx.fillRect(0,0,64,64)}',
                                        }
                                    ],
                                }
                            )
                        )
                    )
                ]
            )

    store = ProjectStore(tmp_path)
    store.create(
        Artwork(
            prompt="Red square",
            spec=OutputSpec(width=64, height=64, format="png"),
            allowed_backends=["scene2d"],
        )
    )
    ctx = make_context(tmp_path, mode="single-shot", plugins=False)
    status = run_single_shot(ctx, OneShot())
    assert status["status"] == "baseline_exported"
    assert ctx.meter.usage["model_calls"] == 1
    with Image.open(store.path(store.evidence(status["export_id"]).paths[0])) as image:
        assert image.getpixel((32, 32))[:3] == (255, 0, 0)


def test_filtered_scene_matches_preview_and_video_batches(tmp_path):
    store = ProjectStore(tmp_path)
    store.create(Artwork(prompt="Filtered motion", spec=OutputSpec(
        width=64, height=64, duration=1, fps=25, format="mp4")))
    ctx = make_context(tmp_path, plugins=False)
    result = ctx.registry.invoke("put_object", {
        "expected_revision": 0, "object_id": "glow", "layer": 0,
        "source": """function(ctx,t){
          ctx.filter='blur(1px)';ctx.shadowBlur=5;ctx.shadowColor='blue';
          ctx.fillStyle='red';ctx.fillRect(8+t*20,16,20,20);
        }""",
    })
    assert result.get("status") != "error", result
    preview = ctx.renderers.preview([0])
    # The old >12-frame path switched raster backends, changing blur pixels.
    _, paths, _ = ctx.renderers._frames([0] * 13)
    with Image.open(store.path(preview.paths[0])) as still:
        with Image.open(store.path(paths[0])) as batch:
            assert still.tobytes() == batch.tobytes()
    detail = preview.metadata["frame_details"][0]
    assert detail["object_bounds"]["glow"] is not None
    assert detail["timing_seconds"]["draw"] > 0
    exported = ctx.renderers.export()
    with av.open(str(store.path(exported.paths[0]))) as video:
        frames = list(video.decode(video=0))
        assert len(frames) == 25
        assert [float(frame.time) for frame in frames] == pytest.approx([i / 25 for i in range(25)])
        assert frames[0].to_image().tobytes() != frames[-1].to_image().tobytes()
    events = [json.loads(line) for line in store.path('trace.jsonl').read_text().splitlines()]
    progress = [event['completed'] for event in events
                if event['event'] == 'render_progress' and event['stage'] == 'frames']
    assert progress == [0, 24, 25]
