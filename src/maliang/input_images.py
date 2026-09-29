"""Validate and normalize user images before creating an immutable task."""

import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 10_000_000
MAX_IMAGE_PIXELS = 16_000_000


def normalize_image(data: bytes) -> bytes:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图像须小于 10 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("仅支持 PNG、JPEG、WebP 静态图像")
                if image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("图像须为单帧且不超过 1600 万像素")
                output = io.BytesIO()
                ImageOps.exif_transpose(image).convert("RGBA").save(output, "PNG")
                result = output.getvalue()
                if len(result) > MAX_IMAGE_BYTES:
                    raise ValueError("图像解码后过大")
                return result
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("无法读取图像或图像过大") from exc


def attach_input_image(store, art, data):
    from .models import Asset

    path, sha = store.blob(data, ".png")
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    if any(asset.id == "input_image" for asset in art.assets):
        raise ValueError("input_image asset already exists")
    art.assets.append(
        Asset(
            id="input_image",
            path=path,
            sha256=sha,
            media_type="image/png",
            provenance={"source": "user_upload", "width": width, "height": height},
        )
    )
