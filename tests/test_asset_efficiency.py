"""Offline efficiency checks: preserve images, provenance, revision and review gates."""

import json

from test_code_directed import directed as directed
from test_code_directed import scaffold
from test_code_first_repair import mock_white_cat

from maliang.agent import make_context
from maliang.asset_workflow import asset_progress, inspection_path
from maliang.models import Artwork, OutputSpec
from maliang.store import ProjectStore


def prepare(ctx, monkeypatch):
    mock_white_cat(monkeypatch)
    scaffold(ctx)
    ctx.registry.invoke("generate_asset", {"expected_revision": 4, "asset_id": "cat_raw"})
    ctx.registry.invoke(
        "extract_subject",
        {
            "expected_revision": 5,
            "source_asset_id": "cat_raw",
            "asset_id": "cat_cut",
            "method": "border_color",
        },
    )
    assert ctx.store.load().revision == 6


def test_bulk_inspect_cache_survives_resume_without_approving_or_mutating(directed, monkeypatch):
    prepare(directed, monkeypatch)
    from maliang.adapters import cutout

    original = cutout.background_profile
    calls = []
    monkeypatch.setattr(cutout, "background_profile", lambda data: calls.append(1) or original(data))
    before = directed.store.load().model_dump()
    count = directed.meter.usage["tool_calls"]
    result = directed.registry.invoke("inspect_assets", {"asset_ids": ["cat_raw", "cat_cut", "cat_raw"]})
    assert len([b for b in result if b["type"] == "image_url"]) == 2
    assert len(calls) == 2
    assert directed.meter.usage["tool_calls"] == count + 3  # wrapper + two unique inspections
    assert directed.store.load().model_dump() == before
    assert not list(directed.store.root.glob("evidence/*.json"))
    assert directed.store.reviews() == {}
    progress = asset_progress(directed.store)["assets"]
    assert all(a["inspection_cached"] and not a["bound_to_scene"] for a in progress)
    resumed = make_context(
        directed.store.root, resume=True, plugins=False, image_generation=directed.image_generation
    )
    replay = resumed.registry.invoke("inspect_assets", {"asset_ids": ["cat_raw", "cat_cut"]})
    assert len(calls) == 2
    assert [b for b in replay if b["type"] == "image_url"] == [b for b in result if b["type"] == "image_url"]
    assert resumed.meter.usage["asset_api_calls"] == 1
    raw = next(a for a in directed.store.load().assets if a.id == "cat_raw")
    inspection_path(directed.store, raw.sha256).write_text("broken cache")
    assert not asset_progress(directed.store)["assets"][0]["inspection_cached"]
    resumed.registry.invoke("inspect_asset", {"asset_id": "cat_raw"})
    assert len(calls) == 3


def test_binding_error_gives_usable_recovery_without_new_generation(directed, monkeypatch):
    prepare(directed, monkeypatch)
    before = directed.store.load().model_dump()
    error = directed.registry.invoke(
        "put_object",
        {"expected_revision": 6, "object_id": "cat", "source": "function(){}", "asset_ids": ["cat_cut"]},
    )
    assert error["status"] == "error"
    assert directed.store.load().model_dump() == before
    choice = error["recovery"]["placements"][0]
    assert choice["target_region_ltrb"] == [16, 8, 48, 56]
    call = choice["suggested_call"]
    result = directed.registry.invoke(call["tool"], call["args"])
    assert result["revision"] == 7
    assert directed.meter.usage["asset_api_calls"] == 1
    assert next(o for o in directed.store.load().objects if o.id == "cat").asset_ids == ["cat_cut"]
    assert directed.store.reviews() == {}
    assert asset_progress(directed.store)["assets"][1]["bound_to_scene"]
    assert directed.store.path("versions/000006.json").is_file()


def test_inspection_error_stops_batch_before_mutation(directed, monkeypatch):
    prepare(directed, monkeypatch)
    capability = directed.registry.capabilities["inspect_asset"]
    monkeypatch.setattr(
        capability, "handler", lambda **kwargs: (_ for _ in ()).throw(ValueError("bad image"))
    )
    result = directed.registry.invoke(
        "execute_steps",
        {
            "expected_revision": 6,
            "steps": [
                {"tool": "inspect_assets", "args": {"asset_ids": ["cat_raw"]}},
                {"tool": "put_object", "args": {"object_id": "new", "source": "function(){}"}},
            ],
        },
    )
    assert json.loads(result[0]["text"])["executed"] == 1
    assert directed.store.load().revision == 6


def test_batch_program_patch_saves_each_revision_and_stops_on_failure(tmp_path):
    store = ProjectStore(tmp_path)
    store.create(Artwork(prompt="a rectangle", spec=OutputSpec(format="png")))
    ctx = make_context(tmp_path, plugins=False)
    source = "function(ctx){ctx.fillStyle='red';ctx.fillRect(0,0,10,10)}"
    result = ctx.registry.invoke(
        "execute_steps",
        {
            "expected_revision": 0,
            "steps": [
                {"tool": "write_program", "args": {"backend": "canvas", "source": source}},
                {"tool": "patch_program", "args": {"old_text": "'red'", "new_text": "'blue'"}},
                {"tool": "patch_program", "args": {"old_text": "does not exist", "new_text": "unused"}},
                {"tool": "patch_program", "args": {"old_text": "'blue'", "new_text": "'green'"}},
            ],
        },
    )
    batch = json.loads(result[0]["text"])
    assert batch["executed"] == 3 and batch["resume_batch_id"]
    assert store.load().revision == 2
    assert "'blue'" in store.path(store.load().program.path).read_text()
    assert store.path("versions/000001.json").is_file()


def test_identical_asset_images_dedup_but_keep_each_identity(directed, monkeypatch):
    prepare(directed, monkeypatch)
    original = directed.store.load().assets[0].model_dump()
    alias = {**original, "id": "same_pixels"}
    directed.store.mutate(6, lambda data: data["assets"].append(alias))
    result = directed.registry.invoke("inspect_assets", {"asset_ids": ["cat_raw", "same_pixels"]})
    assert len([b for b in result if b["type"] == "image_url"]) == 1
    ids = {json.loads(b["text"])["metadata"]["asset_id"] for b in result if b["type"] == "text"}
    assert ids == {"cat_raw", "same_pixels"}
    assert directed.store.load().revision == 7
