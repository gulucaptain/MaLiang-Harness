"""Explicit cross-project reuse; validation and reviews never transfer."""

from .store import ProjectStore, digest


def reuse_content(target: ProjectStore, source: ProjectStore, expected_revision: int):
    original = source.load()
    current = target.load()
    if original.program and original.program.backend not in current.allowed_backends:
        raise ValueError("Source backend is forbidden by the target brief")
    if not current.allow_generated_assets and any(
        a.provenance.get("source") == "openai_images" or a.provenance.get("generated", False)
        for a in original.assets
    ):
        raise ValueError("Target brief forbids generated assets")
    payload = original.model_dump()
    for entry in [
        *(o["draw"] for o in payload["objects"] if o.get("draw")),
        *payload["assets"],
        *([payload["program"]] if payload["program"] else []),
    ]:
        data = source.path(entry["path"]).read_bytes()
        if digest(data) != entry["sha256"]:
            raise ValueError("Source content integrity check failed")
        suffix = source.path(entry["path"]).suffix
        entry["path"], entry["sha256"] = target.blob(data, suffix)
    result = target.mutate(
        expected_revision,
        lambda data: data.update({key: payload[key] for key in ("objects", "events", "assets", "program")}),
    )
    target.log(
        "reuse",
        {
            "source_project": str(source.root),
            "source_revision": original.revision,
            "target_revision": result.revision,
        },
    )
    return result
