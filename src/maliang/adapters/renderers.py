from __future__ import annotations

import base64
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Protocol

import av
from PIL import Image

from ..models import Artwork, Evidence
from ..runtime import Meter
from ..store import ProjectStore, digest


class Renderer(Protocol):
    name: str
    suffix: str
    description: str
    supports_animation: bool

    def validate_source(self, source: str): ...
    def frames(
        self, artwork: Artwork, source: str, times: list[float], output: Path, assets: dict[str, str]
    ): ...


class BrowserRenderer:
    def frames(self, artwork, source, times, output, assets, quality="preview"):
        from .. import render_worker

        # No key or provider environment is passed into the rendering process.
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "PLAYWRIGHT_BROWSERS_PATH")
            if key in os.environ
        }
        executable = os.environ.get("MALIANG_CHROMIUM")
        job = {
            "backend": self.name,
            "quality": quality,
            "source": source,
            "times": times,
            "output": str(output),
            "width": artwork.spec.width,
            "height": artwork.spec.height,
            "seed": artwork.spec.seed,
            "scene": artwork.model_dump(),
            "assets": assets,
            "executable": executable,
        }
        with tempfile.TemporaryDirectory(prefix="maliang-job-") as temp:
            path = Path(temp) / "job.json"
            path.write_text(json.dumps(job))
            result = subprocess.run(
                [sys.executable, str(Path(render_worker.__file__).resolve()), str(path)],
                env=env,
                capture_output=True,
                text=True,
                timeout=600 if self.name == "pathtrace" else max(120, min(300, 30 + 10 * len(times))),
                check=False,
            )
            if result.returncode:
                raise RuntimeError(f"Render failed: {result.stdout[-3000:]} {result.stderr[-1000:]}")


class CanvasRenderer(BrowserRenderer):
    name, suffix, supports_animation = "canvas", ".js", True
    description = (
        "Custom Canvas 2D drawing and animation. Source is a function expression "
        "function(ctx,t,scene,assets,random){...}. t is seconds; scene contains objects/events/spec; "
        "assets maps IDs to decoded Images; random() is seeded identically per frame. "
        "Compute frames from absolute t; no network, imports, DOM UI or wall-clock animation."
    )

    def validate_source(self, source):
        if len(source.encode()) > 250000:
            raise ValueError("Source too large")
        if not source.strip():
            raise ValueError("Empty program")


class SceneRenderer(CanvasRenderer):
    name, suffix, supports_animation = "scene2d", ".json", True
    description = (
        "Retained 2D scene. Use put_object / edit_object / animate_object. "
        'Manifest source is {"version":1}. Each object has its own custom function '
        "function(ctx,t,object,assets,random){...}, drawing in local pixel coordinates. "
        "Use object.properties for appearance; the harness applies x/y, scale, rotation "
        "(clockwise degrees), opacity, layer order and absolute-second keyframes. "
        "Do not reset transforms or resize the canvas in object code. "
        "Object bounds are measured from layer alpha before occlusion, not a visibility claim."
    )

    def validate_source(self, source):
        data = json.loads(source)
        if data != {"version": 1}:
            raise ValueError('scene2d manifest must be {"version":1}; use object tools for content')


class SVGRenderer(BrowserRenderer):
    name, suffix, supports_animation = "svg", ".svg", False
    description = (
        "Static SVG markup. Explicit width/height. No script, foreignObject, external URLs or animation."
    )

    def validate_source(self, source):
        if len(source.encode()) > 250000:
            raise ValueError("SVG too large")
        root = ET.fromstring(source)
        if root.tag.split("}")[-1] != "svg":
            raise ValueError("Expected an SVG root")
        for node in root.iter():
            tag = node.tag.split("}")[-1].lower()
            if tag in {"script", "foreignobject", "animate", "animatetransform", "set", "style"}:
                raise ValueError(f"Unsupported SVG element: {tag}")
            for key, value in node.attrib.items():
                if key.lower().startswith("on") or "url(" in value.lower():
                    raise ValueError("Active SVG content is not supported")
                if key.split("}")[-1] == "href" and not value.startswith("#"):
                    raise ValueError(
                        "External SVG references are not supported; use asset composition backend"
                    )


