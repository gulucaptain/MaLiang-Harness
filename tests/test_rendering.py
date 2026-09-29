"""Integration tests: require Playwright Chromium and PyAV, no API key."""

import json
from pathlib import Path

import pytest
from PIL import Image

from maliang.agent import make_context, run_agent
from maliang.demo import ScriptedDemoModel
from maliang.models import Artwork, OutputSpec
from maliang.store import ProjectStore

pytestmark = pytest.mark.rendering


def test_svg_backend_renders_and_canvas_uses_same_core(tmp_path):
    ProjectStore(tmp_path).create(
        Artwork(prompt="Red rectangle", spec=OutputSpec(width=64, height=64, format="png"))
    )
    ctx = make_context(tmp_path, plugins=False)
    ctx.registry.invoke(
        "write_program",
        {
            "expected_revision": 0,
            "backend": "svg",
            "source": '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="red"/></svg>',
        },
    )
    first = ctx.renderers.preview([0])
    with Image.open(ctx.store.path(first.paths[0])) as im:
        assert im.getpixel((32, 32))[:3] == (255, 0, 0)
    second = ctx.renderers.preview([0])
    assert second.metadata["cache_hit"]
    assert ctx.meter.usage["frames"] == 1
    ctx.registry.invoke(
        "write_program",
        {
            "expected_revision": 1,
            "backend": "canvas",
            "source": 'function(ctx){ctx.fillStyle="blue";ctx.fillRect(0,0,64,64)}',
        },
    )
    third = ctx.renderers.preview([0])
    with Image.open(ctx.store.path(third.paths[0])) as im:
        assert im.getpixel((32, 32))[:3] == (0, 0, 255)


def test_demo_runs_real_agent_repairs_code_and_exports_video(tmp_path):
    ProjectStore(tmp_path).create(
        Artwork(prompt="Offline demo", spec=OutputSpec(width=320, height=240, duration=1, fps=6))
    )
    ctx = make_context(tmp_path, plugins=False)
    status = run_agent(ctx, ScriptedDemoModel(project_root=str(tmp_path)))
    assert status["status"] == "completed"
    trace = [json.loads(line) for line in ctx.store.path("trace.jsonl").read_text().splitlines()]
    assert any(e["event"] == "tool_end" and e.get("status") == "error" for e in trace)
    video = ctx.store.evidence(status["export_id"])
    assert Path(ctx.store.path(video.paths[0])).stat().st_size > 1000
    assert ctx.store.load().revision == 3
    # Finalization stops the graph before a redundant ninth paid call.
    assert ctx.meter.usage["model_calls"] == 8
