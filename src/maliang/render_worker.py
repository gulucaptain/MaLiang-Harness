"""Browser worker: no API credentials, no host filesystem binding, no external requests.

Run as a subprocess with a hard timeout. Browser isolation is NOT a hardened
multi-tenant sandbox; run this worker in a container for untrusted public users.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright


def run(job: dict):
    with sync_playwright() as pw:
        # Use the same installed full Chromium as inference.py, including history previews.
        options = {
            "headless": True,
            "chromium_sandbox": True,
            "executable_path": job.get("executable") or pw.chromium.executable_path,
        }
        if job["backend"] in {"three", "pathtrace"}:
            options["args"] = ["--enable-unsafe-swiftshader"]
            if job["backend"] == "pathtrace" and sys.platform == "darwin":
                # ANGLE Metal on the tested M1/Chromium combination produces black
                # clearcoat and thick glass; OpenGL retains hardware acceleration.
                options["args"] += ["--use-angle=gl", "--enable-gpu"]
        browser = pw.chromium.launch(**options)
        context = browser.new_context(
            viewport={"width": job["width"], "height": job["height"]},
            device_scale_factor=1,
            service_workers="block",
        )

        def route_request(route):
            libraries = {
                "https://maliang.invalid/" + name: name
                for name in ("three.module.min.js", "three.core.min.js", "pathtracer.module.js")
            }
            name = libraries.get(route.request.url)
            if name and job["backend"] in {"three", "pathtrace"}:
                route.fulfill(
                    body=(
                        Path(__file__).parent
                        / "vendor"
                        / ("pathtrace" if name == "pathtracer.module.js" else "three")
                        / name
                    ).read_bytes(),
                    content_type="text/javascript",
                    headers={"Access-Control-Allow-Origin": "*"},
                )
            else:
                route.abort()

        context.route("**/*", route_request)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        if job["backend"] == "pathtrace":
            page.on(
                "console", lambda message: errors.append(message.text) if message.type == "error" else None
            )
        page.set_default_timeout(10000)
        page.set_content("""<!doctype html><html><head><meta http-equiv="Content-Security-Policy"
          content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-eval' https://maliang.invalid; connect-src 'none'">
          <style>html,body{margin:0;overflow:hidden}canvas,svg{display:block}</style></head><body></body></html>""")
        page.evaluate(Path(__file__).with_name("clarity_runtime.js").read_text())
        page.evaluate(Path(__file__).with_name("video_runtime.js").read_text())
        if job["backend"] == "pathtrace":
            page.evaluate(Path(__file__).with_name("pathtrace_runtime.js").read_text())
        if job["backend"] == "svg":
            page.evaluate("svg => { document.body.innerHTML = svg; }", job["source"])
        else:
            page.evaluate(
                """async job => {
                const canvas = document.createElement('canvas');
                canvas.width=job.width; canvas.height=job.height;
                if(job.backend !== 'svg_animation') document.body.appendChild(canvas);
                window.ctx=['three','pathtrace'].includes(job.backend) ? null : canvas.getContext('2d', {willReadFrequently:true});
                window.scene=job.scene;
                window.assets={};
                for(const [id,src] of Object.entries(job.assets)) {
                    const img=new Image(); img.src=src; await img.decode(); window.assets[id]=img;
                }
                if(['three','pathtrace'].includes(job.backend)) {
                    if(job.backend === 'pathtrace') Math.random=seededRandom(job.seed);
                    const THREE=await import('https://maliang.invalid/three.module.min.js');
                    window.threeRenderer=new THREE.WebGLRenderer({canvas,antialias:true,preserveDrawingBuffer:true});
                    threeRenderer.setPixelRatio(1);threeRenderer.setSize(job.width,job.height,false);
                    if(job.backend === 'pathtrace') {
                        window.pathtraceWorld=await buildPathtraceScene(job,THREE,threeRenderer);
                        return;
                    }
                    const factory=(0,eval)('(' + job.source + ')');
                    window.threeWorld=await factory(THREE,threeRenderer,job.scene,window.assets,seededRandom(job.seed));
                    if(!threeWorld?.scene?.isScene || !threeWorld?.camera?.isCamera || typeof threeWorld.update !== 'function') throw Error('three source must return {scene,camera,update(t)}');
                } else if(job.backend === 'scene2d') {
                    window.objectDrawings={};
                    for(const [id,source] of Object.entries(JSON.parse(job.source).drawings)) {
                        window.objectDrawings[id]=(0,eval)('(' + source + ')');
                        if(typeof window.objectDrawings[id] !== 'function') throw Error('Object source must be a function expression: '+id);
                    }
                } else {
                    window.drawFrame = (0,eval)('(' + job.source + ')');
                    if(typeof window.drawFrame !== 'function') throw Error('Source must be a function expression');
                }
            }""",
                job,
            )
        if job["backend"] == "scene2d":
            page.evaluate(Path(__file__).with_name("scene_runtime.js").read_text())
        folder = Path(job["output"])
        folder.mkdir(parents=True, exist_ok=True)
        for index, t in enumerate(job["times"]):
            started = time.perf_counter()
            metadata = {}
            page.evaluate("window.resetClarity()")
            if job["backend"] == "pathtrace":
                metadata["pathtrace"] = page.evaluate("async () => await pathtraceWorld.render()")
            elif job["backend"] == "three":
                page.evaluate(
                    "async t => { await threeWorld.update(t); threeRenderer.render(threeWorld.scene,threeWorld.camera); }",
                    t,
                )
            elif job["backend"] == "svg_animation":
                page.evaluate(
                    "async args => setAnimatedSVG(await drawFrame(args.t,scene,assets,seededRandom(args.seed)))",
                    {"t": t, "seed": job["seed"]},
                )
            elif job["backend"] == "scene2d":
                metadata = page.evaluate(
                    "async args => await window.renderScene(args.t,args.seed,args.bounds)",
                    {"t": t, "seed": job["seed"], "bounds": len(job["times"]) <= 12},
                )
            elif job["backend"] == "canvas":
                page.evaluate(
                    """async ({t,seed,width,height}) => {
                    let state=seed>>>0;
                    const random=()=>{state=(1664525*state+1013904223)>>>0;return state/4294967296;};
                    ctx.canvas.width=width; ctx.canvas.height=height;
                    await window.drawFrame(ctx,t,window.scene,window.assets,random);
                }""",
                    {"t": t, "seed": job["seed"], "width": job["width"], "height": job["height"]},
                )
            if job["backend"] in {"svg", "svg_animation"}:
                page.evaluate("window.collectSvgClarity()")
            metadata["clarity"] = page.evaluate("window.clarity")
            drawn = time.perf_counter()
            if errors:
                raise RuntimeError("; ".join(errors)[:2000])
            target = folder / f"{index:06d}.png"
            if job["backend"] in {"canvas", "scene2d", "three", "pathtrace"}:
                # Read the native framebuffer; avoid compositor/font/screenshot waits.
                # Flatten transparency onto the same white background as the HTML page.
                encoded = page.evaluate(
                    "() => (window.threeRenderer ? threeRenderer.domElement : ctx.canvas).toDataURL('image/png').split(',')[1]"
                )
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as frame:
                    rgba = frame.convert("RGBA")
                    Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba).convert("RGB").save(
                        target, compress_level=1
                    )
            else:
                page.screenshot(path=str(target), animations="disabled", timeout=30000)
            metadata["timing_seconds"] = {
                "draw": round(drawn - started, 6),
                "capture_and_save": round(time.perf_counter() - drawn, 6),
            }
            (folder / f"{index:06d}.json").write_text(json.dumps(metadata))
        context.close()
        browser.close()


if __name__ == "__main__":
    try:
        run(json.loads(Path(sys.argv[1]).read_text()))
        print(json.dumps({"status": "ok"}))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)[:3000]}))
        sys.exit(1)