class SVGAnimationRenderer(CanvasRenderer):
    name, suffix, supports_animation = "svg_animation", ".js", True
    description = (
        "Deterministic animated SVG: source is function(t,scene,assets,random){return '<svg ...>...</svg>'}. "
        "Return fresh SVG markup for absolute seconds t; width/height use scene.spec. "
        "assets contains decoded Images (use assets.ID.src for embedded image href). "
        "No scripts, CSS/SMIL animation, external URLs, foreignObject or style elements. "
        "Use explicit computed numeric attributes for every animated state."
    )


class ThreeRenderer(CanvasRenderer):
    name, suffix, supports_animation = "three", ".js", True
    description = (
        "Local three.js 0.180.0 / WebGL2. Source is function(THREE,renderer,artwork,assets,random) "
        "returning {scene: new THREE.Scene(), camera: a THREE.Camera, update:function(t){...}}. "
        "Create geometry/materials/lights once; update(t) sets ALL animated state from absolute seconds. "
        "Harness renders the returned scene/camera, controls pixel size and samples times. "
        "Use new THREE.Texture(assets.ID), set colorSpace=THREE.SRGBColorSpace and needsUpdate=true for uploaded images. "
        "No imports, CDN, external loaders, requestAnimationFrame, wall clock, audio or controls. "
        "CanvasTexture supports code-drawn text and overlays; exact native output dimensions apply."
    )


class PathtraceRenderer(BrowserRenderer):
    name, suffix, supports_animation = "pathtrace", ".json", False
    description = (
        "Local three-gpu-pathtracer / WebGL2 physical rendering, static PNG. "
        "Use set_pathtrace_scene and edit_pathtrace_scene for validated JSON scenes, not JavaScript. "
        "World coordinates Y-up; rotations are degrees. Geometry: sphere/box/rounded_box/plane/"
        "cylinder/torus/lathe/tube/extrude/mesh. Materials: ceramic/metal/glass/plastic/wood/liquid/matte. "
        "Area lights and procedural environment. Imported image IDs can be used as texture_asset. "
        "Preview defaults to 16 samples (or render.preview_mode=raster for composition checks); "
        "PNG export ALWAYS path traces at final_samples (default 128). Observe the actual final export. "
        "No animation, arbitrary shaders, external loaders or implicit image generation."
    )

    def validate_source(self, source):
        from ..pathtrace import parse_scene

        parse_scene(source)

    def frames(self, artwork, source, times, output, assets, quality="preview"):
        from ..pathtrace import engine_status, parse_scene

        status = engine_status()
        if not status["available"]:
            raise ValueError(status["message"])
        if artwork.spec.format != "png" or len(times) != 1:
            raise ValueError("Pathtrace supports one static PNG frame only")
        doc = parse_scene(source)
        for material in doc.materials:
            if material.texture_asset and material.texture_asset not in assets:
                raise ValueError(f"Unknown texture asset: {material.texture_asset}")
        super().frames(artwork, doc.model_dump_json(), times, output, assets, quality)


