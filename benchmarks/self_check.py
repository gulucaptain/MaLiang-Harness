"""Offline Harness/render smoke check. Uses the existing deterministic demo, never an API."""

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from .common import CODE, load_config, read, write


def main():
    from playwright.sync_api import sync_playwright

    from maliang.demo import ScriptedDemoModel

    from . import worker

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    output = (args.output or CODE / "results" / f"offline-check-{stamp}").resolve()
    if output.exists():
        parser.error("output must be a new directory")
    output.mkdir(parents=True)
    scratch = output / "tmp"
    scratch.mkdir()
    os.environ["TMPDIR"] = str(scratch)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    # The CLI never loads credential files, and the fixture replaces the model factory.
    settings = load_config(CODE / "models.example.json")["harness"]
    settings["image_generation"]["enabled"] = False
    with sync_playwright() as pw:
        os.environ.setdefault("MALIANG_CHROMIUM", pw.chromium.executable_path)
    worker.model_client = lambda *_: ScriptedDemoModel(project_root=str(output / "project"))
    task = dict(
        case_id="offline-check-only",
        modality="image",
        prompt="Offline pond fixture; not benchmark data.",
        spec=dict(width=160, height=160, duration=3, fps=12, format="png", seed=42),
        input_assets=[],
        allowed_backends=["canvas"],
        workflow_policy="legacy",
        allow_generated_assets=False,
        initial_requirements=[
            dict(id="pond_present", kind="object_exists", description="Pond declared", targets=["pond"])
        ],
    )
    model = dict(
        id="offline",
        model="scripted-fixture",
        api="chat_completions",
        base_url="https://offline.invalid/v1",
        api_key_env="UNUSED_OFFLINE_KEY",
    )
    code = worker.execute(dict(task=task, harness=settings, model=model), output, output)
    config = read(output / "project/run_config.json", {})
    config.update(live_llm=False, note="Deterministic offline fixture; no API calls; not benchmark data.")
    write(output / "project/run_config.json", config)
    result = read(output / "result.json")
    print(f"Offline check: {result['outcome']}; records: {output}")
    if code:
        print(f"See {output / 'project/status.json'} and trace.jsonl for the rendering diagnostic.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
