"""Structured scene edits, actual GPU rendering, and web integration."""

import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops, ImageStat
from playwright.sync_api import sync_playwright

import web
from maliang.agent import make_context
from maliang.editing import create_edit, existing_media
from maliang.models import Artwork, OutputSpec, Requirement
from maliang.pathtrace import PathtraceScene, engine_status
from maliang.settings import HarnessSettings
from maliang.store import ProjectStore
from maliang.verification import verify


def scene():
    return {
        "materials": [{"id": "ceramic"}, {"id": "floor", "preset": "matte"}],
        "objects": [
            {"id": "ball", "geometry": {"type": "sphere"}, "material": "ceramic", "position": [0, 0.5, 0]},
            {
                "id": "floor",
                "geometry": {"type": "plane", "size": [200, 200, 1]},
                "material": "floor",
                "rotation": [-90, 0, 0],
            },
        ],
        "render": {"preview_samples": 1, "final_samples": 2},
    }


@pytest.fixture
def ctx(tmp_path):
    store = ProjectStore(tmp_path / "scene")
    store.create(
        Artwork(
            prompt="A ceramic sphere",
            allowed_backends=["pathtrace"],
            spec=OutputSpec(width=96, height=80, format="png"),
            requirements=[Requirement(id="material", kind="visual", description="Ceramic sphere")],
        )
    )
    return make_context(store.root, plugins=False)


def invoke(ctx, name, **kw):
    return ctx.registry.invoke(name, {"expected_revision": ctx.store.load().revision, **kw})


def test_profile_and_validation():
    config = HarnessSettings(model={"name": "offline"})
    assert config.profile("pathtrace").allowed_backends == ["pathtrace"]
    assert config.profile("pathtrace").output.format == "png"
    assert not config.profile("pathtrace").allow_generated_assets
    assert engine_status()["available"]
    valid = scene()
    for change in [
        {"materials": []},
        {"objects": [valid["objects"][0], valid["objects"][0]]},
        {"camera": {"position": [0, 0, 0], "target": [0, 0, 0]}},
        {"camera": {"position": [float("nan"), 1, 0]}},
        {"render": {"preview_samples": 8, "final_samples": 1}},
        {
            "objects": [
                {
                    "id": "bad",
                    "material": "ceramic",
                    "geometry": {"type": "mesh", "vertices": [[0, 0, 0]] * 3, "indices": [0, 1, 8]},
                }
            ]
        },
        {
            "objects": [
                {
                    "id": "bad",
                    "material": "ceramic",
                    "geometry": {"type": "lathe", "profile": [[-1, 0], [1, 1]]},
                }
            ]
        },
        {"objects": [{"id": "bad", "material": "ceramic", "geometry": {"type": "tube"}}]},
        {"unknown": "ignored?"},
    ]:
        with pytest.raises(ValueError):
            PathtraceScene.model_validate({**valid, **change})


def test_edit_restore_and_reference_integrity(ctx, tmp_path):
    assert invoke(ctx, "set_pathtrace_scene", scene=scene())["revision"] == 1
    first = ctx.store.load().program
    result = invoke(
        ctx, "edit_pathtrace_scene", section="materials", item_id="ceramic", changes={"preset": "metal"}
    )
    assert result["revision"] == 2
    doc = json.loads(ctx.store.path(ctx.store.load().program.path).read_text())
    assert doc["objects"] == PathtraceScene.model_validate(scene()).model_dump(mode="json")["objects"]
    assert doc["materials"][0]["preset"] == "metal"
    rejected = invoke(ctx, "edit_pathtrace_scene", section="materials", item_id="ceramic", remove=True)
    assert rejected["status"] == "error"
    assert ctx.store.load().revision == 2
    stale = ctx.registry.invoke(
        "edit_pathtrace_scene", {"expected_revision": 1, "section": "camera", "changes": {"fov": 50}}
    )
    assert stale["status"] == "error"
    invoke(ctx, "restore_version", revision=1)
    assert ctx.store.load().program.sha256 == first.sha256
    edited = tmp_path / "edited"
    create_edit(ctx.store, ProjectStore(edited), revision=1, instruction="Make it metal", preview=False)
    copy = ProjectStore(edited).load()
    assert copy.program.backend == "pathtrace"
    assert copy.allowed_backends == ["pathtrace"]


