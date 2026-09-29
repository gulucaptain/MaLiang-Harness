import runpy
import sys
import time
from pathlib import Path

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite import SqliteSaver

from maliang.agent import build_agent, make_context, run_agent
from maliang.models import Artwork
from maliang.store import ProjectStore, atomic_json

original = SqliteSaver.put


def slow(self, *args, **kwargs):
    time.sleep(0.04)
    return original(self, *args, **kwargs)


SqliteSaver.put = slow
base = runpy.run_path("tests/test_agent.py")["TestModel"]


class Model(base):
    def _generate(self, messages, **kwargs):
        count = sum(isinstance(m, AIMessage) for m in messages)
        tool = (
            {"id": str(count), "name": "read_artwork", "args": {}}
            if count < 8
            else {"id": str(count), "name": "finalize_artwork", "args": {"export_id": "done"}}
        )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[tool]))])


p = Path(sys.argv[1])
ProjectStore(p).create(Artwork(prompt="Offline checkpoint stress"))
c = make_context(p, plugins=False)


def finish(export_id):
    s = {"status": "completed", "revision": 0, "export_id": export_id}
    atomic_json(p / "status.json", s)
    return s


c.registry.capabilities["finalize_artwork"].handler = finish
print(run_agent(c, Model()), flush=True)
assert c.meter.usage["model_calls"] == 9
# The terminal checkpoint must be durable and have no pending execution.

with SqliteSaver.from_conn_string(str(p / "checkpoints.sqlite")) as saver:
    snapshot = build_agent(c, Model(), saver).get_state({"configurable": {"thread_id": "artwork"}})
    assert not snapshot.next, snapshot.next
print("checkpoint drained; no pending tasks", flush=True)
