import base64
import io
import json
import sys
from pathlib import Path

import av
import pytest
from PIL import Image, ImageChops

from maliang.agent import make_context
from maliang.input_images import attach_input_image, normalize_image
from maliang.models import Artwork, OutputSpec, VideoPlan
from maliang.store import ProjectStore


def png_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (64, 64), "navy").save(stream, "PNG")
    return stream.getvalue()


def test_input_validation_and_init(tmp_path, monkeypatch):
    from maliang import cli

    source = tmp_path / "photo.png"
    source.write_bytes(png_bytes())
    project = tmp_path / "task"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "maliang",
            "init",
            str(project),
            "--task",
            "video",
            "--prompt",
            "Animate my photo",
            "--duration",
            "7",
            "--fps",
            "24",
            "--input-image",
            str(source),
        ],
    )
    cli.main()
    art = ProjectStore(project).load()
    assert (art.spec.duration, art.spec.fps) == (7, 24)
    assert art.assets[0].id == "input_image"
    assert art.assets[0].provenance["source"] == "user_upload"
    assert ProjectStore(project).path(art.assets[0].path).is_file()
    assert {"render_clarity", "video_motion"} <= {r.id for r in art.requirements}
    assert {"canvas", "scene2d", "svg_animation", "three"} <= set(art.allowed_backends)
    for data in (b"not an image", b"<svg></svg>"):
        with pytest.raises(ValueError):
            normalize_image(data)


def test_video_plan_covers_duration():
    plan = VideoPlan(
        mode="procedural",
        continuity="Seeded seamless cycle",
        loop=True,
        shots=[{"id": "main", "start": 0, "end": 4, "action": "rotate"}],
    )
    Artwork(prompt="Loop", spec=OutputSpec(duration=4), video_plan=plan)
    with pytest.raises(ValueError, match="entire output duration"):
        Artwork(prompt="Loop", spec=OutputSpec(duration=3), video_plan=plan)
    with pytest.raises(ValueError, match="contiguous"):
        Artwork(
            prompt="Loop",
            spec=OutputSpec(duration=4),
            video_plan=plan.model_copy(update={"shots": [plan.shots[0].model_copy(update={"start": 1})]}),
        )


SOURCES = {
    "canvas": "function(ctx,t,scene,assets){ctx.drawImage(assets.input_image,0,0,64,64);ctx.fillStyle='red';ctx.fillRect(5+30*motion.cycle(t,2),20,12,12);}",
    "svg_animation": "function(t,scene,assets){return `<svg xmlns='http://www.w3.org/2000/svg' width='64' height='64'><image href='${assets.input_image.src}' width='64' height='64'/><rect x='${5+30*motion.cycle(t,2)}' y='20' width='12' height='12' fill='red'/></svg>`;}",
    "three": """function(THREE,renderer,artwork,assets){
 const scene=new THREE.Scene();const photo=new THREE.Texture(assets.input_image);photo.needsUpdate=true;scene.background=photo;
 const camera=new THREE.OrthographicCamera(-2,2,2,-2,.1,10);camera.position.z=4;
 const mesh=new THREE.Mesh(new THREE.BoxGeometry(1,1,1),new THREE.MeshBasicMaterial({color:'red'}));scene.add(mesh);
 return {scene,camera,update(t){mesh.position.x=-1+2*motion.cycle(t,2);mesh.rotation.z=t;}};
 }""",
}


@pytest.mark.rendering
@pytest.mark.parametrize("backend", SOURCES)
def test_code_video_backends_motion_seek_and_encoding(tmp_path, backend):
    store = ProjectStore(tmp_path)
    art = Artwork(
        prompt="Animate", spec=OutputSpec(width=64, height=64, duration=2, fps=6), allowed_backends=[backend]
    )
    attach_input_image(store, art, normalize_image(png_bytes()))
    store.create(art)
    ctx = make_context(tmp_path, plugins=False)
    result = ctx.registry.invoke(
        "write_program", {"expected_revision": 0, "backend": backend, "source": SOURCES[backend]}
    )
    assert result.get("status") != "error", result
    preview = ctx.renderers.preview([0, 1, 0])
    frames = [Image.open(store.path(p)).convert("RGB") for p in preview.paths]
    if backend != "three":
        assert frames[0].getpixel((63, 63)) == (0, 0, 128)
    assert ImageChops.difference(frames[0], frames[1]).getbbox()
    assert not ImageChops.difference(frames[0], frames[2]).getbbox()
    exported = ctx.renderers.export()
    with av.open(str(store.path(exported.paths[0]))) as movie:
        stream = movie.streams.video[0]
        assert (stream.width, stream.height) == (64, 64)
        assert stream.average_rate == 6
        assert sum(1 for _ in movie.decode(video=0)) == 12
    decoded = ctx.renderers.inspect_video(exported.id, 3)
    assert decoded.metadata["decoded_video"]
    assert decoded.timestamps == [0, 1, 11 / 6]
    assert ctx.meter.usage["frames"] == 15
    events = [json.loads(line) for line in store.path("trace.jsonl").read_text().splitlines()]
    assert any(
        e["event"] == "render_progress" and e["stage"] == "encoding" and e["completed"] == 12 for e in events
    )


