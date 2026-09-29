"""Deletion must remove only the requested run and its staging files."""
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import web


@pytest.fixture
def deletion_store(tmp_path, monkeypatch):
    runs = tmp_path / 'runs'
    runs.mkdir()
    monkeypatch.setattr(web, 'RUNS', runs)
    monkeypatch.setattr(web, 'WEB_LOGS', runs / '.web-logs')
    manager = web.Runs()
    monkeypatch.setattr(web, 'RUN_MANAGER', manager)
    for name in ['original', 'child']:
        project = runs / name
        project.mkdir()
        (project / 'artwork.json').write_text('{}')
        (project / 'image.png').write_bytes(b'fixture')
    for directory, suffix in [('.web-logs', 'log'), ('.web-inputs', 'png')]:
        (runs / directory).mkdir()
        (runs / directory / f'original.{suffix}').write_bytes(b'fixture')
    return manager, runs


def test_delete_requires_confirmation_and_removes_local_files(deletion_store):
    _, runs = deletion_store
    server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/api/runs/delete'

    def request(body, origin=None):
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = origin
        return urlopen(Request(url, data=json.dumps(body).encode(), headers=headers))

    try:
        with pytest.raises(HTTPError) as error:
            request({'run': 'original'})
        assert error.value.code == 400
        with pytest.raises(HTTPError) as error:
            request({'run': 'original', 'confirmed': True}, 'https://example.com')
        assert error.value.code == 403
        assert (runs / 'original').is_dir()
        with request({'run': 'original', 'confirmed': True}) as response:
            assert response.status == 200
        assert not (runs / 'original').exists()
        assert not (runs / '.web-logs/original.log').exists()
        assert not (runs / '.web-inputs/original.png').exists()
        assert (runs / 'child/image.png').read_bytes() == b'fixture'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_delete_rejects_running_tasks_and_unsafe_paths(deletion_store, tmp_path):
    manager, runs = deletion_store
    manager.processes['active'] = type('Process', (), {'poll': lambda self: None})()
    with pytest.raises(ValueError, match='正在运行'):
        manager.delete('original')
    assert (runs / 'original/image.png').exists()
    manager.processes.clear()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    (runs / 'linked').symlink_to(outside, target_is_directory=True)
    for name in ['../outside', 'linked', '', None]:
        with pytest.raises(ValueError):
            manager.delete(name)
    assert (outside / 'keep').read_text() == 'keep'
    (runs / 'original/link').symlink_to(outside, target_is_directory=True)
    manager.delete('original')
    assert (outside / 'keep').read_text() == 'keep'
