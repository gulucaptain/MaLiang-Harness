"""Compact asset progress and bounded recovery hints; never visual approval."""

from __future__ import annotations

import json


def inspection_path(store, sha):
    return store.path(f".asset-inspections/{sha}.json")


def cached_inspection(store, sha):
    try:
        data = json.loads(inspection_path(store, sha).read_text())
        if (
            data.get("schema") == 1
            and data.get("sha256") == sha
            and all(key in data for key in ("width", "height", "alpha_extrema", "background_profile"))
        ):
            return data
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def asset_progress(store):
    art = store.load()
    bound = {asset_id for obj in art.objects for asset_id in obj.asset_ids}
    result = []
    for asset in art.assets:
        parent = asset.provenance.get("parent_asset_id")
        task = next((t for t in art.asset_tasks if t.asset_id == (parent or asset.id)), None)
        if task is None:
            continue
        result.append(
            {
                "asset_id": asset.id,
                "source_asset_id": parent or asset.id,
                "inspection_cached": cached_inspection(store, asset.sha256) is not None,
                "bound_to_scene": asset.id in bound,
                "represented_in_scene": asset.id in bound
                or any(
                    child.id in bound and child.provenance.get("parent_asset_id") == asset.id
                    for child in art.assets
                ),
                "target_object_id": task.target_object_id,
                "target_region_ltrb": task.target_region,
                "is_cutout": asset.provenance.get("source") in {"subject_cutout", "white_background_cutout"},
            }
        )
    return {
        "assets": result,
        "next": "Batch inspect unseen needed assets with inspect_assets. Reuse previous visual judgments only if still available; reinspect if uncertain. Once usable and not already represented_in_scene, bind/place in the planned layer and observe_requirements before another generation attempt. Cached analysis is not a model verdict, proof the image remains in context, or final scene evidence.",
        "binding_contract": "Regions are [left, top, right, bottom], NOT [x,y,width,height]. compose_asset binds one source per planned target. Plan separate targets for independently placed source images before generation. Tool binding errors are not image quality failures.",
    }


def recovery_hint(store, name, arguments, error, capabilities):
    if name != "put_object" or "use place_cutout" not in error:
        return None
    art = store.load()
    choices = []
    for asset_id in arguments.get("asset_ids", []):
        asset = next((a for a in art.assets if a.id == asset_id), None)
        if asset is None:
            continue
        parent = asset.provenance.get("parent_asset_id")
        task = next((t for t in art.asset_tasks if t.asset_id == parent), None)
        target = next((o for o in art.objects if task and o.id == task.target_object_id), None)
        if task is None or target is None:
            continue
        choices.append(
            {
                "asset_id": asset_id,
                "target_object_id": target.id,
                "target_region_ltrb": task.target_region,
                "target_transform": target.transform.model_dump(),
                "target_bound_assets": target.asset_ids,
                "suggested_call": {
                    "tool": "place_cutout",
                    "args": {
                        "expected_revision": art.revision,
                        "source_asset_id": parent,
                        "cutout_asset_id": asset_id,
                    },
                }
                if "place_cutout" in capabilities
                else None,
            }
        )
    return {
        "reason": "Generated subjects must use planned binding tools. Do not regenerate images to fix this tool-selection error.",
        "placements": choices,
        "constraints": "Choose a compatible placement; these are alternatives, not a batch to execute blindly. Multiple sources targeting the same object overwrite or conflict, not combine. Binding requires an unrotated unit-scale target; preserve intended transforms and apply them after binding. Keep existing extraction, provenance and final observation gates.",
    }
