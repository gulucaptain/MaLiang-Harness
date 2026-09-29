from __future__ import annotations

import io

from PIL import Image


class AssetStorage:
    def placement_geometry(self, asset, task):
        with Image.open(self.store.path(asset.path)) as opened:
            image_width, image_height = opened.size
        bounds = asset.provenance.get("metrics", {}).get("subject_bounds")
        if not bounds or len(bounds) != 4:
            raise ValueError("Cutout has no measured subject bounds")
        sx = max(0, bounds[0] - 2)
        sy = max(0, bounds[1] - 2)
        right = min(image_width, bounds[2] + 2)
        bottom = min(image_height, bounds[3] + 2)
        sw, sh = right - sx, bottom - sy
        if sw <= 0 or sh <= 0:
            raise ValueError("Invalid cutout subject bounds")
        left, top, region_right, region_bottom = task.target_region
        scale = min((region_right - left) / sw, (region_bottom - top) / sh)
        dw, dh = max(1, round(sw * scale)), max(1, round(sh * scale))
        dx = left + (region_right - left - dw) // 2
        dy = region_bottom - dh
        return sx, sy, sw, sh, dx, dy, dw, dh


    def persist(self, data, asset_id, expected_revision, provenance):
        if len(data) > 20_000_000:
            raise ValueError("Asset exceeds 20 MB")
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 16_000_000:
                raise ValueError("Asset too large")
            output = io.BytesIO()
            image.convert("RGBA").save(output, format="PNG")
        path, sha = self.store.blob(output.getvalue(), ".png")
        asset = {
            "id": asset_id,
            "path": path,
            "sha256": sha,
            "media_type": "image/png",
            "provenance": provenance,
        }
        return self.store.mutate(expected_revision, lambda obj: obj["assets"].append(asset)).model_dump()


    def import_asset(self, expected_revision, asset_id, relative_path):
        # Human references are staged in this project; arbitrary host filesystem access is not exposed.
        return self.persist(
            self.store.path(relative_path).read_bytes(),
            asset_id,
            expected_revision,
            {"source": "project_import", "path": relative_path},
        )


