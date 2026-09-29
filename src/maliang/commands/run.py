from __future__ import annotations

import json
import os
from pathlib import Path

from ..agent import create_model, make_context, run_agent, run_single_shot
from ..models import Budget
from ..store import atomic_json
from .output import dump


def run(args, store, settings=None, settings_path=None):
    mode = args.mode or (settings.mode if settings else "maliang")
    if mode != "maliang" and store.load().workflow_policy == "guided":
        raise ValueError(
            "Baseline runs require init --workflow legacy; create a separate task for comparison"
        )
    model_name = args.model or (settings.model.name if settings else os.environ.get("MALIANG_MODEL"))
    if model_name:
        os.environ["MALIANG_MODEL"] = model_name
    model_options = (
        {
            "timeout": settings.model.timeout_seconds,
            "max_retries": settings.model.max_retries,
            "max_tokens": settings.model.max_output_tokens_per_call,
        }
        if settings
        else {}
    )
    if args.max_output_tokens is not None:
        if not 1 <= args.max_output_tokens <= 1000000:
            raise ValueError("单次输出上限须为 1–1000000")
        model_options["max_tokens"] = args.max_output_tokens
    elif args.resume and store.path("run_config.json").exists():
        previous = json.loads(store.path("run_config.json").read_text())
        saved_limit = previous.get("model_options", {}).get("max_tokens")
        if saved_limit is not None:
            model_options["max_tokens"] = saved_limit
    model = create_model(model_name, **model_options)
    budget = (
        Budget.model_validate_json(Path(args.budget).read_text())
        if args.budget
        else settings.budget
        if settings
        else Budget()
    )
    old = {}
    config_path = store.path("run_config.json")
    if config_path.exists():
        old = json.loads(config_path.read_text())
        if not args.resume:
            raise ValueError("Existing run: use --resume or create a new project")
        if old["mode"] != mode:
            raise ValueError("Cannot change experiment mode on resume")
    atomic_json(
        config_path,
        {
            "mode": mode,
            "model": model_name,
            "model_options": model_options,
            "budget": budget.model_dump(),
            "efficiency": settings.efficiency.model_dump() if settings else old.get("efficiency"),
            "harness_config": str(settings_path.resolve()) if settings and settings_path else None,
            "image_generation": (
                old.get("image_generation")
                if args.resume and "image_generation" in old
                else (
                    settings.image_generation.model_dump()
                    if settings and "image_generation" in settings.model_fields_set
                    else None
                )
            ),
            "live_llm": True,
        },
    )
    context = make_context(store.root, budget, mode, args.resume)
    from ..guidance import runtime_capabilities

    atomic_json(store.path("environment.json"), runtime_capabilities(context))
    result = (
        run_single_shot(context, model)
        if mode == "single-shot"
        else run_agent(context, model, args.resume)
    )
    dump(result)

