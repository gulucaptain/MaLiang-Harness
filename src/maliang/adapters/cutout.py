"""Conservative white-backdrop cutout for isolated generated subjects."""

from __future__ import annotations

import io
from collections import deque

from PIL import Image, ImageFilter


def cutout_white_background(data: bytes, *, threshold: int = 238) -> tuple[bytes, dict]:
    with Image.open(io.BytesIO(data)) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    if width < 32 or height < 32 or width * height > 16_000_000:
        raise ValueError("Unsupported cutout dimensions")
    rgb = image.tobytes()
    total = width * height
    visited = bytearray(total)
    queue = deque()

    def white(index):
        offset = index * 3
        return rgb[offset] >= threshold and rgb[offset + 1] >= threshold and rgb[offset + 2] >= threshold

    border = set(range(width)) | set(range((height - 1) * width, total))
    border.update(row * width for row in range(height))
    border.update(row * width + width - 1 for row in range(height))
    seeds = [index for index in border if white(index)]
    if len(seeds) / len(border) < 0.75:
        raise ValueError("WHITE_BACKGROUND_UNCERTAIN: less than 75% of the border is near white")
    for index in seeds:
        visited[index] = 1
        queue.append(index)
    while queue:
        index = queue.popleft()
        x = index % width
        for neighbor in (
            index - width,
            index + width,
            index - 1 if x else -1,
            index + 1 if x < width - 1 else -1,
        ):
            if 0 <= neighbor < total and not visited[neighbor] and white(neighbor):
                visited[neighbor] = 1
                queue.append(neighbor)
    background_count = visited.count(1)
    fraction = background_count / total
    if not 0.12 <= fraction <= 0.9:
        raise ValueError("WHITE_BACKGROUND_UNCERTAIN: subject/background separation is implausible")
    alpha = Image.frombytes("L", (width, height), bytes(255 * (not bit) for bit in visited))
    box = alpha.getbbox()
    if box is None or box[0] < 2 or box[1] < 2 or box[2] > width - 2 or box[3] > height - 2:
        raise ValueError("WHITE_BACKGROUND_UNCERTAIN: subject touches the image border")
    # Feather only a narrow boundary. The original source remains available for inspection.
    alpha = alpha.filter(ImageFilter.GaussianBlur(radius=0.7))
    rgba = image.convert("RGBA")
    rgba.putalpha(alpha)
    output = io.BytesIO()
    rgba.save(output, format="PNG")
    return output.getvalue(), {
        "background_fraction": round(fraction, 4),
        "subject_bounds": list(box),
        "method": "border_connected_near_white_with_feather",
        "limitation": "Heuristic matte; inspect fine white fur against dark backgrounds",
    }


