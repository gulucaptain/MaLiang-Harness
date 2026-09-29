import json

import pytest
from PIL import Image

from maliang.agent import make_context
from maliang.capabilities import Empty
from maliang.models import Artwork, Budget, Evidence, OutputSpec, Review
from maliang.runtime import BudgetExceeded, Capability
from maliang.store import ProjectStore
from maliang.verification import verify


@pytest.fixture
def context(tmp_path):
    store = ProjectStore(tmp_path)
    store.create(
        Artwork(
            prompt="Draw an orange koi",
            spec=OutputSpec(width=64, height=64, format="png"),
            requirements=[{"id": "koi", "description": "Koi visibly present", "kind": "visual"}],
        )
    )
    return make_context(tmp_path, plugins=False)


def save(context, source="function(ctx){ctx.fillRect(0,0,20,20)}", backend="canvas"):
    return context.registry.invoke(
        "write_program",
        {"expected_revision": context.store.load().revision, "backend": backend, "source": source},
    )


def evidence(context):
    path = context.store.path("previews/test.png")
    path.parent.mkdir(exist_ok=True)
    Image.new("RGB", (64, 64), "orange").save(path)
    ev = Evidence(
        id=context.store.new_id(),
        revision=context.store.load().revision,
        kind="frames",
        paths=["previews/test.png"],
        timestamps=[0],
    )
    context.store.add_evidence(ev)
    return ev


def test_stale_edit_cannot_overwrite(context):
    save(context)
    result = context.registry.invoke(
        "write_program", {"expected_revision": 0, "backend": "canvas", "source": "function(){}"}
    )
    assert result["status"] == "error"
    assert "STALE_STATE" in result["error"]
    assert context.store.load().revision == 1


def test_review_requires_current_render_and_edit_invalidates_it(context):
    save(context)
    ev = evidence(context)
    context.store.add_review(
        Review(requirement_id="koi", revision=1, evidence_ids=[ev.id], verdict="pass", explanation="Visible")
    )
    assert verify(context.store)["requirements"][0]["verdict"] == "pass"
    save(context, "function(ctx){ctx.fillRect(0,0,30,30)}")
    assert verify(context.store)["requirements"][0]["verdict"] == "unreviewed"
    with pytest.raises(ValueError, match="STALE_EVIDENCE"):
        context.store.add_review(
            Review(requirement_id="koi", revision=2, evidence_ids=[ev.id], verdict="pass", explanation="Old")
        )


def test_restore_preserves_task_and_creates_revision(context):
    save(context)
    first = context.store.load()
    save(context, "function(){}")
    restored = context.store.restore(1, 2)
    assert restored.revision == 3
    assert restored.program == first.program
    assert restored.requirements == first.requirements
    assert context.store.path("versions/000002.json").exists()


def test_paths_and_symlinks_cannot_escape(context, tmp_path):
    with pytest.raises(ValueError):
        context.store.path("../outside")
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        context.store.path("escape/secret")


def test_hard_requirements_cannot_be_deleted_via_state_update(context):
    context.registry.invoke(
        "update_artwork", {"expected_revision": 0, "plan": [], "objects": [], "events": []}
    )
    assert context.store.load().requirements[0].id == "koi"


def test_unknown_asset_reference_is_rejected_atomically(context):
    result = context.registry.invoke(
        "update_artwork",
        {
            "expected_revision": 0,
            "plan": [],
            "events": [],
            "objects": [{"id": "fish", "asset_ids": ["missing"]}],
        },
    )
    assert result["status"] == "error"
    assert context.store.load().revision == 0


def test_temporal_constraints_check_actual_declared_order(context):
    result = context.registry.invoke(
        "update_artwork",
        {
            "expected_revision": 0,
            "plan": [],
            "objects": [],
            "events": [{"id": "a", "start": 1, "end": 2}, {"id": "b", "start": 0, "end": 1}],
        },
    )
    assert result["revision"] == 1
    context.registry.invoke(
        "add_requirements",
        {
            "expected_revision": 1,
            "requirements": [
                {"id": "order", "kind": "event_order", "description": "a before b", "targets": ["a", "b"]}
            ],
        },
    )
    assert verify(context.store)["requirements"][1]["verdict"] == "fail"


def test_no_declaration_can_bypass_visual_requirements(context):
    save(context)
    ev = evidence(context)
    result = context.registry.invoke("finalize_artwork", {"export_id": ev.id})
    assert result["status"] == "needs_revision"
    assert not json.loads(context.store.path("status.json").read_text())["status"] == "completed"


def test_budget_failure_does_not_execute_tool(context):
    context.meter.budget = Budget(max_tool_calls=1)
    context.registry.invoke("read_artwork", {})
    with pytest.raises(BudgetExceeded):
        save(context)
    assert context.store.load().revision == 0


def test_forbidden_backend_and_image_generator_not_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv("MALIANG_IMAGE_MODEL", "configured-model")
    ProjectStore(tmp_path).create(Artwork(prompt="Code only", allowed_backends=["svg"]))
    ctx = make_context(tmp_path, plugins=False)
    assert "generate_asset" not in ctx.registry.capabilities
    assert save(ctx)["status"] == "error"
    assert [r["id"] for r in ctx.renderers.available()] == ["svg"]


