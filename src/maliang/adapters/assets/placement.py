from __future__ import annotations

import json

from PIL import Image

from .geometry import background_geometry


class AssetPlacement:
    def place_cutout(self, expected_revision, source_asset_id, cutout_asset_id):
        import json

        art = self.store.load()
        if art.revision != expected_revision:
            raise ValueError("STALE_STATE")
        if art.program is None or art.program.backend != "scene2d":
            raise ValueError("Cutout placement requires a scene2d code scene")
        task = next((t for t in art.asset_tasks if t.asset_id == source_asset_id), None)
        asset = next((a for a in art.assets if a.id == cutout_asset_id), None)
        if task is None or task.target_region is None or task.target_object_id is None:
            raise ValueError("LOCAL_REPAIR_REQUIRED: missing target object and region")
        if (
            asset is None
            or asset.provenance.get("source") not in {"white_background_cutout", "subject_cutout"}
            or asset.provenance.get("parent_asset_id") != source_asset_id
        ):
            raise ValueError("Cutout must derive from the planned source asset")
        obj = next((o for o in art.objects if o.id == task.target_object_id), None)
        if obj is None or obj.draw is None:
            raise ValueError("Code-drawn target object has changed; re-observe and replan")
        if obj.asset_ids:
            previous = next((a for a in art.assets if a.id == obj.asset_ids[0]), None)
            if (
                len(obj.asset_ids) != 1
                or previous is None
                or (previous.provenance.get("parent_asset_id") != source_asset_id)
            ):
                raise ValueError("Target now uses another asset; re-observe and replan")
        if obj.transform.rotation != 0 or obj.transform.scale_x != 1 or obj.transform.scale_y != 1:
            raise ValueError("Place cutout requires unrotated unit-scale target; adjust after placement")
        sx, sy, sw, sh, dx, dy, dw, dh = self.placement_geometry(asset, task)
        x, y = dx - obj.transform.x, dy - obj.transform.y
        source = (
            "function(ctx,t,object,assets,random){"
            f"const img=assets[{json.dumps(cutout_asset_id)}];"
            f"ctx.drawImage(img,{sx},{sy},{sw},{sh},{x},{y},{dw},{dh});"
            "}"
        )
        path, sha = self.store.blob(source.encode(), ".js")

        def change(data):
            target = next(o for o in data["objects"] if o["id"] == task.target_object_id)
            target["draw"] = {"backend": "canvas", "path": path, "sha256": sha}
            target["asset_ids"] = [cutout_asset_id]

        updated = self.store.mutate(expected_revision, change)
        self.store.log(
            "visual_operation",
            {
                "action": "place_cutout",
                "object_id": task.target_object_id,
                "asset_id": cutout_asset_id,
                "revision": updated.revision,
            },
        )
        return {
            "revision": updated.revision,
            "target_object_id": task.target_object_id,
            "target_region": task.target_region,
            "next": "Observe final composite, review quality, compare against the code-only draft",
        }


    def place_background(self, expected_revision, asset_id, observed_anchors, layout_notes):
        art = self.store.load()
        task = next((t for t in art.asset_tasks if t.asset_id == asset_id), None)
        asset = next((a for a in art.assets if a.id == asset_id), None)
        if task is None or task.role != "background" or asset is None:
            raise ValueError("A generated, planned background asset is required")
        self.validate_component(art, task.model_dump())
        if asset.provenance.get("asset_task") != task.model_dump():
            raise ValueError("Background provenance does not match its plan")
        target = next(o for o in art.objects if o.id == task.target_object_id)
        if target.start != 0 or target.end not in {None, art.spec.duration}:
            raise ValueError("Background layer must span the full scene duration")
        if target.motion or target.transform.model_dump() != {
            "x": 0.0,
            "y": 0.0,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "rotation": 0.0,
            "opacity": 1.0,
        }:
            raise ValueError("Background target must have identity transform and no motion")
        if not set(task.background_layout.anchors) <= set(observed_anchors):
            raise ValueError("Re-measure every planned anchor in final canvas coordinates after cover crop")
        for point in observed_anchors.values():
            if len(point) != 2 or not (0 <= point[0] <= art.spec.width and 0 <= point[1] <= art.spec.height):
                raise ValueError("Observed anchor outside canvas")
        with Image.open(self.store.path(asset.path)) as image:
            crop = background_geometry(image.width, image.height, art.spec.width, art.spec.height)
        source = (
            "function(ctx,t,object,assets,random){ctx.drawImage(assets["
            + json.dumps(asset_id)
            + "],"
            + ",".join(str(v) for v in crop)
            + f",0,0,{art.spec.width},{art.spec.height});"
            + "}"
        )
        path, sha = self.store.blob(source.encode(), ".js")
        layer = min((o.layer for o in art.objects if o.id != target.id), default=1) - 1

        def change(data):
            obj = next(o for o in data["objects"] if o["id"] == target.id)
            obj.update(
                draw={"backend": "canvas", "path": path, "sha256": sha}, asset_ids=[asset_id], layer=layer
            )
            obj["properties"]["background_layout"] = {
                "asset_id": asset_id,
                "source_crop": crop,
                "observed_anchors": observed_anchors,
                "layout_notes": layout_notes,
                "planned": task.background_layout.model_dump(),
            }

        updated = self.store.mutate(expected_revision, change)
        self.store.log(
            "visual_operation",
            {
                "action": "place_background",
                "asset_id": asset_id,
                "revision": updated.revision,
                "source_crop": crop,
                "observed_anchors": observed_anchors,
            },
        )
        return {
            "revision": updated.revision,
            "source_crop": crop,
            "observed_anchors": observed_anchors,
            "next": "Align code objects with observed contacts; render and review reserved space, occlusion and lighting. New plate invalidates prior scene reviews.",
        }


    def compose_asset(self, expected_revision, source_asset_id, asset_id, source):
        import json

        art = self.store.load()
        task = next((t for t in art.asset_tasks if t.asset_id == source_asset_id), None)
        asset = next((a for a in art.assets if a.id == asset_id), None)
        if not art.program or art.program.backend != "scene2d" or task is None or not task.target_region:
            raise ValueError("A planned local component and scene2d scene are required")
        if asset is None:
            raise ValueError("Unknown asset")
        is_cutout = (
            asset.provenance.get("source") in {"subject_cutout", "white_background_cutout"}
            and asset.provenance.get("parent_asset_id") == source_asset_id
        )
        if task.role == "subject" and not is_cutout:
            raise ValueError("Subjects must be extracted before code composition")
        if task.role not in {"subject", "texture"} or (
            task.role == "texture" and asset_id != source_asset_id and not is_cutout
        ):
            raise ValueError("Only the planned subject or texture can be composed")
        obj = next((o for o in art.objects if o.id == task.target_object_id), None)
        if (
            obj is None
            or obj.transform.rotation != 0
            or obj.transform.scale_x != 1
            or obj.transform.scale_y != 1
        ):
            raise ValueError(
                "Create an unrotated unit-scale target layer before binding; animate after composition"
            )
        left, top, right, bottom = task.target_region
        x, y = left - obj.transform.x, top - obj.transform.y
        # The model owns cropping, fitting, texture fills and shading inside a bounded layer.
        wrapped = (
            "function(ctx,t,object,assets,random){ctx.save();ctx.beginPath();"
            f"ctx.rect({x},{y},{right - left},{bottom - top});ctx.clip();"
            f"const draw=({source});draw(ctx,t,object,{{[{json.dumps(asset_id)}]:assets[{json.dumps(asset_id)}]}},random);"
            "ctx.restore();}"
        )
        path, sha = self.store.blob(wrapped.encode(), ".js")

        def change(data):
            target = next(o for o in data["objects"] if o["id"] == task.target_object_id)
            target["draw"] = {"backend": "canvas", "path": path, "sha256": sha}
            target["asset_ids"] = [asset_id]

        updated = self.store.mutate(expected_revision, change)
        self.store.log(
            "visual_operation",
            {
                "action": "compose_asset",
                "object_id": task.target_object_id,
                "asset_id": asset_id,
                "revision": updated.revision,
            },
        )
        return {
            "revision": updated.revision,
            "next": "Observe the actual composite; source controls crop, size, placement, blending and procedural animation",
        }


