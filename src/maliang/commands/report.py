from __future__ import annotations

import json

from .output import dump


def report(args, store=None, settings=None, settings_path=None):
    from ..verification import latest_export, verify

    dump(
        {
            "status": json.loads(store.path("status.json").read_text()),
            "validation": verify(store, latest_export(store)),
            "usage": json.loads(store.path("usage.json").read_text())
            if store.path("usage.json").exists()
            else {},
        }
    )
    return