@pytest.mark.parametrize(
    "svg",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
    ],
)
def test_svg_rejects_active_or_external_content(context, svg):
    assert save(context, svg, "svg")["status"] == "error"


def test_imported_asset_is_immutable_and_reusable(context):
    source = context.store.path("reference.png")
    Image.new("RGB", (16, 16), "red").save(source)
    result = context.registry.invoke(
        "import_asset", {"expected_revision": 0, "asset_id": "ref", "relative_path": "reference.png"}
    )
    assert result["revision"] == 1
    blob = context.store.path(result["assets"][0]["path"])
    original = blob.read_bytes()
    Image.new("RGB", (16, 16), "blue").save(source)
    assert blob.read_bytes() == original


def test_plugin_capability_does_not_require_core_changes(context):
    context.registry.register(
        Capability("custom_backend_info", "Another backend", Empty, lambda: {"backend": "test"}, "plugin")
    )
    assert context.registry.invoke("custom_backend_info", {}) == {"backend": "test"}
    tool = next(t for t in context.registry.langchain_tools() if t.name == "custom_backend_info")
    assert tool.invoke({}) == {"backend": "test"}


def test_observation_returns_image_content_not_just_path(context):
    from maliang.capabilities import image_result

    output = image_result(context.store, evidence(context))
    assert output[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_review_attribution_cannot_be_spoofed_by_agent(context):
    ev = evidence(context)
    result = context.registry.invoke(
        "record_review",
        {"requirement_id": "koi", "evidence_ids": [ev.id], "verdict": "pass", "explanation": "Observed"},
    )
    assert result["recorded"]["source"] == "model"
    assert result["independent_quality_assessment"] is False


def test_usage_resume_preserves_consumed_budget(context):
    context.registry.invoke("read_artwork", {})
    resumed = make_context(context.store.root, resume=True, plugins=False)
    assert resumed.meter.usage["tool_calls"] == 1


def test_new_review_revokes_previous_completion(context):
    from maliang.store import atomic_json

    save(context)
    ev = evidence(context)
    atomic_json(context.store.path("status.json"), {"status": "completed", "revision": 1})
    context.store.add_review(
        Review(
            requirement_id="koi",
            revision=1,
            evidence_ids=[ev.id],
            verdict="fail",
            explanation="Missing koi",
            source="human",
        )
    )
    assert json.loads(context.store.path("status.json").read_text())["status"] != "completed"
    assert verify(context.store)["requirements"][0]["source"] == "human"


def test_reuse_preserves_target_brief_and_does_not_reuse_reviews(context, tmp_path):
    from maliang.reuse import reuse_content

    save(context)
    ev = evidence(context)
    context.store.add_review(
        Review(requirement_id="koi", revision=1, evidence_ids=[ev.id], verdict="pass", explanation="Visible")
    )
    target = ProjectStore(tmp_path / "target")
    target.create(
        Artwork(
            prompt="Different brief",
            spec=OutputSpec(format="png"),
            requirements=[{"id": "new", "description": "New requirement"}],
        )
    )
    art = reuse_content(target, context.store, 0)
    assert art.prompt == "Different brief"
    assert art.requirements[0].id == "new"
    assert target.reviews() == {}
    assert (
        target.path(art.program.path).read_bytes()
        == context.store.path(context.store.load().program.path).read_bytes()
    )


def test_non_canvas_backend_can_use_same_export_core(tmp_path):
    class SolidRenderer:
        name, suffix, supports_animation, description = "solid", ".json", False, "JSON color"

        def validate_source(self, source):
            assert "color" in json.loads(source)

        def frames(self, artwork, source, times, output, assets):
            for index, _ in enumerate(times):
                Image.new("RGB", (artwork.spec.width, artwork.spec.height), json.loads(source)["color"]).save(
                    output / f"{index:06d}.png"
                )

    store = ProjectStore(tmp_path)
    store.create(
        Artwork(
            prompt="Green", spec=OutputSpec(width=64, height=64, format="png"), allowed_backends=["solid"]
        )
    )
    ctx = make_context(tmp_path, plugins=False)
    ctx.renderers.register(SolidRenderer())
    save(ctx, '{"color":"green"}', "solid")
    exported = ctx.renderers.export()
    assert verify(store, exported.id)["eligible_to_finalize"]
    with Image.open(store.path(exported.paths[0])) as image:
        assert image.getpixel((0, 0)) == (0, 128, 0)


def test_responses_serializer_preserves_actual_image_feedback():
    import httpx
    from langchain_core.messages import ToolMessage
    from langchain_openai import ChatOpenAI

    # No network or real key; exercise the installed provider serialization code.
    with httpx.Client(trust_env=False) as client:
        model = ChatOpenAI(
            model="offline-contract", api_key="unused", http_client=client, use_responses_api=True
        )
        payload = model._get_request_payload(
            [
                ToolMessage(
                    tool_call_id="frame_call",
                    content=[
                        {"type": "text", "text": "Current frame"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                    ],
                )
            ]
        )
    output = payload["input"][0]["output"]
    assert isinstance(output, list)
    assert any(x.get("type") == "input_image" and x["image_url"].startswith("data:image/png") for x in output)
