"""Load portable user-authored tasks without requiring historical experiment outputs."""

from pathlib import Path

from .common import digest, inside, jsonl, safe_id


def load_tasks(path: Path, modality="all", case_ids=None, limit=None):
    """Read JSONL tasks; input asset paths are relative to the JSONL directory.

    Reference images are optional. Their hashes are computed during planning and,
    when supplied, verified against the user-provided hash before freezing inputs.
    """
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f"Dataset JSONL not found: {path}")
    cases, seen = [], set()
    allowed = {
        "case_id", "modality", "prompt", "spec", "allowed_backends",
        "workflow_policy", "allow_generated_assets", "initial_requirements", "input_assets",
    }
    for row in jsonl(path):
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError("Each dataset row must be a task object with supported fields")
        task = dict(row)
        case_id = safe_id(task["case_id"])
        if case_id in seen:
            raise ValueError(f"Duplicate case ID: {case_id}")
        seen.add(case_id)
        spec = task["spec"]
        kind = "video" if spec.get("format") == "mp4" else "image"
        if task.get("modality", kind) != kind:
            raise ValueError(f"Modality does not match output format: {case_id}")
        task["modality"] = kind
        task.setdefault("allowed_backends", ["scene2d", "canvas", "svg_animation" if kind == "video" else "svg"])
        task.setdefault("workflow_policy", "guided")
        task.setdefault("allow_generated_assets", False)
        task.setdefault("initial_requirements", [
            {"id": "brief_fulfillment", "kind": "visual", "description": "Match the original prompt"}
        ])
        assets = []
        for asset in task.get("input_assets", []):
            item = dict(asset)
            source = inside(path.parent, item["path"])
            actual = digest(source)
            if item.get("sha256", actual) != actual:
                raise ValueError(f"Input asset hash mismatch: {case_id}")
            item["sha256"] = actual
            assets.append(item)
        task["input_assets"] = assets
        if modality == "all" or modality == kind:
            cases.append({"task": task, "baseline_path": None, "baseline_sha256": None, "historical_config": None})
    if case_ids:
        absent = set(case_ids) - {c["task"]["case_id"] for c in cases}
        if absent:
            raise ValueError(f"Unknown case IDs for this modality: {sorted(absent)}")
        cases = [c for c in cases if c["task"]["case_id"] in case_ids]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        cases = cases[:limit]
    if not cases:
        raise ValueError("No cases selected")
    return cases
