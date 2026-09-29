"""Offline HTTP/browser acceptance of history selection, comparison and edit dispatch."""

import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

import web
from maliang.agent import make_context
from maliang.editing import history_preview
from maliang.models import Artwork, OutputSpec
from maliang.store import ProjectStore


@pytest.mark.rendering
def test_history_editor_browser(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    source = ProjectStore(runs / "history-fixture")
    source.create(Artwork(prompt="A circle", spec=OutputSpec(width=160, height=96, format="png")))
    for i in range(140):
        source.log("model_start", {"call": i + 1})
        source.log("model_response", {"content": "公开操作说明：" + "保留背景，调整圆形。" * 100})
        source.log("model_end", {})
    ctx = make_context(source.root, plugins=False)
    # Recorded telemetry fixture; no remote model or image API is called.
    ctx.meter.usage.update(model_calls=19, asset_api_calls=3)
    ctx.registry.invoke(
        "write_program",
        {
            "expected_revision": 0,
            "backend": "canvas",
            "source": "function(ctx){ctx.fillStyle='#102030';ctx.fillRect(0,0,160,96);ctx.fillStyle='orange';ctx.beginPath();ctx.arc(70,48,24,0,7);ctx.fill()}",
        },
    )
    ctx.registry.invoke(
        "patch_program", {"expected_revision": 1, "old_text": "'orange'", "new_text": "'cyan'"}
    )
    history_preview(source, 1)
    history_preview(source, 2)
    monkeypatch.setattr(web, "RUNS", runs)
    monkeypatch.setattr(web, "WEB_LOGS", runs / ".web-logs")
    monkeypatch.setattr(web, "RUN_MANAGER", web.Runs())
    calls = []
    live = [None]
    web.RUN_MANAGER.processes["history-fixture"] = SimpleNamespace(poll=lambda: live[0])

    class Process:
        def poll(self):
            return None

    monkeypatch.setattr(
        web,
        "subprocess",
        SimpleNamespace(
            Popen=lambda command, **kwargs: calls.append(command) or Process(), STDOUT=-2, DEVNULL=-3
        ),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=pw.chromium.executable_path, headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"http://127.0.0.1:{server.server_port}/studio?run=history-fixture")
            page.get_by_role("button", name="v1 · canvas").click()
            page.wait_for_function(
                "document.getElementById('editTarget').textContent.includes('v1') && !document.getElementById('previewVersion').disabled"
            )
            assert page.locator("#versionPreview img").count() >= 1
            assert page.locator("#llmCalls").inner_text() == "19"
            assert page.locator("#toolCalls").inner_text() == "2"
            assert page.locator("#imageGenCalls").inner_text() == "3"
            assert page.locator("#currentRevision").inner_text() == "v2"
            assert "历史 v1" in page.locator("#viewingRevision").inner_text()
            page.locator('[data-detail="code"]').click()
            assert "function(ctx)" in page.locator("#codeContent").inner_text()
            page.locator("#stepsTab").click()
            page.get_by_role("button", name="修改绘制代码", exact=False).click()
            page.wait_for_function("document.getElementById('codeContent').textContent.includes('cyan')")
            assert page.locator("#codeContent .added").count() > 0
            assert page.locator("#follow").get_attribute("aria-pressed") == "false"
            assert page.locator("#selectedMeta").inner_text().startswith("v2")
            page.locator("#versionsTab").click()
            page.get_by_role("button", name="v1 · canvas").click()
            page.wait_for_function("document.getElementById('editTarget').textContent === 'v1'")
            page.locator("#compareToggle").check()
            page.wait_for_function(
                "document.querySelectorAll('#beforeView img').length > 0 && document.querySelectorAll('#afterView img').length > 0"
            )
            assert page.locator("#selectedLabel").inner_text() == "版本 v1"
            assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
            page.locator('[data-mode="video"]').click()
            assert page.locator("#videoFields").is_visible()
            assert page.locator("#createTitle").inner_text() == "生成静音视频"
            page.locator("#prompt").fill("Video draft")
            page.locator('[data-mode="image"]').click()
            assert not page.locator("#videoFields").is_visible()
            page.locator("#prompt").fill("Image draft")
            page.locator('[data-mode="video"]').click()
            assert page.locator("#prompt").input_value() == "Video draft"
            page.locator('[data-mode="history"]').click()
            page.screenshot(path=str(tmp_path / "editor-desktop.png"), full_page=True)
            assert page.locator("#steps .step").count() == 120
            before_count = int(page.locator("#stepCount").inner_text())
            ctx.registry.invoke("read_artwork", {})
            page.wait_for_function(
                "Number(document.getElementById('stepCount').textContent) > " + str(before_count)
            )
            assert page.locator("#editTarget").inner_text() == "v1"
            assert page.locator("#editSubmit").is_disabled()
            assert page.locator("#toolCalls").inner_text() == "3"
            assert page.locator("#currentRevision").inner_text() == "v2"
            live[0] = 0
            page.wait_for_function("!document.getElementById('editSubmit').disabled")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(tmp_path / "editor-mobile.png"), full_page=True)
            page.locator("#maxOutputTokens").fill("24000")
            page.locator("#editPrompt").fill("把圆变成绿色，保持背景不变")
            page.locator("#editSubmit").click()
            page.wait_for_function("new URLSearchParams(location.search).get('run') !== 'history-fixture'")
            assert calls[0][calls[0].index("--max-output-tokens") + 1] == "24000"
            assert len(calls) == 1
            assert calls[0][calls[0].index("--revision") + 1] == "1"
            assert calls[0][calls[0].index("--prompt") + 1] == "把圆变成绿色，保持背景不变"
            new_id = page.evaluate("new URLSearchParams(location.search).get('run')")
            web.RUN_MANAGER.processes[new_id] = SimpleNamespace(poll=lambda: 0)
            page.goto(f"http://127.0.0.1:{server.server_port}/studio?run=history-fixture")
            page.wait_for_function("!document.getElementById('resume').disabled")
            page.locator("#resume").click()
            page.wait_for_function("document.getElementById('status').textContent === '运行中'")
            assert len(calls) == 2 and "--resume" in calls[1]
            assert calls[1][calls[1].index("--project") + 1] == str(source.root)
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
