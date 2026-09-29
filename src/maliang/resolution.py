"""Resolve native canvas size once, before the immutable task is created."""

import re

from .models import OutputSpec

ORIENTATIONS = ("auto", "portrait", "landscape", "square")
RESOLUTIONS = ("profile", "standard", "high")


def resolve_output(base: OutputSpec, prompt: str, *, orientation="auto", resolution="profile", **overrides):
    if orientation not in ORIENTATIONS or resolution not in RESOLUTIONS:
        raise ValueError("Invalid orientation or resolution")
    data = base.model_dump()
    data.update({k: v for k, v in overrides.items() if v is not None})
    # Explicit pixel dimensions are authoritative, including legacy one-axis overrides.
    if overrides.get("width") is None and overrides.get("height") is None:
        if orientation == "auto":
            patterns = {
                "portrait": r"竖版|竖向|纵向|纵版|vertically[ -]oriented|vertical (?:poster|image|video|canvas)|portrait (?:orientation|format|poster|layout|video)",
                "landscape": r"横版|横向|横屏|landscape (?:orientation|format|layout|video)|horizontally[ -]oriented",
                "square": r"正方形|方形画布|square (?:canvas|image|format|poster)",
            }
            matches = [key for key, pattern in patterns.items() if re.search(pattern, prompt, re.I)]
            orientation = matches[0] if len(matches) == 1 else "auto"
        w, h = data["width"], data["height"]
        if orientation == "portrait":
            w, h = min(w, h), max(w, h)
            if w == h:
                w = round(h * 9 / 16)
        elif orientation == "landscape":
            w, h = max(w, h), min(w, h)
            if w == h:
                h = round(w * 9 / 16)
        elif orientation == "square":
            w = h = max(w, h)
        if resolution != "profile":
            edge = (
                {"standard": 2048, "high": 3072}
                if data["format"] == "png"
                else {"standard": 1280, "high": 1920}
            )[resolution]
            ratio = edge / max(w, h)
            w, h = round(w * ratio), round(h * ratio)
        if data["format"] == "mp4":
            w, h = max(32, round(w / 2) * 2), max(32, round(h / 2) * 2)
        data.update(width=w, height=h)
    return OutputSpec.model_validate(data)
