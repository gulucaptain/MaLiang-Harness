import pytest
from PIL import Image

from maliang.models import OutputSpec
from maliang.resolution import resolve_output


def test_orientation_and_native_resolution():
    base = OutputSpec(width=2048, height=1152, format="png")
    result = resolve_output(base, "A vertically oriented architectural infographic")
    assert (result.width, result.height) == (1152, 2048)
    assert resolve_output(base, "a portrait of a woman").width == 2048
    assert resolve_output(base, "竖版海报", resolution="high").height == 3072
    assert resolve_output(base, "竖版海报", orientation="landscape").width == 2048
    assert resolve_output(base, "正方形海报").height == 2048


def test_explicit_pixels_preserved_and_video_limits():
    base = OutputSpec(width=2048, height=1152, format="png")
    assert resolve_output(base, "竖版", width=640, resolution="high").height == 1152
    video = OutputSpec(width=1280, height=720, format="mp4")
    result = resolve_output(video, "竖版", resolution="high")
    assert (result.width, result.height) == (1080, 1920)
    OutputSpec(width=4096, height=4096, format="png")
    with pytest.raises(ValueError, match="1920"):
        OutputSpec(width=2048, height=1152, format="mp4")


@pytest.mark.rendering
@pytest.mark.parametrize("backend", ["canvas", "scene2d", "svg"])
def test_native_export_and_transformed_text_diagnostics(tmp_path, backend):
    import json
    import os

    from maliang.render_worker import run

    source = "function(ctx){ctx.scale(.5,.5);ctx.font='16px serif';ctx.fillText('Tiny label',8,32);ctx.scale(2,2);ctx.font='20px serif';ctx.fillText('Clear',4,48);}"
    scene = {"spec": {"width": 2048, "height": 64}, "objects": []}
    if backend == "scene2d":
        source = json.dumps({"drawings": {"label": source}})
        scene["objects"] = [
            {
                "id": "label",
                "draw": True,
                "layer": 0,
                "start": 0,
                "end": None,
                "motion": [],
                "transform": {"x": 0, "y": 0, "rotation": 0, "scale_x": 1, "scale_y": 1, "opacity": 1},
            }
        ]
    if backend == "svg":
        source = '<svg xmlns="http://www.w3.org/2000/svg" width="2048" height="64"><text x="8" y="32" font-size="16" transform="scale(.5)">Tiny label</text><text x="4" y="48" font-size="20">Clear</text></svg>'
    run(
        {
            "backend": backend,
            "source": source,
            "width": 2048,
            "height": 64,
            "scene": scene,
            "assets": {},
            "output": str(tmp_path),
            "times": [0, 1],
            "seed": 42,
            "executable": os.environ.get("MALIANG_CHROMIUM"),
        }
    )
    assert Image.open(tmp_path / "000000.png").size == (2048, 64)
    for index in range(2):
        metrics = json.loads((tmp_path / f"{index:06d}.json").read_text())["clarity"]
        assert metrics["text_calls"] == 2
        assert metrics["tiny_text_calls"] == 1
        assert metrics["min_text_px"] == pytest.approx(8)
