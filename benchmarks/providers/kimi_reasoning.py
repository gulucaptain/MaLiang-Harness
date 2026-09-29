"""Keep Kimi reasoning history in multi-turn Chat Completions requests."""

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI


class KimiReasoningChatOpenAI(ChatOpenAI):
    def __init__(self, **kwargs):
        # The non-streaming response contains complete reasoning_content.
        kwargs.setdefault("disable_streaming", True)
        super().__init__(**kwargs)

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        raw = response if isinstance(response, dict) else response.model_dump(warnings=False)
        for choice, generation in zip(raw.get("choices", []), result.generations):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning is not None and isinstance(generation.message, AIMessage):
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        original = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        outbound = payload.get("messages", [])
        if len(original) != len(outbound):
            raise ValueError("Kimi message conversion changed length; cannot preserve thinking history")
        for message, encoded in zip(original, outbound):
            if isinstance(message, AIMessage) and "reasoning_content" in message.additional_kwargs:
                encoded["reasoning_content"] = message.additional_kwargs["reasoning_content"]
        return payload
