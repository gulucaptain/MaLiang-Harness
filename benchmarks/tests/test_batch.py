import copy
import json
import os
import sys
import threading

import pytest
from PIL import Image

from benchmarks import batch_eval as batcher
from benchmarks import common, report, worker


def model():
    return dict(
        id="mock-model",
        model="test/model",
        api="chat_completions",
        base_url="https://unit.test/v1",
        api_key_env="UNIT_KEY",
        parallel_tool_calls=None,
    )


def task():
    return dict(
        case_id="case-01",
        modality="image",
        prompt="Draw a blue circle",
        spec={"width": 64, "height": 64, "duration": 3, "fps": 12, "format": "png", "seed": 42},
        input_assets=[],
        allowed_backends=["canvas"],
        workflow_policy="guided",
        allow_generated_assets=False,
        initial_requirements=[
            {"id": "brief_fulfillment", "kind": "visual", "description": "Match the prompt"}
        ],
    )


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    cfg = tmp_path / "models.json"
    common.write(cfg, {"harness_config": str(common.HARNESS / "harness.json"), "models": [model()]})
    baseline = tmp_path / "old.png"
    Image.new("RGB", (64, 64), "blue").save(baseline)
    case = dict(
        task=task(),
        baseline_path=str(baseline),
        baseline_sha256=common.digest(baseline),
        historical_config={"model": "old-gpt", "not_model_input": "solution"},
    )
    monkeypatch.setattr(batcher, "load_cases", lambda *args: [copy.deepcopy(case)])
    monkeypatch.setattr(batcher, "source_fingerprint", lambda: {"fixture": "unchanged"})
    root = tmp_path / "batch"
    plan = batcher.prepare(cfg, root, repeats=2)
    return root, plan


def test_plan_freezes_clean_inputs_and_rejects_tampering(frozen):
    root, plan = frozen
    assert len(plan["jobs"]) == 2
    row = plan["jobs"][0]
    job = common.read(root / row["job_path"])
    assert set(job) == {"job_id", "task", "model", "harness", "repeat"}
    assert "solution" not in json.dumps(job)
    assert "baseline" not in json.dumps(job)
    batcher.validate_plan(root)
    job["task"]["prompt"] = "changed"
    common.write(root / row["job_path"], job)
    with pytest.raises(ValueError, match="Frozen job changed"):
        batcher.validate_plan(root)


def test_resume_does_not_retry_or_overwrite_finished_jobs(frozen):
    root, plan = frozen
    assert len(batcher.jobs_to_run(root, plan, False, False)) == 2
    for row, outcome in zip(plan["jobs"], ["success", "timeout"]):
        common.write((root / row["job_path"]).parent / "attempt-001/result.json", {"outcome": outcome})
    with pytest.raises(ValueError, match="Existing attempts"):
        batcher.jobs_to_run(root, plan, False, False)
    assert batcher.jobs_to_run(root, plan, True, False) == []
    assert batcher.jobs_to_run(root, plan, True, True) == [plan["jobs"][1]]


def test_report_keeps_first_failure_and_counts_all_attempts(frozen):
    root, plan = frozen
    folder = (root / plan["jobs"][0]["job_path"]).parent
    common.write(
        folder / "attempt-001/result.json",
        {"outcome": "timeout", "wall_seconds": 5, "usage": {"input_tokens": 10, "output_tokens": 2}},
    )
    common.write(
        folder / "attempt-002/result.json",
        {"outcome": "success", "wall_seconds": 3, "usage": {"input_tokens": 20, "output_tokens": 3}},
    )
    (root / "ratings.csv").write_text("manual rating must survive")
    report.generate_report(root)
    summary = common.read(root / "summary.json")[0]
    assert summary["planned"] == 2 and summary["finished"] == 1
    assert summary["success_first"] == 0 and summary["success_latest"] == 1
    assert summary["success_rate_latest_over_planned"] == 0.5
    assert summary["total_input_tokens"] == 30 and summary["total_attempt_wall_seconds"] == 8
    assert (root / "ratings.csv").read_text() == "manual rating must survive"


