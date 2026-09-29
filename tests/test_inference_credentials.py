"""The example template must never become an implicit credential source."""

import sys
from pathlib import Path

import pytest

import inference


def test_example_credentials_are_not_loaded(tmp_path, monkeypatch):
    config = Path(inference.__file__).parent / "examples/harness.quickstart.json"
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=template-must-not-load\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(inference, "__file__", str(tmp_path / "inference.py"))
    monkeypatch.setattr(sys, "argv", ["inference.py", "--config", str(config), "--check"])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        inference.main()
