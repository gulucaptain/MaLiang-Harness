"""Native brush integration, immutable editing and offline web entry acceptance."""

import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from PIL import ImageChops
from playwright.sync_api import sync_playwright

import web
from maliang.agent import make_context
from maliang.editing import create_edit, history_preview
from maliang.models import Artwork, OutputSpec
from maliang.paint import PaintDocument, PaintLayer, PaintStroke
from maliang.paint_native import native_status, render_document
from maliang.settings import HarnessSettings
from maliang.store import ProjectStore


def stroke(id="first", **overrides):
    return {
        "id": id,
        "color": "#d02b34",
        "size": 20,
        "points": [{"x": 12, "y": 32, "pressure": 0.8}, {"x": 80, "y": 32, "pressure": 0.8}],
        **overrides,
    }


@pytest.fixture
def native():
    if not native_status()["available"]:
        pytest.skip("libmypaint 1.6 not installed")


@pytest.fixture
def ctx(tmp_path):
    store = ProjectStore(tmp_path / "painting")
    store.create(
        Artwork(
            prompt="Paint a red stroke",
            allowed_backends=["paint"],
            spec=OutputSpec(width=96, height=65, format="png"),
        )
    )
    return make_context(store.root, plugins=False)


def invoke(ctx, name, **args):
    return ctx.registry.invoke(name, {"expected_revision": ctx.store.load().revision, **args})


def test_profile_and_invalid_documents():
    settings = HarnessSettings(model={"name": "test"})
    assert settings.profile("paint").allowed_backends == ["paint"]
    assert not settings.profile("paint").allow_generated_assets
    assert settings.profile("image").allowed_backends == ["scene2d", "canvas", "svg"]
    with pytest.raises(ValueError):
        PaintStroke.model_validate(stroke(points=[{"x": float("nan"), "y": 0}, {"x": 0, "y": 0}]))
    with pytest.raises(ValueError, match="unique"):
        PaintDocument(layers=[PaintLayer(id="a", strokes=[PaintStroke(**stroke()), PaintStroke(**stroke())])])


def test_existing_backends_do_not_require_native(tmp_path, monkeypatch):
    monkeypatch.setenv("MALIANG_MYPAINT_LIBRARY", "/nonexistent/libmypaint.so")
    assert not native_status()["available"]
    store = ProjectStore(tmp_path)
    store.create(Artwork(prompt="legacy"))
    context = make_context(tmp_path, plugins=False)
    assert "paint_strokes" not in context.registry.capabilities
    assert {b["id"] for b in context.renderers.available()} == {"canvas", "scene2d", "svg"}


def test_missing_dependency_is_actionable(ctx, monkeypatch):
    monkeypatch.setenv("MALIANG_MYPAINT_LIBRARY", "/nonexistent/libmypaint.so")
    result = invoke(ctx, "init_painting")
    assert "brew install libmypaint" in result["error"]
    assert ctx.store.load().revision == 0


def test_native_render_pressure_eraser_and_replay(native):
    def draw(strokes):
        return render_document(PaintDocument(layers=[PaintLayer(id="a", strokes=strokes)]), 96, 65)

    red = PaintStroke(**stroke())
    result = draw([red])
    assert result.size == (96, 65)
    assert result.getpixel((50, 32))[0] > result.getpixel((50, 32))[1] + 50
    assert result.getpixel((95, 64)) == (255, 255, 255, 255)
    assert result.tobytes() == draw([red]).tobytes()
    thin = PaintStroke(
        **stroke(points=[{"x": 12, "y": 32, "pressure": 0.15}, {"x": 80, "y": 32, "pressure": 0.15}])
    )
    assert draw([thin]).tobytes() != result.tobytes()
    erased = draw([red, PaintStroke(**stroke(id="erase", brush="eraser", size=35))])
    assert erased.getpixel((50, 32))[1] > result.getpixel((50, 32))[1]
    soft = draw([PaintStroke(**stroke(brush="soft"))])
    assert soft.tobytes() != result.tobytes()


