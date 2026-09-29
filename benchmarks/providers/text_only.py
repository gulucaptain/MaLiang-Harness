"""Pro-only transport adaptation: preserve artifacts, omit image input."""
from langchain_openai import ChatOpenAI

NOTICE = (
    "EXPERIMENT: no visual feedback. You are deepseek-v4-pro, a text-only model. "
    "Images from user inputs and rendering/inspection tools are not sent to you. "
    "Use textual tool results, source code and programmatic checks to create and export artwork. "
    "Do not claim to have seen images or to have visually verified requirements. "
    "If finalization requires visual evidence you cannot assess, retain/export a draft and "
    "report the limitation truthfully. Do not fabricate a passing visual review."
)


def omit_images(value):
    if isinstance(value, list):
        return [omit_images(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") in {"input_image", "image_url", "image"}:
            return {"type": "input_text", "text": "[Image omitted: this model has no visual input.]"}
        return {key: omit_images(item) for key, item in value.items()}
    return value


class TextOnlyChatOpenAI(ChatOpenAI):
    def _get_request_payload(self, *args, **kwargs):
        if self.model_name != "deepseek-v4-pro" or not self.use_responses_api:
            raise ValueError("No-vision adapter is restricted to deepseek-v4-pro Responses API")
        payload = omit_images(super()._get_request_payload(*args, **kwargs))
        payload["instructions"] = (payload.get("instructions") or "") + "\n" + NOTICE
        return payload
