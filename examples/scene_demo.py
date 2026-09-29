"""Run the retained-scene fixture through Deep Agents without an API key."""

import argparse
import json
from pathlib import Path

from maliang.agent import make_context, run_agent
from maliang.models import Artwork, OutputSpec
from maliang.scene_demo import SceneDemoModel
from maliang.store import ProjectStore, atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="runs/scene-control-demo")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    store = ProjectStore(root / args.project)
    store.create(
        Artwork(
            prompt="Offline fixture: sunset poster, moving sun, fixed mountains. Validate object control and procedural MP4.",
            workflow_policy="guided",
            allowed_backends=["scene2d"],
            spec=OutputSpec(width=640, height=360, duration=3, fps=12, format="mp4"),
            requirements=[{"id": "brief", "description": "Sunset scene with horizontal sun motion"}],
        )
    )
    atomic_json(
        store.path("run_config.json"),
        {
            "mode": "scripted-scene-demo",
            "live_llm": False,
            "quality_evaluation": "scripted fixture, not a creative quality benchmark",
        },
    )
    context = make_context(store.root, plugins=False)
    status = run_agent(context, SceneDemoModel(project_root=str(store.root)))
    print(json.dumps(status, ensure_ascii=False, indent=2))
    if status.get("export_id"):
        for path in store.evidence(status["export_id"]).paths:
            print(store.path(path))
    return 0 if status["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