def test_batches_history_restore_and_edit(native, ctx, tmp_path):
    result = invoke(
        ctx,
        "execute_steps",
        steps=[
            {"tool": "init_painting", "args": {}},
            {"tool": "paint_strokes", "args": {"layer_id": "painting", "strokes": [stroke()]}},
        ],
    )
    assert json.loads(result[0]["text"])["remaining_steps"] == 0
    assert ctx.store.load().revision == 2
    first = ctx.renderers.preview([0])
    before = ctx.store.path(first.paths[0]).read_bytes()
    duplicate = invoke(ctx, "paint_strokes", layer_id="painting", strokes=[stroke()])
    assert duplicate["status"] == "error" and ctx.store.load().revision == 2
    stale = ctx.registry.invoke(
        "paint_strokes", {"expected_revision": 0, "layer_id": "painting", "strokes": [stroke(id="stale")]}
    )
    assert stale["status"] == "error" and ctx.store.load().revision == 2
    invoke(ctx, "remove_paint_strokes", stroke_ids=["first"])
    blank = ctx.renderers.preview([0])
    assert ctx.store.path(blank.paths[0]).read_bytes() != before
    restored = ctx.store.restore(2, 3)
    assert restored.revision == 4
    assert ctx.store.path(ctx.renderers.preview([0]).paths[0]).read_bytes() == before
    history = history_preview(ctx.store, 2)
    assert ctx.store.path(history["media"][0]["path"]).read_bytes() == before
    target = ProjectStore(tmp_path / "edit")
    create_edit(ctx.store, target, 2, "Add a blue stroke", preview=False)
    edited = make_context(target.root, plugins=False)
    assert "paint_strokes" in edited.registry.capabilities
    invoke(edited, "paint_strokes", layer_id="painting", strokes=[stroke(id="blue", color="#2244cc")])
    assert ctx.store.path(first.paths[0]).read_bytes() == before
    assert ctx.store.load().revision == 4
    export = edited.renderers.export()
    assert export.paths[0].endswith(".png")


def test_layers_hide_and_opacity(native):
    red = PaintLayer(id="red", strokes=[PaintStroke(**stroke())])
    blue = PaintLayer(id="blue", strokes=[PaintStroke(**stroke(id="blue", color="#2244cc"))])
    a = render_document(PaintDocument(layers=[red]), 96, 65)
    blue.visible = False
    b = render_document(PaintDocument(layers=[red, blue]), 96, 65)
    assert a.tobytes() == b.tobytes()
    blue.visible, blue.opacity = True, 0.5
    c = render_document(PaintDocument(layers=[red, blue]), 96, 65)
    assert ImageChops.difference(a, c).convert("RGB").getbbox()


def test_web_paint_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(web, "WEB_LOGS", tmp_path / "logs")
    monkeypatch.setattr("maliang.paint_native.require_library", lambda: None)
    calls = []
    monkeypatch.setattr(
        web.subprocess, "Popen", lambda cmd, **kwargs: calls.append(cmd) or SimpleNamespace(poll=lambda: None)
    )
    web.Runs().start("画苹果", "paint")
    assert calls[0][calls[0].index("--task") + 1] == "paint"
    assert "--allow-generated-assets" not in calls[0]


@pytest.mark.rendering
def test_paint_browser_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    monkeypatch.setattr("maliang.paint_native.native_status", lambda: {"available": True})
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=pw.chromium.executable_path, headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"http://127.0.0.1:{server.server_port}/studio")
            assert page.locator("nav.modules button").all_text_contents()[-2].strip() == "笔触绘画"
            page.locator('[data-mode="paint"]').click()
            page.wait_for_function("!document.getElementById('submit').disabled")
            assert page.locator("#paintInfo").is_visible()
            assert page.locator("#videoFields").is_hidden()
            page.locator("#prompt").fill("画一个苹果")
            page.locator('[data-mode="image"]').click()
            assert page.locator("#paintInfo").is_hidden()
            page.locator('[data-mode="paint"]').click()
            assert page.locator("#prompt").input_value() == "画一个苹果"
            submitted = []

            def intercept(route):
                submitted.append(route.request.post_data_json)
                route.fulfill(status=400, content_type="application/json", body='{"error":"offline test"}')

            page.route("**/api/runs", intercept)
            page.locator("#submit").click()
            page.wait_for_function("document.getElementById('error').textContent === 'offline test'")
            assert submitted[0]["kind"] == "paint"
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
