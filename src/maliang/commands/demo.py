from __future__ import annotations

from ..models import Artwork, OutputSpec
from ..store import atomic_json
from .output import dump


def demo(args, store=None, settings=None, settings_path=None):
    from ..agent import make_context, run_agent
    from ..demo import ScriptedDemoModel

    store.create(
        Artwork(
            prompt="Offline fixture: a quiet ink pond with two koi; validate rendering and repair.",
            spec=OutputSpec(format=args.format),
            allowed_backends=["canvas"],
            requirements=[
                {
                    "id": "pond_present",
                    "kind": "object_exists",
                    "description": "Pond declared in artwork",
                    "targets": ["pond"],
                }
            ],
        )
    )
    context = make_context(store.root)
    atomic_json(store.path("run_config.json"), {"mode": "scripted-demo", "live_llm": False})
    dump(run_agent(context, ScriptedDemoModel(project_root=str(store.root))))
    return

