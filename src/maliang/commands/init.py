from __future__ import annotations

import json
from pathlib import Path

from ..models import Artwork, ObservationSpec, OutputSpec, Requirement
from .output import dump


def init(args, store=None, settings=None, settings_path=None):
    if args.spec and (
        args.orientation != "auto"
        or args.resolution != "profile"
        or args.width is not None
        or args.height is not None
    ):
        raise ValueError("--spec already defines dimensions; omit size options")
    if args.spec and args.input_image:
        raise ValueError("--spec cannot be combined with --input-image")
    input_data = None
    if args.input_image:
        from ..input_images import normalize_image

        input_path = Path(args.input_image)
        if input_path.stat().st_size > 10_000_000:
            raise ValueError("图像须小于 10 MB")
        input_data = normalize_image(input_path.read_bytes())
    if args.spec and args.task:
        raise ValueError("--spec already defines the output type; omit --task")
    if args.task and args.format and args.format != ("mp4" if args.task == "video" else "png"):
        raise ValueError("--format does not match --task")
    kind = args.task or ("video" if args.format == "mp4" else "image")
    profile = settings.profile(kind) if settings else None
    prompt = Path(args.prompt_file).read_text() if args.prompt_file else args.prompt
    if args.spec:
        data = json.loads(Path(args.spec).read_text())
        if prompt:
            data["prompt"] = prompt
        data["workflow_policy"] = args.workflow or data.get("workflow_policy", "guided")
        art = Artwork.model_validate(data)
    else:
        prompt = prompt or (profile.prompt if profile else None)
        if not prompt:
            raise ValueError("Supply --prompt, --prompt-file or --spec")
        output = (
            profile.output
            if profile
            else OutputSpec(width=512, height=512, format="png")
            if kind != "video"
            else OutputSpec(width=640, height=360, format="mp4")
        )
        from ..resolution import resolve_output

        art = Artwork(
            prompt=prompt,
            workflow_policy=args.workflow
            or ("guided" if not settings or settings.mode == "maliang" else "legacy"),
            spec=resolve_output(
                output,
                prompt,
                orientation=args.orientation,
                resolution=args.resolution,
                width=args.width,
                height=args.height,
                duration=args.duration if args.duration is not None else output.duration,
                fps=args.fps if args.fps is not None else output.fps,
                format=args.format or output.format,
                seed=args.seed if args.seed is not None else output.seed,
            ),
            allowed_backends=args.backends.split(",")
            if args.backends
            else list(profile.allowed_backends)
            if profile
            else [kind] if kind in {"paint", "pathtrace"} else ["scene2d", "canvas", "svg"],
            allow_generated_assets=args.allow_generated_assets
            or bool(profile and profile.allow_generated_assets),
        )
    if not art.requirements:
        art.requirements = [
            Requirement(
                id="brief_fulfillment",
                kind="visual",
                description="The actual rendered result satisfies the original user brief; inspect the original prompt in read_artwork.",
            )
        ]
    if not args.spec:
        art.requirements.append(
            Requirement(
                id="render_clarity",
                kind="temporal" if art.spec.format == "mp4" else "visual",
                observation=ObservationSpec(
                    samples=3, end=max(art.spec.duration / 2, art.spec.duration - 1 / art.spec.fps)
                )
                if art.spec.format == "mp4"
                else ObservationSpec(),
                description="Inspect final composition and native-resolution detail crops: intended readable text is legible and correct, fine lines are distinct, imported imagery is not visibly blurred by excessive enlargement. For video inspect representative frames across time and the encoded clip. Decorative illegible marks need not be readable. Do not approve merely because the output dimensions are correct.",
            )
        )
    if not args.spec and art.spec.format == "mp4":
        art.requirements.append(
            Requirement(
                id="video_motion",
                kind="temporal",
                description="The requested actions and camera changes visibly occur over time with consistent objects, appropriate pacing and no unintended freezes/jumps. Inspect action and shot boundaries plus a rendered clip; verify the loop seam when requested. A static image encoded as video is not sufficient for an animated brief.",
                observation=ObservationSpec(
                    samples=3, end=max(art.spec.duration / 2, art.spec.duration - 1 / art.spec.fps)
                ),
            )
        )
    if input_data is not None:
        from ..input_images import attach_input_image

        attach_input_image(store, art, input_data)
    store.create(art)
    dump({"project": str(store.root), "revision": 0})
    return

