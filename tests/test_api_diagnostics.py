import httpx
import openai

from maliang.api_diagnostics import explain_api_error


def test_connection_error_mentions_active_proxy_without_credentials(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://secret@example.test:7890")
    error = openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))

    explanation = explain_api_error(error)

    assert "example.test:7890" in explanation
    assert "secret" not in explanation


def test_missing_model_error_has_actionable_configuration_names():
    response = httpx.Response(404, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    error = openai.NotFoundError("model not found", response=response, body=None)

    explanation = explain_api_error(error)

    assert "MALIANG_MODEL" in explanation
    assert "OPENAI_BASE_URL" in explanation
    assert "model not found" not in explanation


def test_timeout_is_not_misdiagnosed_as_dns_failure():
    error = openai.APITimeoutError(request=httpx.Request("POST", "https://example.test/v1/responses"))
    explanation = explain_api_error(error)
    assert "请求超时" in explanation
    assert "timeout_seconds" in explanation
    assert "检查 DNS" not in explanation