class PaintRenderer:
    name, suffix, supports_animation = "paint", ".json", False
    description = (
        "Native libmypaint 1.6 brush painting, static PNG. Use init_painting, "
        "set_paint_layer, paint_strokes and remove_paint_strokes; restore_version undoes batches. "
        "Persistent versioned JSON stroke document; each preview replays it in an isolated worker. "
        "No bitmap stamping, generated pixels, arbitrary code, or animation. References can be inspected. "
        "Brushes: round, soft, ink, smudge (same layer only), eraser. "
        "Plan backend=paint with route=code components; observe native details between major batches."
    )

    def validate_source(self, source):
        from ..paint import parse_document

        parse_document(source)

    def frames(self, artwork, source, times, output, assets):
        from ..paint_native import require_library

        require_library()
        if artwork.spec.format != "png" or len(times) != 1:
            raise ValueError("Paint backend supports one static PNG frame only")
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "MALIANG_MYPAINT_LIBRARY")
               if k in os.environ}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        with tempfile.TemporaryDirectory(prefix="maliang-paint-") as temp:
            job = Path(temp) / "job.json"
            job.write_text(json.dumps({"source": source, "width": artwork.spec.width,
                                       "height": artwork.spec.height, "output": str(output)}))
            result = subprocess.run([sys.executable, "-m", "maliang.paint_worker", str(job)],
                                    env=env, capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError(f"Paint render failed: {result.stderr[-2000:]}")


class RenderService:
    def __init__(self, store: ProjectStore, meter: Meter):
        self.store, self.meter = store, meter
        self.backends: dict[str, Renderer] = {}

    def register(self, renderer: Renderer):
        if renderer.name in self.backends:
            raise ValueError("Duplicate render backend")
        self.backends[renderer.name] = renderer

    def available(self):
        allowed = self.store.load().allowed_backends
        return [
            {"id": r.name, "description": r.description, "animation": r.supports_animation}
            for r in self.backends.values()
            if r.name in allowed
        ]

    def validate_program(self, backend: str, source: str):
        if backend not in self.store.load().allowed_backends or backend not in self.backends:
            raise ValueError("Backend unavailable or forbidden")
        self.backends[backend].validate_source(source)

    def _frames(
        self, times: list[float], use_cache: bool = True, omitted_object_id: str | None = None, quality: str = "preview"
    ) -> tuple[Artwork, list[str], bool]:
        art = self.store.load()
        if omitted_object_id is not None:
            if art.program is None or art.program.backend != "scene2d":
                raise ValueError("Object omission requires scene2d")
            if omitted_object_id not in {obj.id for obj in art.objects}:
                raise ValueError("Unknown object to omit")
            art = art.model_copy(
                update={"objects": [obj for obj in art.objects if obj.id != omitted_object_id]}
            )
        if not art.program:
            raise ValueError("Write a program before rendering")
        if (
            not times
            or len(times) > 3600
            or any(not math.isfinite(t) or t < 0 or t > art.spec.duration for t in times)
        ):
            raise ValueError("Invalid timestamps")
        times = [round(float(t), 9) for t in times]
        backend = self.backends[art.program.backend]
        if not backend.supports_animation and len(times) > 1:
            raise ValueError("This backend supports still images only")
        source = self.store.path(art.program.path).read_text()
        if digest(source.encode()) != art.program.sha256:
            raise ValueError("Program integrity check failed")
        assets = {}
        for asset in art.assets:
            data = self.store.path(asset.path).read_bytes()
            if digest(data) != asset.sha256:
                raise ValueError("Asset integrity check failed")
            assets[asset.id] = f"data:{asset.media_type};base64,{base64.b64encode(data).decode()}"
        backend.validate_source(source)
        if art.program.backend == "scene2d":
            drawings = {}
            for obj in art.objects:
                if not obj.draw:
                    raise ValueError(f"Unbound scene object: {obj.id}; use put_object")
                data = self.store.path(obj.draw.path).read_bytes()
                if digest(data) != obj.draw.sha256:
                    raise ValueError(f"Object source integrity failed: {obj.id}")
                drawings[obj.id] = data.decode()
            source = json.dumps({"version": 1, "drawings": drawings})
        key = digest(
            json.dumps(
                {"renderer_version": 7, "artwork": art.model_dump(), "times": times, "quality": quality if art.program.backend == "pathtrace" else "default"}, sort_keys=True
            ).encode()
        )
        folder = self.store.path(f"cache/{key}")
        paths = [f"cache/{key}/{i:06d}.png" for i in range(len(times))]
        hit = use_cache and all(self.store.path(p).is_file() for p in paths)
        if not hit:
            self.meter.consume("frames", len(times))
            with tempfile.TemporaryDirectory(prefix="maliang-frames-") as temp:
                if art.program.backend == "pathtrace":
                    backend.frames(art, source, times, Path(temp), assets, quality=quality)
                else:
                    backend.frames(art, source, times, Path(temp), assets)
                for i in range(len(times)):
                    with Image.open(Path(temp) / f"{i:06d}.png") as image:
                        if image.size != (art.spec.width, art.spec.height):
                            raise ValueError("Renderer returned incorrect dimensions")
                folder.mkdir(parents=True, exist_ok=True)
                for file in Path(temp).iterdir():
                    shutil.copy2(file, folder / file.name)
        return art, paths, hit

    def preview_without_object(self, object_id: str, timestamp: float = 0) -> Evidence:
        art, paths, hit = self._frames([timestamp], omitted_object_id=object_id)
        result = Evidence(
            id=self.store.new_id(),
            revision=art.revision,
            kind="frames",
            paths=paths,
            timestamps=[timestamp],
            metadata={"scope": "preview_without_object", "omitted_object_id": object_id, "cache_hit": hit},
        )
        self.store.add_evidence(result)
        return result

    def preview(self, times: list[float], use_cache=True) -> Evidence:
        if len(times) > 12:
            raise ValueError("Request at most 12 preview frames")
        art, paths, hit = self._frames(times, use_cache)
        evidence = Evidence(
            id=self.store.new_id(),
            revision=art.revision,
            kind="frames",
            paths=paths,
            timestamps=times,
            metadata={
                "cache_hit": hit,
                "frame_details": [
                    json.loads(self.store.path(p).with_suffix(".json").read_text())
                    if self.store.path(p).with_suffix(".json").exists()
                    else {}
                    for p in paths
                ],
            },
        )
        self.store.add_evidence(evidence)
        return evidence

    def crop(self, evidence_id: str, index: int, box: list[int]) -> Evidence:
        source = self.store.evidence(evidence_id)
        if source.revision != self.store.load().revision:
            raise ValueError("STALE_EVIDENCE")
        if source.kind not in {"frames", "crop", "export"} or not 0 <= index < len(source.paths) or not source.paths[index].endswith(".png"):
            raise ValueError("Choose an image evidence frame")
        with Image.open(self.store.path(source.paths[index])) as image:
            if len(box) != 4 or not (
                0 <= box[0] < box[2] <= image.width and 0 <= box[1] < box[3] <= image.height
            ):
                raise ValueError("Crop box must be [left,top,right,bottom] inside the image")
            eid = self.store.new_id()
            relative = f"previews/{eid}.png"
            self.store.path(relative).parent.mkdir(exist_ok=True)
            image.crop(box).save(self.store.path(relative))
        ev = Evidence(
            id=eid,
            revision=source.revision,
            kind="crop",
            paths=[relative],
            timestamps=source.timestamps[index : index + 1],
            metadata={**source.metadata, "parent": evidence_id, "box": box},
        )
        self.store.add_evidence(ev)
        return ev

    def video(self, start: float, end: float, final: bool = False) -> Evidence:
        art = self.store.load()
        if not 0 <= start < end <= art.spec.duration:
            raise ValueError("Invalid clip interval")
        if art.spec.width % 2 or art.spec.height % 2:
            raise ValueError("MP4 preview requires even output dimensions")
        cached = self.store.reusable_evidence(
            "export" if final else "clip", interval=[start, end], encoder_version=1
        )
        if cached:
            return cached
        count = max(1, math.ceil((end - start) * art.spec.fps))
        times = [start + i / art.spec.fps for i in range(count)]
        # Bound each browser invocation for longer videos and check budgets between batches.
        paths, hit = [], True
        self.store.log(
            "render_progress", {"stage": "frames", "completed": 0, "total": count, "start": start, "end": end}
        )
        for offset in range(0, count, 24):
            self.meter.check()
            batch_art, batch_paths, batch_hit = self._frames(times[offset : offset + 24])
            if batch_art.revision != art.revision:
                raise ValueError("Artwork changed while exporting")
            paths.extend(batch_paths)
            hit = hit and batch_hit
            self.store.log("render_progress", {"stage": "frames", "completed": len(paths), "total": count})
        eid = self.store.new_id()
        relative = f"{'exports' if final else 'previews/clips'}/{eid}.mp4"
        output = self.store.path(relative)
        output.parent.mkdir(parents=True, exist_ok=True)
        with av.open(str(output), mode="w") as container:
            stream = container.add_stream("libx264", rate=art.spec.fps)
            stream.width, stream.height = art.spec.width, art.spec.height
            stream.pix_fmt = "yuv420p"
            stream.options = {"crf": "18", "preset": "fast"}
            self.store.log("render_progress", {"stage": "encoding", "completed": 0, "total": count})
            for index, path in enumerate(paths):
                self.meter.check()
                with Image.open(self.store.path(path)) as image:
                    frame = av.VideoFrame.from_image(image.convert("RGB"))
                frame.pts = index
                frame.time_base = Fraction(1, art.spec.fps)
                for packet in stream.encode(frame):
                    container.mux(packet)
                if (index + 1) % 60 == 0 or index + 1 == count:
                    self.store.log(
                        "render_progress", {"stage": "encoding", "completed": index + 1, "total": count}
                    )
            for packet in stream.encode():
                container.mux(packet)
        ev = Evidence(
            id=eid,
            revision=art.revision,
            kind="export" if final else "clip",
            paths=[relative],
            timestamps=[start, end],
            metadata={"frames": count, "fps": art.spec.fps, "cache_hit": hit,
                      "interval": [start, end], "encoder_version": 1,
                      "sha256": digest(output.read_bytes())},
        )
        self.store.add_evidence(ev)
        return ev

    def inspect_video(self, evidence_id: str, samples: int = 6) -> Evidence:
        source = self.store.evidence(evidence_id)
        if source.kind not in {"clip", "export"} or not source.paths[0].endswith(".mp4"):
            raise ValueError("Expected MP4 evidence")
        if not 1 <= samples <= 12:
            raise ValueError("Use 1–12 samples")
        if source.revision != self.store.load().revision:
            raise ValueError("STALE_EVIDENCE")
        cached = self.store.reusable_evidence(
            "frames", parent=evidence_id, decoded_video=True, requested_samples=samples
        )
        if cached:
            return cached
        total = int(source.metadata["frames"])
        count = min(samples, total)
        indices = {round(i * (total - 1) / max(1, count - 1)) for i in range(count)}
        eid = self.store.new_id()
        folder = self.store.path(f"previews/{eid}")
        folder.mkdir(parents=True)
        paths, times = [], []
        start = source.timestamps[0] if source.timestamps else 0
        with av.open(str(self.store.path(source.paths[0]))) as container:
            for index, frame in enumerate(container.decode(video=0)):
                self.meter.check()
                if index in indices:
                    path = f"previews/{eid}/{index:06d}.png"
                    frame.to_image().save(self.store.path(path))
                    paths.append(path)
                    times.append(start + index / source.metadata["fps"])
                if index >= max(indices):
                    break
        if len(paths) != count:
            raise ValueError("Encoded video has fewer frames than expected")
        result = Evidence(
            id=eid,
            revision=source.revision,
            kind="frames",
            paths=paths,
            timestamps=times,
            metadata={"parent": evidence_id, "decoded_video": True, "scope": "encoded_video_samples", "requested_samples": samples},
        )
        self.store.add_evidence(result)
        return result

    def export(self) -> Evidence:
        art = self.store.load()
        if art.spec.format == "mp4":
            return self.video(0, art.spec.duration, final=True)
        image_export_version = 2 if art.program and art.program.backend == "pathtrace" else 1
        cached = self.store.reusable_evidence("export", image_export_version=image_export_version)
        if cached:
            return cached
        _, paths, hit = self._frames([0], quality="final")
        eid = self.store.new_id()
        relative = f"outputs/{eid}.png"
        self.store.path(relative).parent.mkdir(exist_ok=True)
        shutil.copy2(self.store.path(paths[0]), self.store.path(relative))
        ev = Evidence(
            id=eid, revision=art.revision, kind="export", paths=[relative],
            metadata={"cache_hit": hit, "image_export_version": image_export_version,
                      "sha256": digest(self.store.path(relative).read_bytes()),
                      "frame_details": [json.loads(self.store.path(paths[0]).with_suffix(".json").read_text())]
                      if self.store.path(paths[0]).with_suffix(".json").exists() else []}
        )
        self.store.add_evidence(ev)
        return ev
