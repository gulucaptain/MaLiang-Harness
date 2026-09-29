"""Offline material scene: no model or image-generation API calls."""

import argparse
import json
from pathlib import Path

from maliang.agent import make_context
from maliang.models import Artwork, OutputSpec
from maliang.pathtrace import PathtraceScene
from maliang.store import ProjectStore, atomic_json


def demo_scene():
    return PathtraceScene.model_validate(
        {
            "camera": {"position": [3.6, 2.9, 5.2], "target": [0, 0.55, 0], "fov": 37},
            "environment": {"top": "#c7d7e7", "bottom": "#a59b87", "intensity": 0.3, "background": "#ddd4c5"},
            "render": {"preview_samples": 8, "final_samples": 128, "bounces": 8},
            "lights": [
                {"id": "key", "position": [-3, 4.5, 3], "width": 3, "height": 3, "intensity": 8},
                {
                    "id": "rim",
                    "position": [2, 3, -3],
                    "width": 2,
                    "height": 3,
                    "intensity": 4,
                    "color": "#dce9ff",
                },
            ],
            "materials": [
                {"id": "ceramic", "preset": "ceramic", "color": "#e9e2d1", "roughness": 0.19},
                {"id": "steel", "preset": "metal", "roughness": 0.14},
                {"id": "glass", "preset": "glass", "roughness": 0.025},
                {"id": "table", "preset": "matte", "color": "#b3a28c", "roughness": 0.8},
            ],
            "objects": [
                {
                    "id": "bowl",
                    "material": "ceramic",
                    "position": [-0.85, 0, 0],
                    "geometry": {
                        "type": "lathe",
                        "segments": 96,
                        "profile": [
                            [0, 0.05],
                            [0.5, 0.05],
                            [0.65, 0.09],
                            [0.83, 0.25],
                            [1.02, 0.6],
                            [1.1, 0.87],
                            [1.09, 0.91],
                            [1.05, 0.91],
                            [1.02, 0.87],
                            [0.94, 0.62],
                            [0.76, 0.3],
                            [0.57, 0.18],
                            [0, 0.18],
                        ],
                    },
                },
                {
                    "id": "metal_sphere",
                    "material": "steel",
                    "position": [0.65, 0.36, 0.55],
                    "geometry": {"type": "sphere", "radius": 0.36, "segments": 64},
                },
                {
                    "id": "glass_cup",
                    "material": "glass",
                    "position": [0.8, 0, -0.65],
                    "geometry": {
                        "type": "lathe",
                        "segments": 96,
                        "profile": [
                            [0, 0.02],
                            [0.35, 0.02],
                            [0.4, 0.05],
                            [0.44, 1.3],
                            [0.43, 1.33],
                            [0.405, 1.33],
                            [0.4, 1.29],
                            [0.37, 0.13],
                            [0, 0.13],
                        ],
                    },
                },
                {
                    "id": "table",
                    "material": "table",
                    "rotation": [-90, 0, 0],
                    "geometry": {"type": "plane", "size": [200, 200, 1]},
                },
            ],
        }
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="runs/pathtrace-engine-demo")
    parser.add_argument("--size", type=int, default=512)
    args = parser.parse_args()
    store = ProjectStore(Path(args.project).resolve())
    store.create(
        Artwork(
            prompt="离线写实渲染验证：陶瓷碗、金属球、玻璃杯",
            allowed_backends=["pathtrace"],
            spec=OutputSpec(width=args.size, height=args.size, format="png"),
        )
    )
    ctx = make_context(store.root, plugins=False)
    result = ctx.registry.invoke(
        "set_pathtrace_scene", {"expected_revision": 0, "scene": demo_scene().model_dump()}
    )
    if result.get("status") == "error":
        raise RuntimeError(result)
    ctx.renderers.preview([0])
    exported = ctx.renderers.export()
    atomic_json(
        store.path("status.json"),
        {
            "status": "draft",
            "revision": store.load().revision,
            "export_id": exported.id,
            "reason": "Offline engine demonstration; no model quality assessment.",
        },
    )
    print(
        json.dumps(
            {
                "project": str(store.root),
                "image": str(store.path(exported.paths[0])),
                "render": exported.metadata,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
