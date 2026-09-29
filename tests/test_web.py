from pathlib import Path

import web


class FakeProcess:
    def poll(self):
        return None


def test_web_log_does_not_make_new_project_nonempty(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    logs = runs / ".web-logs"
    monkeypatch.setattr(web, "RUNS", runs)
    monkeypatch.setattr(web, "WEB_LOGS", logs)
    calls = []

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(web.subprocess, "Popen", popen)
    manager = web.Runs()
    run_id = manager.start("画一只猫", "image", orientation="portrait", resolution="high")
    project = runs / run_id

    assert not project.exists()
    assert (logs / f"{run_id}.log").is_file()
    command = calls[0][0]
    assert Path(command[command.index("--project") + 1]) == project
    assert command[command.index("--orientation") + 1] == "portrait"
    assert command[command.index("--resolution") + 1] == "high"


def test_history_survives_server_restart_and_resume_appends_log(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    logs = runs / ".web-logs"
    run_id = "web-20260921-120528-286359"
    project = runs / run_id
    project.mkdir(parents=True)
    (project / "artwork.json").write_text("{}")
    logs.mkdir()
    log_path = logs / f"{run_id}.log"
    log_path.write_text("previous attempt\n")
    monkeypatch.setattr(web, "RUNS", runs)
    monkeypatch.setattr(web, "WEB_LOGS", logs)
    calls = []

    def popen(command, **kwargs):
        calls.append(command)
        return FakeProcess()

    monkeypatch.setattr(web.subprocess, "Popen", popen)
    manager = web.Runs()
    assert manager.project(run_id) == project
    assert manager.start("", "image", run_id) == run_id
    assert "--resume" in calls[0] and "--prompt" not in calls[0]
    assert log_path.read_text() == "previous attempt\n"


def test_trace_pagination_preserves_events_and_partial_lines(tmp_path):
    import json

    path = tmp_path / "trace.jsonl"
    path.write_bytes(b"".join((json.dumps({"n": n}) + "\n").encode() for n in range(450)) + b'{"n":450')
    seen, offset = [], 0
    for _ in range(3):
        events, offset = web.read_events(path, offset)
        seen.extend(events)
    assert [event["n"] for event in seen] == list(range(450))
    with path.open("ab") as stream:
        stream.write(b"}\n")
    assert web.read_events(path, offset)[0] == [{"n": 450}]


def test_duplicate_evidence_content_has_same_identity_without_dropping_events(tmp_path):
    project = tmp_path.resolve()
    (project / "preview.png").write_bytes(b"same png content")
    (project / "export.png").write_bytes(b"same png content")
    events = [
        {"event": "evidence", "id": "a", "paths": ["preview.png"]},
        {"event": "evidence", "id": "b", "paths": ["export.png"]},
    ]
    web.event_media_hashes(project, events)
    assert len(events) == 2
    assert events[0]["media_hashes"]["preview.png"] == events[1]["media_hashes"]["export.png"]


def test_edit_start_validates_version_and_preserves_source(tmp_path, monkeypatch):
    import pytest

    from maliang.models import Artwork
    from maliang.store import ProjectStore

    runs = tmp_path / "runs"
    source = ProjectStore(runs / "inference-example")
    source.create(Artwork(prompt="source"))
    monkeypatch.setattr(web, "RUNS", runs)
    monkeypatch.setattr(web, "WEB_LOGS", runs / ".web-logs")
    calls = []
    monkeypatch.setattr(
        web.subprocess, "Popen", lambda command, **kwargs: calls.append(command) or FakeProcess()
    )
    manager = web.Runs()
    with pytest.raises(ValueError):
        manager.start("edit", "image", edit_from="inference-example", revision=99)
    with pytest.raises(ValueError):
        manager.start("edit", "image", edit_from="inference-example", revision=0, image="data")
    run_id = manager.start("edit", "image", edit_from="inference-example", revision=0)
    assert run_id != "inference-example"
    assert "--edit-from" in calls[0] and "--revision" in calls[0]
    assert "--resume" not in calls[0] and "--task" not in calls[0]
    assert source.load().revision == 0
    with pytest.raises(ValueError):
        manager.project("../outside")


def test_output_limit_validation_and_forwarding(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(web, "WEB_LOGS", tmp_path / "logs")
    calls = []
    monkeypatch.setattr(web.subprocess, "Popen", lambda command, **kwargs: calls.append(command) or FakeProcess())
    manager = web.Runs()
    for invalid in (0, -1, True, 1.5, "24000", 1000001):
        with pytest.raises(ValueError, match="单次输出上限"):
            manager.start("video", "video", max_output_tokens=invalid)
    assert not calls
    manager.start("video", "video", max_output_tokens=24000)
    assert calls[0][calls[0].index("--max-output-tokens") + 1] == "24000"