def test_hard_timeout_terminates_child_process(frozen, tmp_path, monkeypatch, capsys):
    root, plan = frozen
    fake = tmp_path / "scripts"
    fake.mkdir()
    (fake / "worker.py").write_text("import time\ntime.sleep(60)\n")
    original_popen = batcher.subprocess.Popen

    def launch_sleeping_worker(command, **kwargs):
        return original_popen([sys.executable, str(fake / "worker.py"), *command[3:]], **kwargs)

    monkeypatch.setattr(batcher.subprocess, "Popen", launch_sleeping_worker)
    outcome = batcher.run_one(
        root,
        plan["jobs"][0],
        dict(os.environ),
        0.6,
        threading.Event(),
        index=2,
        total=50,
        progress_interval=0.1,
    )
    assert outcome == "timeout"
    progress = capsys.readouterr().out
    assert "[开始] 第 2/50 条" in progress
    assert "[运行中] 第 2/50 条" in progress
    result = common.read((root / plan["jobs"][0]["job_path"]).parent / "attempt-001/result.json")
    assert result["exit_code"] != 0
    assert result["wall_seconds"] < 10


def test_environment_precedence_without_shell_execution(tmp_path, monkeypatch):
    marker = tmp_path / "not-created"
    env = tmp_path / "keys.env"
    env.write_text(f'UNIT_KEY=file-key\nOTHER_KEY="$(touch {marker})"\n')
    monkeypatch.setenv("UNIT_KEY", "shell-key")
    values = common.load_environment(env)
    assert values["UNIT_KEY"] == "shell-key"
    assert values["OTHER_KEY"].startswith("$(touch") and not marker.exists()


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_protocol_transport_and_tool_calls(protocol):
    import httpx
    from langchain_core.messages import HumanMessage

    captured = []

    def handle(request):
        body = json.loads(request.content)
        captured.append((request.url.path, body))
        if protocol == "chat_completions":
            payload = {
                "id": "chat-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test/model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "check", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }
        else:
            payload = {
                "id": "resp-test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "test/model",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc1",
                        "call_id": "c1",
                        "name": "check",
                        "arguments": "{}",
                        "status": "completed",
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            }
        return httpx.Response(200, json=payload)

    cfg = model()
    cfg["api"] = protocol
    client = common.model_client(
        cfg,
        {"timeout_seconds": 1, "max_retries": 0, "max_output_tokens_per_call": 100},
        {"UNIT_KEY": "fake-test-key"},
    )
    # Inject a transport on the already constructed SDK clients; no network is used.
    transport = httpx.Client(transport=httpx.MockTransport(handle))
    client.root_client._client = transport
    tool = {
        "type": "function",
        "function": {
            "name": "check",
            "description": "check",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    response = client.bind_tools([tool]).invoke(
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": "inspect"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                ]
            )
        ]
    )
    assert response.tool_calls[0]["name"] == "check"
    assert captured[0][0] == ("/v1/responses" if protocol == "responses" else "/v1/chat/completions")
    assert "parallel_tool_calls" not in captured[0][1]
    assert "tools" in captured[0][1]
    transport.close()


def test_initialization_preserves_reference_and_has_no_gpt_solution(tmp_path):
    from maliang.store import ProjectStore

    ref = tmp_path / "inputs/ref.png"
    ref.parent.mkdir()
    Image.new("RGB", (10, 20), "red").save(ref)
    value = task()
    value["input_assets"] = [
        {
            "id": "input_image",
            "path": "inputs/ref.png",
            "sha256": common.digest(ref),
            "media_type": "image/png",
        }
    ]
    store = ProjectStore(tmp_path / "new-project")
    worker.initialize(value, store, tmp_path)
    art = store.load()
    assert art.revision == 0 and art.program is None and art.plan == []
    assert art.spec.width == 64
    assert art.assets[0].provenance["source"] == "user_upload"
    assert common.digest(store.path(art.assets[0].path)) == common.digest(ref)


@pytest.mark.parametrize("status,expected", [("completed", "success"), ("draft", "not_completed")])
def test_worker_output_gate_not_just_process_exit(tmp_path, monkeypatch, status, expected):
    import maliang.agent

    settings = common.load_config(common.CODE / "models.example.json")["harness"]
    settings["image_generation"]["enabled"] = False
    attempt = tmp_path / "attempt-001"
    attempt.mkdir()
    monkeypatch.setattr(worker, "model_client", lambda *args: object())

    def fake_run(context, model):
        project = context.store.root
        export = "a" * 32
        (project / "outputs").mkdir()
        Image.new("RGB", (64, 64), "blue").save(project / f"outputs/{export}.png")
        common.write(project / "status.json", dict(status=status, revision=0, export_id=export))
        common.write(
            project / "validation.json",
            dict(
                revision=0,
                eligible_to_finalize=True,
                technical_checks=[{"check": "dimensions", "passed": True}],
            ),
        )
        common.write(project / f"evidence/{export}.json", dict(revision=0, paths=[f"outputs/{export}.png"]))

    monkeypatch.setattr(maliang.agent, "run_agent", fake_run)
    worker.execute(dict(task=task(), harness=settings, model=model()), attempt, tmp_path)
    result = common.read(attempt / "result.json")
    assert result["outcome"] == expected and result["media_valid"]
    assert (attempt / "output.png").is_file()


def test_media_validation_rejects_wrong_dimensions(tmp_path):
    target = tmp_path / "bad.png"
    Image.new("RGB", (32, 64), "red").save(target)
    with pytest.raises(ValueError, match="dimensions"):
        common.validate_media(target, task()["spec"])


def test_missing_credentials_starts_no_jobs(frozen, monkeypatch):
    root, plan = frozen
    monkeypatch.setattr(batcher, "load_environment", lambda _: {})
    with pytest.raises(ValueError, match="Missing environment variables"):
        batcher.execute(root)
    assert not batcher.attempts(root, plan["jobs"][0])


def test_video_decoding_and_spec_check(tmp_path):
    import av

    target = tmp_path / "clip.mp4"
    with av.open(str(target), "w") as container:
        stream = container.add_stream("libx264", rate=8)
        stream.width, stream.height, stream.pix_fmt = 64, 64, "yuv420p"
        for i in range(8):
            frame = av.VideoFrame.from_image(Image.new("RGB", (64, 64), (i * 25, 0, 0)))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    spec = dict(width=64, height=64, duration=1, fps=8, format="mp4")
    assert common.validate_media(target, spec)["frames"] == 8
    spec["duration"] = 5
    with pytest.raises(ValueError, match="duration"):
        common.validate_media(target, spec)


def test_new_prompt_without_baseline_is_explicit_and_runnable(tmp_path):
    bench = tmp_path / "bench"
    image = bench / "image"
    folder = image / "case-01"
    folder.mkdir(parents=True)
    value = task()
    common.write(folder / "task.json", value)
    record = dict(
        case_id="case-01",
        task_path="image/case-01/task.json",
        baseline_path=None,
        baseline_sha256=None,
        baseline_status="pending_generation",
    )
    common.write_jsonl(image / "tasks.jsonl", [value])
    common.write_jsonl(image / "manifest.jsonl", [record])
    cases = common.load_cases("image", bench=bench)
    assert len(cases) == 1 and cases[0]["baseline_path"] is None
    del record["baseline_status"]
    common.write_jsonl(image / "manifest.jsonl", [record])
    with pytest.raises(ValueError, match="Missing baseline without explicit"):
        common.load_cases("image", bench=bench)


def test_report_marks_missing_baseline_without_inventing_one(frozen):
    root, plan = frozen
    for row in plan["jobs"]:
        row["baseline_path"] = None
        row["baseline_sha256"] = None
    common.write(root / "plan.json", plan)
    report.generate_report(root)
    assert all(not r["baseline_available"] for r in common.jsonl(root / "results.jsonl"))
