from __future__ import annotations

import json

from .output import dump


def tools(args, store=None, settings=None, settings_path=None):
    from ..agent import make_context

    context = make_context(store.root, resume=True)
    if args.command == "capabilities":
        dump({"tools": context.registry.describe(), "backends": context.renderers.available()})
    else:
        result = context.registry.invoke(args.name, json.loads(args.args))
        # Do not print binary image payloads into the terminal.
        if isinstance(result, list):
            result = [x for x in result if x.get("type") == "text"]
        dump(result)
    return

