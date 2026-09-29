"""Trace projection must associate real actions, snapshots and evidence truthfully."""

import json

from maliang.agent import make_context
from maliang.models import Artwork, OutputSpec
from maliang.process_view import ProcessIndex, explicit_text, revision_changes
from maliang.store import ProjectStore


def append(root, *events):
    with (root / "trace.jsonl").open("a") as stream:
        for event in events:
            stream.write(json.dumps(event) + "\n")


def test_incremental_nested_steps_and_public_explanation(tmp_path):
    append(
        tmp_path,
        {"event": "model_start", "call": 1},
        {
            "event": "model_response",
            "content": [
                {"type": "reasoning", "text": "private"},
                {"type": "text", "text": "Change the circle; preserve the background."},
            ],
        },
        {"event": "model_end"},
        {"event": "tool_start", "tool": "execute_steps", "arguments": {}},
        {"event": "tool_start", "tool": "patch_program", "revision": 4, "arguments": {}},
        {"event": "revision", "from": 4, "to": 5},
        {"event": "tool_end", "tool": "patch_program"},
        {"event": "tool_start", "tool": "render_frames", "revision": 5, "arguments": {}},
        {
            "event": "evidence",
            "paths": ["outputs/frame.png"],
            "timestamps": [0],
            "revision": 5,
            "kind": "frames",
        },
        {"event": "tool_end", "tool": "render_frames"},
        {"event": "tool_end", "tool": "execute_steps"},
    )
    index = ProcessIndex(tmp_path)
    steps = index.read()["steps"]
    assert len(steps) == 3
    patch = index.read(steps[1]["id"])
    assert (patch["before_revision"], patch["revision"]) == (4, 5)
    assert patch["summary"] == "Change the circle; preserve the background."
    assert "private" not in patch["summary"]
    assert index.read(steps[2]["id"])["media"][0]["revision"] == 5
    stable_ids = [s["id"] for s in steps]
    append(
        tmp_path,
        {"event": "tool_start", "tool": "edit_object", "revision": 5, "arguments": {}},
        {"event": "tool_end", "tool": "edit_object", "status": "error", "error": "conflict"},
    )
    steps = index.read()["steps"]
    assert [s["id"] for s in steps[:3]] == stable_ids
    assert steps[-1]["revision"] == 5 and steps[-1]["status"] == "error"
    assert ProcessIndex(tmp_path).read() == index.read()


def test_partial_lines_restart_and_interruption(tmp_path):
    append(tmp_path, {"event": "model_start", "call": 1})
    index = ProcessIndex(tmp_path)
    first = index.read()
    with (tmp_path / "trace.jsonl").open("a") as stream:
        stream.write('{"event":"model_response","content":"hello"}')
    assert index.read() == first
    with (tmp_path / "trace.jsonl").open("a") as stream:
        stream.write("\n")
    assert index.read()["steps"][0]["summary"] == "hello"
    append(tmp_path, {"event": "run_error"})
    assert index.read()["steps"][0]["status"] == "interrupted"
    (tmp_path / "trace.jsonl").write_text("")
    assert index.read()["steps"] == []
    assert explicit_text([{"type": "reasoning", "text": "not public"}]) == ""


def test_code_diff_uses_exact_snapshots(tmp_path):
    store = ProjectStore(tmp_path)
    store.create(Artwork(prompt="Circle", spec=OutputSpec(width=160, height=96, format="png")))
    ctx = make_context(tmp_path, plugins=False)
    ctx.registry.invoke(
        "write_program",
        {
            "expected_revision": 0,
            "backend": "canvas",
            "source": "function(ctx){ctx.fillStyle='red';ctx.fillRect(0,0,20,20)}",
        },
    )
    ctx.registry.invoke("patch_program", {"expected_revision": 1, "old_text": "'red'", "new_text": "'blue'"})
    changes = revision_changes(store, store.load(), 1)
    assert changes["base_revision"] == 1
    assert changes["files"][0]["name"] == "program"
    diff = changes["files"][0]["diff"]
    assert "-function(ctx){ctx.fillStyle='red'" in diff
    assert "+function(ctx){ctx.fillStyle='blue'" in diff
    assert not revision_changes(store, store.load(), 2)["files"]
