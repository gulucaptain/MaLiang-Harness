from __future__ import annotations

from .output import dump


def inspect(args, store=None, settings=None, settings_path=None):
    dump(store.load().model_dump())
    return

