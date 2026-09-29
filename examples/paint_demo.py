"""Offline native brush sample with real history/exports; never calls model APIs."""

import argparse
import math
from pathlib import Path

from maliang.agent import make_context
from maliang.models import Artwork, OutputSpec
from maliang.store import ProjectStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    args = parser.parse_args()
    store = ProjectStore(args.project)
    store.create(Artwork(
        prompt="libmypaint 离线笔刷样张：圆笔、软笔、压力细线、同层涂抹与擦除。仅验证引擎，不是模型画质评测。",
        allowed_backends=["paint"], spec=OutputSpec(width=800, height=560, format="png"),
    ))
    ctx = make_context(store.root, plugins=False)

    def call(name, **kwargs):
        result = ctx.registry.invoke(name, {"expected_revision": store.load().revision, **kwargs})
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(result["error"])
        return result

    call("init_painting", background="#f3efe5")
    for row, brush in enumerate(["round", "soft", "ink", "smudge", "eraser"]):
        y = 68 + row * 104
        strokes = []
        if brush in {"smudge", "eraser"}:
            for offset, color in [(-20, "#247e80"), (20, "#e59a43")]:
                strokes.append({"id": f"base_{row}_{offset + 20}", "color": color, "size": 48,
                                "points": [{"x": x, "y": y + offset, "pressure": 1}
                                           for x in range(90, 712, 8)]})
        points = [{"x": 90 + i * 10, "y": y + math.sin(i / 8) * 15,
                   "pressure": 0.12 + 0.85 * math.sin(math.pi * i / 62), "dt": 0.02}
                  for i in range(63)]
        strokes.append({"id": f"sample_{brush}", "brush": brush, "color": "#b44136",
                        "size": 18 if brush == "ink" else 45, "points": points})
        call("paint_strokes", layer_id="painting", strokes=strokes)
        ctx.renderers.preview([0])
    export = ctx.renderers.export()
    ctx.registry.invoke("finish_draft", {"reason": "离线脚本笔刷样张；验证实际 libmypaint 渲染及回放，不代表模型创作或超写实质量。"})
    print(store.path(export.paths[0]))


if __name__ == "__main__":
    main()
