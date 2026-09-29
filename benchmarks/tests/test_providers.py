import pytest
from langchain_core.messages import AIMessage, HumanMessage

from benchmarks import common
from benchmarks.providers import model_class


@pytest.mark.parametrize('adapter', ['kimi_reasoning', 'kimi_k3'])
def test_kimi_adapter_preserves_reasoning_across_turns(adapter):
    client = model_class(adapter)(model='offline-test', api_key='offline-key', use_responses_api=False)
    message = AIMessage(content='first answer', additional_kwargs={'reasoning_content': 'saved reasoning'})
    payload = client._get_request_payload([message, HumanMessage(content='continue')])
    assert payload['messages'][0]['reasoning_content'] == 'saved reasoning'


def test_text_only_adapter_omits_images_and_discloses_limitation():
    client = model_class('text_only')(model='deepseek-v4-pro', api_key='offline-key', use_responses_api=True)
    payload = client._get_request_payload([HumanMessage(content=[
        {'type': 'text', 'text': 'inspect'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,private-image'}},
    ])])
    assert 'private-image' not in str(payload)
    assert 'no visual feedback' in payload['instructions']


def test_unknown_adapter_rejected_before_execution(tmp_path):
    config = common.read(common.CODE / 'models.example.json')
    config['harness_config'] = str(common.HARNESS / 'examples/harness.quickstart.json')
    config['models'][0]['adapter'] = 'not-a-provider'
    path = tmp_path / 'models.json'
    common.write(path, config)
    with pytest.raises(ValueError, match='Unknown model adapter'):
        common.load_config(path)
