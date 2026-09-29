import hashlib
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from test_agent import TestModel

from maliang.agent import make_context, prepare_image_messages, run_agent
from maliang.models import Artwork, Evidence, Requirement, Review
from maliang.store import ProjectStore
from maliang.verification import verify


def eid(name):
    return hashlib.md5(name.encode()).hexdigest()


def setup_review(tmp_path):
    store = ProjectStore(tmp_path)
    store.create(
        Artwork(
            prompt="A moving image",
            workflow_policy="guided",
            requirements=[
                Requirement(id="motion", kind="temporal", description="Moves"),
                Requirement(id="style", kind="visual", description="Looks right"),
                Requirement(
                    id="audio", kind="temporal", description="Music", required_capability="audio_track"
                ),
            ],
        )
    )
    path, _ = store.blob(b"evidence fixture", ".png")
    for id, req, times, kind in [
        ("own", "motion", [0, 1, 2], "frames"),
        ("single", "motion", [0], "frames"),
        ("other", "style", [0, 1, 2], "frames"),
        ("decoded", None, [0, 1, 2], "frames"),
        ("export", None, [], "export"),
    ]:
        store.add_evidence(
            Evidence(
                id=eid(id),
                revision=0,
                kind=kind,
                paths=[path],
                timestamps=times,
                metadata={"requirement_id": req},
            )
        )
    return store


def test_mixed_review_keeps_primary_evidence_gate(tmp_path):
    store = setup_review(tmp_path)
    review = Review(
        requirement_id="motion",
        revision=0,
        evidence_ids=[eid(name) for name in ["own", "other", "decoded", "export"]],
        verdict="pass",
        explanation="Own temporal observation plus supporting video",
    )
    store.add_review(review)
    assert store.reviews()["motion"].verdict == "pass"
    with pytest.raises(ValueError, match="evidence tied"):
        store.add_review(review.model_copy(update={"evidence_ids": [eid("decoded"), eid("export")]}))
    with pytest.raises(ValueError, match="three distinct"):
        store.add_review(review.model_copy(update={"evidence_ids": [eid("single"), eid("decoded")]}))
    store.mutate(0, lambda data: None)
    with pytest.raises(ValueError, match="STALE_EVIDENCE"):
        store.add_review(review.model_copy(update={"revision": 1}))


def test_unavailable_audio_can_fail_without_visual_observation(tmp_path):
    store = setup_review(tmp_path)
    review = Review(
        requirement_id="audio",
        revision=0,
        evidence_ids=[eid("export")],
        verdict="fail",
        explanation="No audio stream is implemented",
    )
    store.add_review(review)
    with pytest.raises(ValueError, match="unavailable"):
        store.add_review(review.model_copy(update={"verdict": "pass"}))
    result = next(r for r in verify(store)["requirements"] if r["id"] == "audio")
    assert (result["verdict"], result["source"]) == ("ignored", "policy")
    assert result["hard"] is False
    context = make_context(tmp_path, plugins=False)
    from maliang.guidance import workflow_progress

    assert all(r["requirement_id"] != "audio" for r in workflow_progress(store)["pending_observations"])
    assert (
        "unavailable" in context.registry.invoke("observe_requirement", {"requirement_id": "audio"})["error"]
    )


def test_old_visuals_expire_by_model_turn_but_new_distinct_images_survive():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,OLD"}}
    fresh = {"type": "image_url", "image_url": {"url": "data:image/png;base64,NEW"}}
    original = [
        ToolMessage(content=[{"type": "text", "text": "Evidence IDs remain"}, image], tool_call_id="old"),
        AIMessage(content="I saw the picture"),
        AIMessage(content="Recording reviews"),
        ToolMessage(content=[fresh], tool_call_id="new"),
    ]
    result = prepare_image_messages(original, 4, max_replay_turns=1)
    assert image not in result[0].content
    assert fresh in result[-1].content
    assert image in original[0].content
    assert result[0].content[0]["text"] == "Evidence IDs remain"
    replay = prepare_image_messages(original[:2], 4, max_replay_turns=1)
    assert image in replay[0].content


def test_finish_draft_batch_ends_normally_and_can_resume(tmp_path):
    class DraftModel(TestModel):
        def _generate(self, messages, **kwargs):
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "id": "draft",
                                    "name": "execute_steps",
                                    "args": {
                                        "expected_revision": 0,
                                        "steps": [
                                            {
                                                "tool": "finish_draft",
                                                "args": {
                                                    "reason": "Audio is unsupported; visual work preserved"
                                                },
                                            }
                                        ],
                                    },
                                }
                            ],
                        )
                    )
                ]
            )

    store = ProjectStore(tmp_path)
    store.create(Artwork(prompt="Music video"))
    context = make_context(tmp_path, plugins=False)
    status = run_agent(context, DraftModel())
    assert status["status"] == "draft"
    assert context.meter.usage["model_calls"] == 1
    assert (tmp_path / "validation.json").exists()
    resumed = make_context(tmp_path, resume=True, plugins=False)
    run_agent(resumed, TestModel(), resume=True)
    assert resumed.meter.usage["model_calls"] > 1
    assert any(
        json.loads(line)["event"] == "run_end" for line in store.path("trace.jsonl").read_text().splitlines()
    )


def test_web_rejects_exhausted_resume_until_budget_changes(tmp_path, monkeypatch):
    import web
    from maliang.store import atomic_json

    root = tmp_path / "root"
    root.mkdir()
    config = json.loads((Path(web.ROOT) / "harness.json").read_text())
    config["budget"]["max_input_tokens"] = 100
    atomic_json(root / "harness.json", config)
    monkeypatch.setattr(web, "ROOT", root)
    project = tmp_path / "run"
    project.mkdir()
    atomic_json(project / "status.json", {"status": "budget_exhausted"})
    atomic_json(project / "usage.json", {"input_tokens": 101, "output_tokens": 5})
    assert "不会清零" in web.resume_budget_problem(project)
    config["budget"]["max_input_tokens"] = 200
    atomic_json(root / "harness.json", config)
    assert web.resume_budget_problem(project) is None


def test_silent_video_ignores_audio_checkpoint_but_preserves_visual_gate(tmp_path):
    from maliang.guidance import workflow_progress

    store = setup_review(tmp_path)
    def add_checkpoint(data):
        data['checkpoints'] = [{'id': 'audio_only', 'description': 'Audio', 'requirement_ids': ['audio']}]
    store.mutate(0, add_checkpoint)
    ctx = make_context(tmp_path, plugins=False)
    result = ctx.registry.invoke('complete_checkpoint', {'checkpoint_id': 'audio_only'})
    assert result['status'] == 'passed'
    report = verify(store)
    assert not report['eligible_to_finalize']
    assert next(r for r in report['requirements'] if r['id'] == 'motion')['hard']
    assert workflow_progress(store)['capability_gaps'] == []
    assert next(r for r in report['requirements'] if r['id'] == 'audio')['verdict'] == 'ignored'
    # This exception applies only to the explicitly silent video workflow.
    store.mutate(1, lambda data: data['spec'].update(format='png'))
    audio = next(r for r in verify(store)['requirements'] if r['id'] == 'audio')
    assert audio['hard'] and audio['verdict'] == 'fail'
