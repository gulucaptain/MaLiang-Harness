import json

import pytest
from PIL import Image

from maliang.agent import make_context
from maliang.models import Artwork, OutputSpec
from maliang.store import ProjectStore


@pytest.mark.rendering
def test_export_observation_and_decode_reuse_with_revision_invalidation(tmp_path):
    store = ProjectStore(tmp_path)
    store.create(Artwork(prompt='Dots', spec=OutputSpec(width=64, height=64, duration=1, fps=3),
                         requirements=[{'id': 'dots', 'description': 'Visible colored dots'}]))
    ctx = make_context(tmp_path, plugins=False)
    def write(revision, color):
        return ctx.registry.invoke('write_program', {'expected_revision': revision, 'backend': 'canvas',
            'source': f'function(ctx,t){{ctx.fillStyle="{color}";ctx.fillRect(10+t,10,20,20);}}'})
    write(0, 'red')
    a = json.loads(ctx.registry.invoke('observe_requirement', {'requirement_id': 'dots'})[0]['text'])
    b = json.loads(ctx.registry.invoke('observe_requirement', {'requirement_id': 'dots'})[0]['text'])
    assert a['id'] == b['id']
    with Image.open(store.path(a['paths'][0])) as im:
        assert im.getpixel((0, 0))[:3] == (255, 255, 255)
        assert im.getpixel((15, 15))[:3] == (255, 0, 0)
    batch = ctx.registry.invoke('execute_steps', {'expected_revision': 1, 'steps': [
        {'tool': 'export_artifact', 'args': {}},
        {'tool': 'inspect_video', 'args': {'evidence_id': '$latest_export', 'samples': 3}}]})
    assert json.loads(batch[0]['text'])['remaining_steps'] == 0
    first = ctx.renderers.export()
    assert first.paths[0].startswith("exports/")
    assert not store.path("outputs").exists()
    usage = ctx.meter.usage['frames']
    assert ctx.renderers.export().id == first.id
    assert ctx.meter.usage['frames'] == usage
    decoded = ctx.renderers.inspect_video(first.id, 3)
    assert ctx.renderers.inspect_video(first.id, 3).id == decoded.id
    store.path(decoded.paths[0]).unlink()
    assert ctx.renderers.inspect_video(first.id, 3).id != decoded.id
    # Missing output must be regenerated, not returned as an apparently successful export.
    store.path(first.paths[0]).unlink()
    second = ctx.renderers.export()
    assert second.id != first.id
    write(1, 'blue')
    with pytest.raises(ValueError, match='STALE_EVIDENCE'):
        ctx.renderers.inspect_video(second.id)
    c = json.loads(ctx.registry.invoke('observe_requirement', {'requirement_id': 'dots'})[0]['text'])
    assert c['id'] != a['id']
    assert ctx.renderers.export().revision == 2


@pytest.mark.rendering
def test_png_export_rebuilds_corrupt_cache(tmp_path):
    store = ProjectStore(tmp_path)
    store.create(Artwork(prompt='Image', spec=OutputSpec(width=64, height=64, format='png')))
    ctx = make_context(tmp_path, plugins=False)
    ctx.registry.invoke('write_program', {'expected_revision': 0, 'backend': 'canvas',
        'source': 'function(ctx){ctx.fillStyle="teal";ctx.fillRect(0,0,64,64);}'})
    first = ctx.renderers.export()
    assert ctx.renderers.export().id == first.id
    store.path(first.paths[0]).write_bytes(b'broken')
    restored = ctx.renderers.export()
    assert restored.id != first.id
    with Image.open(store.path(restored.paths[0])) as image:
        assert image.size == (64, 64)


def test_only_published_video_is_in_outputs_and_history_survives(tmp_path):
    from maliang.models import Evidence
    from web import resolve_media_path
    store = ProjectStore(tmp_path)
    store.create(Artwork(prompt='Video', spec=OutputSpec(width=64, height=64)))
    for eid in ['a' * 32, 'b' * 32]:
        path = f'exports/{eid}.mp4'
        store.path(path).parent.mkdir(exist_ok=True)
        store.path(path).write_bytes(eid.encode())
        store.add_evidence(Evidence(id=eid, revision=0, kind='export', paths=[path]))
    assert not store.path('outputs').exists()
    store.publish_video('a' * 32)
    store.publish_video('b' * 32)
    assert [p.name for p in store.path('outputs').iterdir()] == ['b' * 32 + '.mp4']
    assert store.evidence('a' * 32).paths == ['exports/' + 'a' * 32 + '.mp4']
    assert resolve_media_path(tmp_path, 'outputs/' + 'a' * 32 + '.mp4').is_file()