@pytest.mark.rendering
def test_real_render_preview_final_cache_and_review(ctx):
    invoke(ctx, "set_pathtrace_scene", scene=scene())
    preview = ctx.renderers.preview([0])
    pt = preview.metadata["frame_details"][0]["pathtrace"]
    assert pt["samples"] == 1 and pt["quality"] == "preview" and pt["gpu"]
    assert ctx.renderers.preview([0]).metadata["cache_hit"]
    image = Image.open(ctx.store.path(preview.paths[0])).convert("RGB")
    assert image.size == (96, 80)
    assert max(ImageStat.Stat(image).stddev) > 10  # actual geometry, not blank output
    # Same seed and stable sampling replay the same image on this device.
    replay = ctx.renderers.preview([0], use_cache=False)
    assert (
        ImageChops.difference(image, Image.open(ctx.store.path(replay.paths[0])).convert("RGB")).getbbox()
        is None
    )
    exported = ctx.renderers.export()
    final = exported.metadata["frame_details"][0]["pathtrace"]
    assert final["samples"] == 2 and final["quality"] == "final"
    assert not exported.metadata["cache_hit"]
    assert ctx.renderers.export().id == exported.id
    assert existing_media(ctx.store, ctx.store.load().revision)[0]["path"] == exported.paths[0]
    assert not verify(ctx.store, exported.id)["eligible_to_finalize"]
    result = ctx.registry.invoke("observe_requirement", {"requirement_id": "material"})
    ev = json.loads(result[0]["text"])
    assert ev["metadata"]["pathtrace_export"] == exported.id
    assert ev["paths"] == exported.paths
    ctx.registry.invoke(
        "record_review",
        {
            "requirement_id": "material",
            "evidence_ids": [ev["id"]],
            "verdict": "pass",
            "explanation": "Inspected actual final image.",
        },
    )
    assert verify(ctx.store, exported.id)["eligible_to_finalize"]
    assert ctx.renderers.crop(exported.id, 0, [0, 0, 48, 40]).paths
    invoke(ctx, "edit_pathtrace_scene", section="render", changes={"preview_mode": "raster"})
    raster = ctx.renderers.preview([0])
    assert raster.metadata["frame_details"][0]["pathtrace"]["mode"] == "raster"
    final = ctx.renderers.export()
    assert final.metadata["frame_details"][0]["pathtrace"]["mode"] == "pathtrace"


def test_dispatch_and_missing_library(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(web, "WEB_LOGS", tmp_path / "logs")
    calls = []
    monkeypatch.setattr(
        web.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(poll=lambda: 0)
    )
    web.Runs().start("写实玻璃杯", "pathtrace")
    assert calls[0][calls[0].index("--task") + 1] == "pathtrace"
    monkeypatch.setattr(
        "maliang.pathtrace.engine_status", lambda: {"available": False, "message": "missing library"}
    )
    with pytest.raises(ValueError, match="missing library"):
        web.Runs().start("cup", "pathtrace")
    assert len(calls) == 1


@pytest.mark.rendering
def test_browser_entry_and_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=pw.chromium.executable_path)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.goto(f"http://127.0.0.1:{server.server_port}/studio?mode=pathtrace")
            assert [t.strip() for t in page.locator("nav.modules button").all_text_contents()][-2:] == [
                "笔触绘画",
                "写实渲染",
            ]
            page.wait_for_function("!document.getElementById('submit').disabled")
            assert page.locator("#pathtraceInfo").is_visible()
            assert page.locator("#paintInfo").is_hidden()
            page.locator("#prompt").fill("暖光下的陶瓷碗")
            page.locator('[data-mode="paint"]').click()
            page.locator('[data-mode="pathtrace"]').click()
            assert page.locator("#prompt").input_value() == "暖光下的陶瓷碗"
            submitted = []

            def intercept(route):
                submitted.append(route.request.post_data_json)
                route.fulfill(status=400, content_type="application/json", body='{"error":"offline test"}')

            page.route("**/api/runs", intercept)
            page.locator("#submit").click()
            page.wait_for_function("document.getElementById('error').textContent.includes('offline test')")
            assert submitted[0]["kind"] == "pathtrace"
            assert not errors
            page.screenshot(path=str(tmp_path / "pathtrace-ui.png"), full_page=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.rendering
def test_coated_ceramic_and_thick_glass_are_not_black(tmp_path):
    import runpy
    from pathlib import Path

    from maliang.adapters.renderers import PathtraceRenderer

    demo = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples/pathtrace_demo.py"))
    doc = demo["demo_scene"]()
    doc.render.preview_samples = 64
    art = Artwork(prompt="Material regression", spec=OutputSpec(width=192, height=192, format="png"))
    PathtraceRenderer().frames(art, doc.model_dump_json(), [0], tmp_path, {})
    image = Image.open(tmp_path / "000000.png").convert("RGB")
    # These interior regions were almost entirely black on the incompatible Metal path.
    for box in ([40, 80, 85, 115], [128, 72, 153, 112]):
        assert min(ImageStat.Stat(image.crop(box)).mean) > 35