def background_profile(data: bytes) -> dict:
    """Small color summary for choosing an extraction approach."""
    from collections import Counter

    with Image.open(io.BytesIO(data)) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    step = max(1, min(width, height) // 128)
    samples = []
    for x in range(0, width, step):
        samples.extend((image.getpixel((x, 0)), image.getpixel((x, height - 1))))
    for y in range(0, height, step):
        samples.extend((image.getpixel((0, y)), image.getpixel((width - 1, y))))
    bins = Counter(tuple(value // 16 for value in rgb) for rgb in samples)
    palette = [[min(255, value * 16 + 8) for value in color] for color, _ in bins.most_common(16)]
    near_white = sum(all(value >= 225 for value in rgb) for rgb in samples) / len(samples)
    return {
        "dimensions": [width, height],
        "border_near_white_fraction": round(near_white, 3),
        "border_palette_rgb": palette,
        "note": "Color samples cannot prove clean separation; inspect the cutout and composite preview",
    }


def extract_subject(
    data: bytes,
    *,
    method: str,
    tolerance: int = 40,
    polygon: list[list[int]] | None = None,
    protect_regions: list[list[int]] | None = None,
    feather_px: float = 0.8,
) -> tuple[bytes, dict]:
    """Border-color flood or an LLM-supplied contour, with optional interior protection."""
    from functools import lru_cache

    from PIL import ImageChops, ImageDraw

    if method not in {"border_color", "polygon", "hybrid"}:
        raise ValueError("Unknown extraction method")
    if not 10 <= tolerance <= 100 or not 0 <= feather_px <= 4:
        raise ValueError("Invalid extraction parameters")
    with Image.open(io.BytesIO(data)) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    if width < 32 or height < 32 or width * height > 16_000_000:
        raise ValueError("Unsupported cutout dimensions")
    if method in {"polygon", "hybrid"}:
        if method == "polygon" and not polygon:
            raise ValueError("Polygon method requires an outer subject contour")
    if polygon and (
        not 3 <= len(polygon) <= 128
        or any(len(point) != 2 or not (0 <= point[0] < width and 0 <= point[1] < height) for point in polygon)
    ):
        raise ValueError("Contour requires 3-128 source-image pixel points inside the image")
    if protect_regions and (
        len(protect_regions) > 8
        or any(
            len(box) != 4 or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)
            for box in protect_regions
        )
    ):
        raise ValueError("Protect regions must be up to eight valid source-image rectangles")
    if protect_regions and method != "hybrid":
        raise ValueError("Protect regions are only used by hybrid extraction")
    profile = background_profile(data)
    if method in {"border_color", "hybrid"}:
        palette = profile["border_palette_rgb"]
        rgb = image.tobytes()
        total = width * height
        visited = bytearray(total)
        queue = deque()
        tolerance_sq = tolerance * tolerance

        @lru_cache(maxsize=131072)
        def similar(r, g, b):
            return any(
                (r - color[0]) ** 2 + (g - color[1]) ** 2 + (b - color[2]) ** 2 <= tolerance_sq
                for color in palette
            )

        def background(index):
            offset = index * 3
            return similar(rgb[offset] // 4 * 4, rgb[offset + 1] // 4 * 4, rgb[offset + 2] // 4 * 4)

        border = set(range(width)) | set(range((height - 1) * width, total))
        border.update(row * width for row in range(height))
        border.update(row * width + width - 1 for row in range(height))
        seeds = [index for index in border if background(index)]
        if len(seeds) / len(border) < 0.7:
            raise ValueError("BACKGROUND_UNCERTAIN: border colors too varied for adaptive extraction")
        for index in seeds:
            visited[index] = 1
            queue.append(index)
        while queue:
            index = queue.popleft()
            x = index % width
            for neighbor in (
                index - width,
                index + width,
                index - 1 if x else -1,
                index + 1 if x < width - 1 else -1,
            ):
                if 0 <= neighbor < total and not visited[neighbor] and background(neighbor):
                    visited[neighbor] = 1
                    queue.append(neighbor)
        background_fraction = visited.count(1) / total
        if not 0.05 <= background_fraction <= 0.97:
            raise ValueError("BACKGROUND_UNCERTAIN: implausible subject/background ratio")
        alpha = Image.frombytes("L", (width, height), bytes(255 * (not bit) for bit in visited))
    else:
        alpha = Image.new("L", (width, height), 0)
        background_fraction = None
    if polygon:
        contour = Image.new("L", (width, height), 0)
        ImageDraw.Draw(contour).polygon([tuple(point) for point in polygon], fill=255)
        if method == "polygon":
            alpha = contour
        else:
            alpha = ImageChops.lighter(alpha, contour)
    if protect_regions:
        protected = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(protected)
        for left, top, right, bottom in protect_regions:
            draw.rectangle((left, top, right - 1, bottom - 1), fill=255)
        alpha = ImageChops.lighter(alpha, protected)
    bounds = alpha.getbbox()
    if bounds is None:
        raise ValueError("BACKGROUND_UNCERTAIN: empty subject mask")
    if method == "border_color" and (
        bounds[0] < 2 or bounds[1] < 2 or bounds[2] > width - 2 or bounds[3] > height - 2
    ):
        raise ValueError("BACKGROUND_UNCERTAIN: foreground reaches image edge; use a contour")
    if feather_px:
        alpha = alpha.filter(ImageFilter.GaussianBlur(feather_px))
    rgba = image.convert("RGBA")
    rgba.putalpha(alpha)
    output = io.BytesIO()
    rgba.save(output, format="PNG")
    return output.getvalue(), {
        "method": method,
        "tolerance": tolerance if method != "polygon" else None,
        "protected_regions": len(protect_regions or []),
        "has_protection_polygon": bool(polygon and method == "hybrid"),
        "feather_px": feather_px,
        "subject_bounds": list(bounds),
        "background_fraction": round(background_fraction, 4) if background_fraction is not None else None,
        "limitation": "Heuristic mask; inspect white fur, whiskers, shadows, and composite edges",
    }
