from __future__ import annotations

import json

import av
from PIL import Image

from .store import ProjectStore, digest


def latest_export(store):
    revision = store.load().revision
    exports = []
    for p in store.root.glob("evidence/*.json"):
        data = json.loads(p.read_text())
        if data.get("kind") == "export" and data.get("revision") == revision:
            exports.append((p.stat().st_mtime_ns, data["id"]))
    return max(exports)[1] if exports else None


def verify(store: ProjectStore, export_id: str | None = None) -> dict:
    art = store.load()
    checks = []

    def add(name, passed, detail, level="technical"):
        checks.append({"check": name, "passed": bool(passed), "detail": detail, "level": level})

    add("program_exists", art.program is not None, "A saved source program is required")
    if art.program:
        path = store.path(art.program.path)
        add(
            "program_integrity",
            path.exists() and digest(path.read_bytes()) == art.program.sha256,
            "Immutable source hash",
        )
    if art.program and art.program.backend == "scene2d":
        add("scene_has_objects", bool(art.objects), "At least one bound drawable object")
        for obj in art.objects:
            add(f"object_binding:{obj.id}", obj.draw is not None, "Object bound to a draw function")
            if obj.draw:
                path = store.path(obj.draw.path)
                add(
                    f"object_integrity:{obj.id}",
                    path.exists() and digest(path.read_bytes()) == obj.draw.sha256,
                    "Object source hash",
                )
    if art.workflow_policy == "guided":
        add(
            "creation_plan",
            bool(art.capability_assessment and art.checkpoints),
            "Capability assessment and checkpoints required",
        )
        add(
            "planned_backend",
            bool(art.program and art.program.backend == art.planned_backend),
            "Execution follows assessed backend",
        )
        covered = {r for c in art.checkpoints for r in c.requirement_ids}
        add(
            "requirements_covered",
            {
                r.id
                for r in art.requirements
                if r.hard and not (art.spec.format == "mp4" and r.required_capability == "audio_track")
            }
            <= covered,
            "Every hard requirement belongs to a checkpoint",
        )
        for checkpoint in art.checkpoints:
            file = store.path(f"checkpoints/{checkpoint.id}.json")
            record = json.loads(file.read_text()) if file.exists() else {}
            add(
                f"checkpoint:{checkpoint.id}",
                record.get("revision") == art.revision,
                "Checkpoint verified at current revision",
            )
    for asset in art.assets:
        path = store.path(asset.path)
        add(
            f"asset:{asset.id}",
            path.exists() and digest(path.read_bytes()) == asset.sha256,
            "Immutable asset hash",
        )
    reviews = store.reviews()
    objects, events = {o.id: o for o in art.objects}, {e.id: e for e in art.events}
    requirements = []
    for req in art.requirements:
        verdict, source, detail = "unreviewed", "none", "Requires current visual evidence"
        ignored_audio = art.spec.format == "mp4" and req.required_capability == "audio_track"
        if ignored_audio:
            verdict, source, detail = (
                "ignored",
                "policy",
                "Silent video policy: audio requests are excluded, not fulfilled.",
            )
        elif req.required_capability == "audio_track":
            verdict, source, detail = (
                "fail",
                "capability",
                "Audio-track synthesis/export is not implemented; deliver a visual draft with finish_draft.",
            )
        elif req.kind == "object_exists":
            passed = bool(req.targets) and all(target in objects for target in req.targets)
            verdict, source, detail = (
                ("pass" if passed else "fail"),
                "programmatic",
                "Declaration only; not visibility",
            )
        elif req.kind == "event_order":
            passed = len(req.targets) >= 2 and all(target in events for target in req.targets)
            if passed:
                passed = all(events[a].end <= events[b].start for a, b in zip(req.targets, req.targets[1:]))
            verdict, source, detail = (
                ("pass" if passed else "fail"),
                "programmatic",
                "Timeline declaration; not visual timing",
            )
        elif req.id in reviews:
            review = reviews[req.id]
            verdict, source, detail = review.verdict, review.source, review.explanation
        requirements.append(
            {
                "id": req.id,
                "hard": req.hard and not ignored_audio,
                "verdict": verdict,
                "source": source,
                "detail": detail,
            }
        )
    if export_id:
        ev = store.evidence(export_id)
        add(
            "current_export",
            ev.kind == "export" and ev.revision == art.revision,
            "Export must belong to current revision",
        )
        if art.program and art.program.backend == "pathtrace":
            details = ev.metadata.get("frame_details", [{}])
            pt = details[0].get("pathtrace", {}) if details else {}
            add(
                "pathtrace_final_samples",
                pt.get("mode") == "pathtrace"
                and pt.get("quality") == "final"
                and pt.get("samples", 0) >= pt.get("requested_samples", 1),
                "Final export must complete requested path tracing samples",
            )
            for req in art.requirements:
                if not req.hard or req.kind in {"object_exists", "event_order"}:
                    continue
                review = reviews.get(req.id)
                observed_final = bool(
                    review
                    and any(
                        store.evidence(eid).metadata.get("pathtrace_export") == export_id
                        or eid == export_id
                        or store.evidence(eid).metadata.get("parent") == export_id
                        for eid in review.evidence_ids
                    )
                )
                add(
                    f"final_image_review:{req.id}",
                    observed_final,
                    "After export, observe_requirements and review the final PNG before finalizing",
                )
        file = store.path(ev.paths[0])
        if art.spec.format == "png":
            try:
                with Image.open(file) as image:
                    add("dimensions", image.size == (art.spec.width, art.spec.height), str(image.size))
                    add("format", image.format == "PNG", str(image.format))
            except Exception as exc:
                add("decode", False, str(exc))
        else:
            try:
                with av.open(str(file)) as container:
                    videos = list(container.streams.video)
                    add("video_stream", bool(videos), "Decoded video stream")
                    if videos:
                        stream = videos[0]
                        add(
                            "dimensions",
                            (stream.width, stream.height) == (art.spec.width, art.spec.height),
                            f"{stream.width}x{stream.height}",
                        )
                        fps = float(stream.average_rate or 0)
                        duration = (
                            float(stream.duration * stream.time_base) if stream.duration is not None else 0
                        )
                        add("fps", abs(fps - art.spec.fps) < 0.01, str(fps))
                        add(
                            "duration",
                            abs(duration - art.spec.duration) <= 1 / art.spec.fps + 0.01,
                            str(duration),
                        )
                        decoded = sum(1 for _ in container.decode(video=0))
                        import math

                        add(
                            "decoded_frames",
                            decoded == math.ceil(art.spec.duration * art.spec.fps),
                            str(decoded),
                        )
            except (av.FFmpegError, OSError, ValueError) as exc:
                add("video_decode", False, str(exc))
    passed = all(c["passed"] for c in checks) and all(
        r["verdict"] == "pass" for r in requirements if r["hard"]
    )
    return {
        "revision": art.revision,
        "technical_checks": checks,
        "requirements": requirements,
        "eligible_to_finalize": passed and export_id is not None,
        "note": "Model reviews are self-assessments, not independent human quality ratings.",
    }
