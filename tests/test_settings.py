import json
import subprocess
import sys
from pathlib import Path

import pytest

from maliang import agent, cli
from maliang.models import Artwork, OutputSpec
from maliang.settings import load_harness_settings
from maliang.store import ProjectStore

ROOT_CONFIG = Path(__file__).resolve().parents[1] / "harness.json"


def test_harness_file_validates_and_unknown_options_fail(tmp_path):
    settings = load_harness_settings(ROOT_CONFIG)
    assert settings.budget.max_input_tokens == 1200000
    assert settings.profile("image").output.format == "png"
    assert settings.profile("video").output.format == "mp4"
    assert settings.profile("image").prompt != settings.profile("video").prompt

    data = json.loads(ROOT_CONFIG.read_text())
    data["budget"]["max_input_token"] = 100
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="max_input_token"):
        load_harness_settings(path)

    data = json.loads(ROOT_CONFIG.read_text())
    data["video"] = {
        "prompt": "A short scene",
        "output": {"width": 640, "height": 360, "format": "mp4"},
        "allowed_backends": ["scene2d", "canvas"],
    }
    data["video"]["output"]["format"] = "png"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="video.output.format"):
        load_harness_settings(path)

    old_style = json.loads(ROOT_CONFIG.read_text())
    old_style.pop("image")
    old_style.pop("video", None)
    old_style["defaults"] = {
        "output": {"width": 512, "height": 512, "duration": 3, "fps": 12, "format": "png"},
        "allowed_backends": ["scene2d", "canvas", "svg"],
        "allow_generated_assets": False,
    }
    path.write_text(json.dumps(old_style))
    legacy = load_harness_settings(path)
    assert legacy.profile("image").output.format == "png"
    assert legacy.profile("video").output.format == "mp4"


def test_low_level_init_uses_config_defaults_and_cli_override(tmp_path, monkeypatch):
    project = tmp_path / "art"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "maliang",
            "init",
            str(project),
            "--prompt",
            "A poster",
            "--harness-config",
            str(ROOT_CONFIG),
            "--width",
            "640",
        ],
    )
    cli.main()

    artwork = ProjectStore(project).load()
    assert artwork.spec.width == 640
    assert artwork.spec.height == load_harness_settings(ROOT_CONFIG).profile("image").output.height
    assert artwork.spec.format == "png"
    assert artwork.allowed_backends == ["scene2d", "canvas", "svg"]


def test_low_level_video_profile_and_user_prompt(tmp_path, monkeypatch):
    profile = load_harness_settings(ROOT_CONFIG).profile("video")
    default_project = tmp_path / "video-default"
    monkeypatch.setattr(
        sys,
        "argv",
        ["maliang", "init", str(default_project), "--harness-config", str(ROOT_CONFIG), "--task", "video"],
    )
    cli.main()
    default_art = ProjectStore(default_project).load()
    assert default_art.prompt == profile.prompt
    assert default_art.spec.format == "mp4"
    assert default_art.spec.width == profile.output.width
    assert default_art.allowed_backends == profile.allowed_backends
    clarity = next(r for r in default_art.requirements if r.id == "render_clarity")
    assert clarity.kind == "temporal"
    assert clarity.observation.samples == 3
    assert clarity.observation.end == default_art.spec.duration - 1 / default_art.spec.fps

    user_project = tmp_path / "video-user"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "maliang",
            "init",
            str(user_project),
            "--harness-config",
            str(ROOT_CONFIG),
            "--task",
            "video",
            "--prompt",
            "Animate a red circle",
        ],
    )
    cli.main()
    assert ProjectStore(user_project).load().prompt == "Animate a red circle"


def test_inference_task_routes_prompt_and_format_without_live_api(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT_CONFIG.parent))
    import inference

    original_run = subprocess.run

    def fake_run(command, check=False):
        action = command[3]
        if action == "doctor":
            return subprocess.CompletedProcess(command, 0)
        if action == "run":
            assert command[command.index("--max-output-tokens") + 1] == "24000"
            project = Path(command[4])
            (project / "status.json").write_text('{"status":"completed"}')
            return subprocess.CompletedProcess(command, 0)
        if action == "report":
            return subprocess.CompletedProcess(command, 0)
        return original_run(command, check=check)

    monkeypatch.setattr(inference.subprocess, "run", fake_run)
    fake_env = tmp_path / "test.env"
    fake_env.write_text("OPENAI_API_KEY=offline-test-key\n")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    monkeypatch.chdir(ROOT_CONFIG.parent)

    for kind in ("image", "video"):
        project = tmp_path / kind
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "inference.py",
                "--max-output-tokens", "24000",
                "--task",
                kind,
                "--prompt",
                f"User {kind} request",
                "--env",
                str(fake_env),
                "--project",
                str(project),
            ],
        )
        assert inference.main() == 0
        artwork = ProjectStore(project).load()
        assert artwork.prompt == f"User {kind} request"
        assert artwork.spec.format == ("png" if kind == "image" else "mp4")


def test_low_level_run_snapshots_effective_model_and_budget(tmp_path, monkeypatch):
    project = tmp_path / "art"
    ProjectStore(project).create(Artwork(prompt="Poster", spec=OutputSpec(format="png")))
    seen = {}

    def fake_model(model_name, **options):
        seen["model"] = model_name
        seen["options"] = options
        return object()

    def fake_run_agent(context, model, resume):
        seen["resume_usage"] = context.meter.usage["input_tokens"] if resume else None
        return {"status": "mocked"}

    monkeypatch.setattr(agent, "create_model", fake_model)
    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        sys,
        "argv",
        ["maliang", "run", str(project), "--harness-config", str(ROOT_CONFIG), "--max-output-tokens", "24000"],
    )
    cli.main()

    snapshot = json.loads((project / "run_config.json").read_text())
    assert seen["model"] == "gpt-6-astra"
    assert seen["options"] == {"timeout": load_harness_settings(ROOT_CONFIG).model.timeout_seconds, "max_retries": 0, "max_tokens": 24000}
    assert seen["resume_usage"] is None
    assert snapshot["budget"]["max_input_tokens"] == 1200000
    assert snapshot["model_options"] == seen["options"]
    assert snapshot["image_generation"]["workflow"] == "code_directed"

    (project / "usage.json").write_text(
        json.dumps(
            {
                "model_calls": 24,
                "tool_calls": 22,
                "frames": 106,
                "input_tokens": 411302,
                "output_tokens": 4038,
                "elapsed_seconds": 298,
                "asset_api_calls": 0,
            }
        )
    )
    changed_config = tmp_path / "changed-harness.json"
    changed = json.loads(ROOT_CONFIG.read_text())
    changed["image_generation"]["workflow"] = "planned_assets"
    changed_config.write_text(json.dumps(changed))
    monkeypatch.setattr(
        sys,
        "argv",
        ["maliang", "run", str(project), "--harness-config", str(changed_config), "--resume"],
    )
    cli.main()
    assert seen["resume_usage"] == 411302
    assert seen["options"]["max_tokens"] == 24000
    assert (
        json.loads((project / "run_config.json").read_text())["image_generation"]["workflow"]
        == "code_directed"
    )
