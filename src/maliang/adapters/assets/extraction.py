from __future__ import annotations

import io

from PIL import Image

from ...models import Evidence


class AssetExtraction:
    def extract_white(self, expected_revision, source_asset_id, asset_id, white_threshold):
        from ..cutout import cutout_white_background

        art = self.store.load()
        if art.revision != expected_revision:
            raise ValueError("STALE_STATE")
        if asset_id in {a.id for a in art.assets}:
            raise ValueError("Asset ID already exists")
        source = next((a for a in art.assets if a.id == source_asset_id), None)
        if source is None:
            raise ValueError("Unknown source asset")
        task = next((t for t in art.asset_tasks if t.asset_id == source_asset_id), None)
        if task is None or task.extraction != "white_background":
            raise ValueError("WHITE_BACKGROUND_PLAN_REQUIRED")
        data, metrics = cutout_white_background(
            self.store.path(source.path).read_bytes(), threshold=white_threshold
        )
        updated = self.persist(
            data,
            asset_id,
            expected_revision,
            {
                "source": "white_background_cutout",
                "parent_asset_id": source_asset_id,
                "parent_sha256": source.sha256,
                "threshold": white_threshold,
                "metrics": metrics,
            },
        )
        self.store.log(
            "visual_operation",
            {"action": "extract_white_background", "asset_id": asset_id, "revision": updated["revision"]},
        )
        return {
            "revision": updated["revision"],
            "asset_id": asset_id,
            "metrics": metrics,
            "next": "Inspect transparent edges before placing into the existing coded scene",
        }


    def extract_adaptive(self, 
        expected_revision, source_asset_id, asset_id, method, tolerance, polygon, protect_regions, feather_px
    ):
        from ..cutout import extract_subject

        art = self.store.load()
        if art.revision != expected_revision:
            raise ValueError("STALE_STATE")
        if asset_id in {a.id for a in art.assets}:
            raise ValueError("Asset ID already exists")
        source = next((a for a in art.assets if a.id == source_asset_id), None)
        task = next((t for t in art.asset_tasks if t.asset_id == source_asset_id), None)
        if source is None or task is None or task.role != "subject":
            raise ValueError("SUBJECT_PLAN_REQUIRED: use a planned generated subject")
        if source.provenance.get("source") not in {"qwen", "openai_images"}:
            raise ValueError("Source must be an original generated subject")
        data, metrics = extract_subject(
            self.store.path(source.path).read_bytes(),
            method=method,
            tolerance=tolerance if tolerance is not None else self.config.cutout_default_tolerance,
            polygon=polygon,
            protect_regions=protect_regions,
            feather_px=feather_px if feather_px is not None else self.config.cutout_default_feather_px,
        )
        updated = self.persist(
            data,
            asset_id,
            expected_revision,
            {
                "source": "subject_cutout",
                "parent_asset_id": source_asset_id,
                "parent_sha256": source.sha256,
                "metrics": metrics,
            },
        )
        self.store.log(
            "visual_operation",
            {
                "action": "extract_subject",
                "source_asset_id": source_asset_id,
                "asset_id": asset_id,
                "revision": updated["revision"],
            },
        )
        return {
            "revision": updated["revision"],
            "asset_id": asset_id,
            "metrics": metrics,
            "next": "Inspect the cutout and preview it on the coded scene before placement",
        }


    def preview_cutout(self, source_asset_id, cutout_asset_id):
        from ...capabilities import image_result

        art = self.store.load()
        task = next((t for t in art.asset_tasks if t.asset_id == source_asset_id), None)
        asset = next((a for a in art.assets if a.id == cutout_asset_id), None)
        if (
            task is None
            or task.target_region is None
            or task.target_object_id is None
            or asset is None
            or (
                asset.provenance.get("source") not in {"subject_cutout", "white_background_cutout"}
                or asset.provenance.get("parent_asset_id") != source_asset_id
            )
        ):
            raise ValueError("Preview requires a cutout derived from the planned source")
        before = self.context.renderers.preview_without_object(task.target_object_id)
        with Image.open(self.store.path(before.paths[0])) as rendered:
            base = rendered.convert("RGBA")
        sx, sy, sw, sh, dx, dy, dw, dh = self.placement_geometry(asset, task)
        with Image.open(self.store.path(asset.path)) as opened:
            overlay = (
                opened.convert("RGBA")
                .crop((sx, sy, sx + sw, sy + sh))
                .resize((dw, dh), Image.Resampling.LANCZOS)
            )
        base.alpha_composite(overlay, (dx, dy))
        output = io.BytesIO()
        base.save(output, format="PNG")
        path, _ = self.store.blob(output.getvalue(), ".png")
        evidence = Evidence(
            id=self.store.new_id(),
            revision=art.revision,
            kind="frames",
            paths=[path],
            timestamps=[0],
            metadata={
                "scope": "composite_preview_only",
                "cutout_asset_id": cutout_asset_id,
                "target_region": task.target_region,
                "note": "Preview is not a committed scene or final review evidence",
            },
        )
        self.store.add_evidence(evidence)
        return image_result(self.store, evidence)


