import json
from pathlib import Path

import pytest
from PIL import Image

from benchmarks import batch_eval, common
from benchmarks.datasets import load_tasks


def write_tasks(path, rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def task(case_id='circle'):
    return {'case_id': case_id, 'prompt': 'Draw a blue circle',
            'spec': {'width': 64, 'height': 64, 'format': 'png'}}


def test_portable_dataset_planning_freezes_reference_bytes(tmp_path):
    image = tmp_path / 'reference.png'
    Image.new('RGB', (16, 16), 'blue').save(image)
    row = task()
    row['input_assets'] = [{'id': 'reference', 'path': image.name, 'media_type': 'image/png'}]
    dataset = tmp_path / 'tasks.jsonl'
    write_tasks(dataset, [row])
    batch = tmp_path / 'batch'
    plan = batch_eval.prepare(common.CODE / 'models.example.json', batch, dataset=dataset)
    job = common.read(batch / plan['jobs'][0]['job_path'])
    asset = job['task']['input_assets'][0]
    assert (batch / asset['path']).read_bytes() == image.read_bytes()
    assert asset['sha256'] == common.digest(image)
    image.unlink()
    dataset.unlink()
    # Running a frozen plan does not depend on the original mutable dataset.
    assert batch_eval.validate_plan(batch)['jobs'] == plan['jobs']
    assert plan['jobs'][0]['baseline_path'] is None


def test_dataset_filters_and_rejects_duplicate_ids(tmp_path):
    dataset = tmp_path / 'tasks.jsonl'
    write_tasks(dataset, [task('a'), task('b')])
    assert [c['task']['case_id'] for c in load_tasks(dataset, case_ids=['b'])] == ['b']
    assert len(load_tasks(dataset, limit=1)) == 1
    with pytest.raises(ValueError, match='Unknown case'):
        load_tasks(dataset, case_ids=['missing'])
    write_tasks(dataset, [task(), task()])
    with pytest.raises(ValueError, match='Duplicate'):
        load_tasks(dataset)


def test_dataset_rejects_escaping_asset_path(tmp_path):
    dataset_dir = tmp_path / 'dataset'
    dataset_dir.mkdir()
    (tmp_path / 'outside.png').write_bytes(b'private')
    row = task()
    row['input_assets'] = [{'id': 'reference', 'path': '../outside.png', 'media_type': 'image/png'}]
    dataset = dataset_dir / 'tasks.jsonl'
    write_tasks(dataset, [row])
    with pytest.raises(ValueError, match='escapes'):
        load_tasks(dataset)


def test_default_authored_examples_plan_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv('MODEL_API_KEY', raising=False)
    batch = tmp_path / 'batch'
    plan = batch_eval.prepare(common.CODE / 'models.example.json', batch,
                              dataset=common.CODE / 'examples/tasks.jsonl')
    assert len(plan['jobs']) == 2
    assert {job['modality'] for job in plan['jobs']} == {'image', 'video'}
    assert not plan['harness']['image_generation']['enabled']
    assert all(Path(batch / row['job_path']).is_file() for row in plan['jobs'])
