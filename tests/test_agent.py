from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from maliang.agent import make_context, run_agent
from maliang.models import Artwork, Budget
from maliang.store import ProjectStore, atomic_json


class TestModel(BaseChatModel):
    __test__ = False

    @property
    def _llm_type(self):
        return "offline-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens_from_messages(self, messages, tools=None):
        return 100

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        calls = [m for m in messages if isinstance(m, AIMessage) and m.tool_calls]
        if not calls:
            msg = AIMessage(content="", tool_calls=[{"id": "read", "name": "read_artwork", "args": {}}])
        else:
            msg = AIMessage(
                content="Completed!",
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        return ChatResult(generations=[ChatGeneration(message=msg)])


def test_real_deepagents_loop_checkpoints_and_cannot_self_declare_success(tmp_path):
    ProjectStore(tmp_path).create(Artwork(prompt="A landscape"))
    ctx = make_context(tmp_path, plugins=False)
    status = run_agent(ctx, TestModel())
    assert status["status"] == "incomplete"
    assert ctx.meter.usage["model_calls"] == 2
    assert ctx.meter.usage["input_tokens"] == 10
    assert ctx.store.path("checkpoints.sqlite").stat().st_size > 0
    resumed = make_context(tmp_path, resume=True, plugins=False)
    status = run_agent(resumed, TestModel(), resume=True)
    assert status["status"] == "incomplete"
    assert resumed.meter.usage["model_calls"] == 3


def test_model_budget_is_enforced_inside_deepagents(tmp_path):
    ProjectStore(tmp_path).create(Artwork(prompt="A landscape"))
    ctx = make_context(tmp_path, Budget(max_model_calls=1), plugins=False)
    status = run_agent(ctx, TestModel())
    assert status["status"] == "budget_exhausted"
    assert ctx.meter.usage["model_calls"] == 1


def test_agent_stops_before_another_model_call_after_finalization(tmp_path):
    class FinalizingModel(TestModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            msg = AIMessage(
                content="",
                tool_calls=[{"id": "finish", "name": "finalize_artwork", "args": {"export_id": "done"}}],
            )
            return ChatResult(generations=[ChatGeneration(message=msg)])

    ProjectStore(tmp_path).create(Artwork(prompt="A landscape"))
    ctx = make_context(tmp_path, plugins=False)

    def completed(export_id):
        status = {"status": "completed", "revision": 0, "export_id": export_id}
        atomic_json(ctx.store.path("status.json"), status)
        return status

    # Isolate the agent's exit behavior; the real finalization gate is tested separately.
    ctx.registry.capabilities["finalize_artwork"].handler = completed
    status = run_agent(ctx, FinalizingModel())
    assert status["status"] == "completed"
    assert ctx.meter.usage["model_calls"] == 1
    assert any(
        '"event": "run_end"' in line for line in ctx.store.path("trace.jsonl").read_text().splitlines()
    )


def test_slow_checkpoint_writer_finishes_without_deadlock(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "tests/helpers/checkpoint_finish.py"), str(tmp_path)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "checkpoint drained; no pending tasks" in result.stdout
