from __future__ import annotations

import json

from PIL import Image

from ...models import Evidence
from .geometry import background_geometry


class AssetInspection:
    def inspect_asset(self, asset_id):
        from ...capabilities import image_result
        from ..cutout import background_profile

        art = self.store.load()
        asset = next((a for a in art.assets if a.id == asset_id), None)
        if asset is None:
            raise ValueError("Unknown asset")
        from ...asset_workflow import cached_inspection, inspection_path
        from ...store import atomic_json

        # Cache intrinsic image analysis only. Never cache a model verdict or scene evidence.
        cache_path = inspection_path(self.store, asset.sha256)
        cached = cached_inspection(self.store, asset.sha256)
        if cached is None:
            with Image.open(self.store.path(asset.path)) as opened:
                cached = {
                    "schema": 1,
                    "sha256": asset.sha256,
                    "width": opened.width,
                    "height": opened.height,
                    "alpha_extrema": opened.convert("RGBA").getchannel("A").getextrema(),
                    "background_profile": background_profile(self.store.path(asset.path).read_bytes()),
                }
        # Deliberately NOT saved as scene evidence: raw assets cannot satisfy final artwork reviews.
        evidence = Evidence(
            id=self.store.new_id(),
            revision=art.revision,
            kind="frames",
            paths=[asset.path],
            metadata={
                "scope": "raw_asset_only",
                "asset_id": asset_id,
                "background_profile": cached["background_profile"]
                if asset.provenance.get("source") in {"qwen", "openai_images"}
                else None,
                "alpha_extrema": cached["alpha_extrema"],
                "next": "For backgrounds: inspect composition, reserved spaces, contacts and unintended actors; use place_background with observed canvas anchors. Other subjects may need extraction.",
                "background_layout": next(
                    (
                        t.background_layout.model_dump()
                        for t in art.asset_tasks
                        if t.asset_id == asset_id and t.background_layout
                    ),
                    None,
                ),
                "cover_source_crop": background_geometry(
                    cached["width"], cached["height"], art.spec.width, art.spec.height
                ),
            },
        )
        result = image_result(self.store, evidence)
        atomic_json(cache_path, cached)
        self.store.log(
            "asset_inspection", {"asset_id": asset_id, "sha256": asset.sha256, "revision": art.revision}
        )
        return result


    def inspect_assets(self, asset_ids):
        ids = list(dict.fromkeys(asset_ids))
        known = {a.id for a in self.store.load().assets}
        if any(asset_id not in known for asset_id in ids):
            raise ValueError("Unknown asset in inspection group")
        blocks, seen = [], set()
        for asset_id in ids:
            result = self.registry.invoke("inspect_asset", {"asset_id": asset_id})
            if isinstance(result, dict) and result.get("error"):
                return result
            for block in result:
                if block.get("type") == "image_url":
                    identity = json.dumps(block, sort_keys=True)
                    if identity in seen:
                        continue
                    seen.add(identity)
                blocks.append(block)
        return blocks


