"""Argument parsing and dispatch for the public MaLiang-Harness CLI."""

import argparse
import sys
from importlib import import_module
from pathlib import Path

from .settings import load_harness_settings
from .store import ProjectStore


def build_parser():
    parser = argparse.ArgumentParser(description="MaLiang-Harness visual creation harness on Deep Agents")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create an immutable brief and output specification")
    init.add_argument("project")
    init.add_argument("--prompt")
    init.add_argument("--prompt-file")
    init.add_argument("--spec", help="Artwork JSON; prompt may be overridden by --prompt")
    init.add_argument("--task", choices=["image", "video", "paint", "pathtrace"], help="Use the image, video, paint or pathtrace profile")
    init.add_argument("--harness-config", help="Harness 配置 JSON；默认读取当前目录的 harness.json")
    init.add_argument("--format", choices=["png", "mp4"])
    init.add_argument("--orientation", choices=["auto", "portrait", "landscape", "square"], default="auto")
    init.add_argument("--resolution", choices=["profile", "standard", "high"], default="profile")
    init.add_argument("--width", type=int)
    init.add_argument("--height", type=int)
    init.add_argument("--duration", type=float)
    init.add_argument("--input-image", help="PNG/JPEG/WebP reference or compositing image")
    init.add_argument("--fps", type=int)
    init.add_argument("--seed", type=int)
    init.add_argument("--backends")
    init.add_argument("--workflow", choices=["guided", "legacy"], default=None)
    init.add_argument("--allow-generated-assets", action="store_true")
    run = sub.add_parser("run", help="Run a real API-backed Deep Agent")
    run.add_argument("project")
    run.add_argument("--harness-config", help="Harness 配置 JSON；默认读取当前目录的 harness.json")
    run.add_argument("--model")
    run.add_argument("--max-output-tokens", type=int)
    run.add_argument("--mode", choices=["maliang", "generic-agent", "single-shot"])
    run.add_argument("--resume", action="store_true")
    run.add_argument("--budget", help="Budget JSON file")
    demo = sub.add_parser("demo", help="Offline scripted Deep Agents integration, not an LLM evaluation")
    demo.add_argument("project")
    demo.add_argument("--format", choices=["png", "mp4"], default="mp4")
    for name in ("inspect", "capabilities", "report"):
        sub.add_parser(name).add_argument("project")
    review = sub.add_parser("review", help="Record a human review against rendered evidence")
    review.add_argument("project")
    review.add_argument("requirement")
    review.add_argument("evidence", nargs="+")
    review.add_argument("--verdict", choices=["pass", "fail", "uncertain"], required=True)
    review.add_argument("--explanation", required=True)
    tool = sub.add_parser("tool", help="Invoke a registered capability with JSON arguments")
    tool.add_argument("project")
    tool.add_argument("name")
    tool.add_argument("--args", default="{}")
    reuse = sub.add_parser(
        "reuse", help="Reuse source objects/program/assets; preserve target brief and re-review"
    )
    reuse.add_argument("project")
    reuse.add_argument("source_project")
    sub.add_parser("doctor")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        settings = None
        settings_path = None
        if args.command in ("init", "run"):
            settings_path = Path(args.harness_config) if args.harness_config else Path("harness.json")
            settings = load_harness_settings(settings_path) if settings_path.is_file() else None
            if args.harness_config and settings is None:
                raise ValueError(f"Harness 配置文件不存在：{settings_path}")
        store = None if args.command == "doctor" else ProjectStore(args.project)
        command = "tools" if args.command in ("capabilities", "tool") else args.command
        handler = getattr(import_module(f".commands.{command}", __package__), command)
        return handler(args, store, settings, settings_path)
    except Exception as exc:
        print(f"MaLiang-Harness error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