@pytest.mark.rendering
def test_animated_svg_rejects_external_content(tmp_path):
    store = ProjectStore(tmp_path)
    store.create(
        Artwork(
            prompt="SVG",
            spec=OutputSpec(width=64, height=64, format="png"),
            allowed_backends=["svg_animation"],
        )
    )
    ctx = make_context(tmp_path, plugins=False)
    ctx.registry.invoke(
        "write_program",
        {
            "expected_revision": 0,
            "backend": "svg_animation",
            "source": 'function(){return \'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/x.png"/></svg>\'}',
        },
    )
    with pytest.raises(RuntimeError, match="External SVG"):
        ctx.renderers.preview([0])


def test_web_upload_stages_outside_project_and_forwards_video_options(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import web

    calls = []
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(web, "WEB_LOGS", tmp_path / "runs/.web-logs")
    monkeypatch.setattr(
        web,
        "subprocess",
        SimpleNamespace(
            DEVNULL=-3,
            STDOUT=-2,
            Popen=lambda command, **kw: calls.append(command) or SimpleNamespace(poll=lambda: 0),
        ),
    )
    manager = web.Runs()
    run = manager.start(
        "animate photo", "video", duration=12, fps=24, image=base64.b64encode(png_bytes()).decode()
    )
    cmd = calls[0]
    assert cmd[cmd.index("--duration") + 1] == "12"
    assert cmd[cmd.index("--fps") + 1] == "24"
    assert Path(cmd[cmd.index("--input-image") + 1]).is_file()
    assert not (web.RUNS / run).exists()
    for kwargs in (
        {"duration": float("nan")},
        {"duration": 0},
        {"fps": True},
        {"image": "invalid"},
        {"duration": 121},
    ):
        with pytest.raises(ValueError):
            manager.start("test", "video", **kwargs)


@pytest.mark.rendering
def test_browser_video_upload_form_and_media_ranges(tmp_path, monkeypatch):
    import threading
    import urllib.request
    from types import SimpleNamespace

    from playwright.sync_api import sync_playwright

    import web

    calls = []
    runs = tmp_path / "runs"
    monkeypatch.setattr(web, "RUNS", runs)
    monkeypatch.setattr(web, "WEB_LOGS", runs / ".web-logs")
    monkeypatch.setattr(web, "RUN_MANAGER", web.Runs())

    def launch(command, **kwargs):
        calls.append(command)
        project = Path(command[command.index("--project") + 1])
        duration = float(command[command.index("--duration") + 1])
        fps = int(command[command.index("--fps") + 1])
        art = Artwork(
            prompt="Uploaded UI test", spec=OutputSpec(width=64, height=64, duration=duration, fps=fps)
        )
        store = ProjectStore(project)
        attach_input_image(store, art, Path(command[command.index("--input-image") + 1]).read_bytes())
        store.create(art)
        (project / "status.json").write_text('{"status":"completed","revision":0}')
        (project / "outputs").mkdir()
        (project / "outputs/range.mp4").write_bytes(b"0123456789")
        return SimpleNamespace(poll=lambda: 0)

    monkeypatch.setattr(web, "subprocess", SimpleNamespace(Popen=launch, DEVNULL=-3, STDOUT=-2))
    server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=pw.chromium.executable_path)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/studio")
            page.locator('[data-mode="video"]').click()
            assert page.locator("#duration").is_visible()
            page.locator("#duration").fill("7")
            page.locator("#fps").select_option("24")
            page.locator("#prompt").fill("在图像上添加移动箭头")
            page.locator("#inputImage").set_input_files(
                {"name": "reference.png", "mimeType": "image/png", "buffer": png_bytes()}
            )
            page.locator("#submit").click()
            page.wait_for_function(
                "document.getElementById('status').textContent === '已完成' && document.querySelector('#assets img')"
            )
            assert "7 秒" in page.locator("#outputSpec").inner_text()
            assert page.locator("#assets img").count() == 1
            assert not errors
            page.screenshot(path=str(tmp_path / "video-ui.png"), full_page=True)
            assert len(calls) == 1
            project = Path(calls[0][calls[0].index("--project") + 1])
            url = base + f"/media?run={project.name}&path=outputs/range.mp4"
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"Range": "bytes=2-5"})
            ) as response:
                assert response.status == 206
                assert response.headers["Content-Range"] == "bytes 2-5/10"
                assert response.read() == b"2345"
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_uploaded_image_reaches_model_without_generation_permission(tmp_path):
    import runpy

    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from maliang.agent import run_agent

    base = runpy.run_path(str(Path(__file__).with_name("test_agent.py")))["TestModel"]

    class VisionModel(base):
        def _generate(self, messages, **kwargs):
            content = next(message.content for message in messages if isinstance(message, HumanMessage))
            assert any(block.get("type") == "image_url" for block in content)
            assert any("input_image" in block.get("text", "") for block in content)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Inspected input"))])

    store = ProjectStore(tmp_path)
    art = Artwork(prompt="Describe my image", allow_generated_assets=False)
    attach_input_image(store, art, normalize_image(png_bytes()))
    store.create(art)
    context = make_context(tmp_path, plugins=False)
    assert run_agent(context, VisionModel())["status"] == "incomplete"
    assert context.meter.usage["model_calls"] == 1
