"""Browser contract tests for chat UI using deterministic API fixtures, never a model."""

import io
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

import web


@pytest.mark.rendering
def test_chat_artifacts_process_edit_and_mobile(tmp_path):
    import av
    from playwright.sync_api import expect, sync_playwright

    video_path = tmp_path / 'fixture.mp4'
    with av.open(str(video_path), mode='w') as container:
        stream = container.add_stream('libx264', rate=2)
        stream.width, stream.height, stream.pix_fmt = 128, 96, 'yuv420p'
        for color in ['#82b4a0', '#428060']:
            for packet in stream.encode(av.VideoFrame.from_image(Image.new('RGB', (128, 96), color))):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    video_bytes = video_path.read_bytes()
    image = io.BytesIO()
    Image.new('RGB', (128, 96), '#82b4a0').save(image, format='PNG')
    posts = []
    records = [
        {'id': 'image-example', 'title': '森林里的小屋', 'kind': 'image', 'is_edit': False},
        {'id': 'video-example', 'title': '流动的色彩', 'kind': 'video', 'is_edit': False},
    ]
    server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1050})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def respond(route):
                url = urlparse(route.request.url)
                query = parse_qs(url.query)
                run = query.get('run', [''])[0]
                video = run == 'video-example'
                editing = run == 'edit-example'
                running = run == 'busy-example'
                if url.path == '/api/runs' and route.request.method == 'POST':
                    body = route.request.post_data_json
                    posts.append(body)
                    if body['prompt'] == '失败请求':
                        route.fulfill(status=400, json={'error': '已有任务正在运行'})
                        return
                    records.insert(0, {'id': 'edit-example', 'title': body['prompt'], 'kind': 'image', 'is_edit': True})
                    route.fulfill(json={'run_id': 'edit-example' if body.get('edit_from') else 'busy-example'})
                elif url.path == '/api/runs/delete':
                    body = route.request.post_data_json
                    assert body['confirmed'] is True
                    records[:] = [r for r in records if r['id'] != body['run']]
                    route.fulfill(json={'deleted': body['run']})
                elif url.path == '/api/runs':
                    route.fulfill(json={'records': records})
                elif url.path == '/api/state':
                    route.fulfill(json={'revision': None if running else 2, 'prompt': '森林里的小屋', 'exit_code': None if running else 0,
                                        'status': {'status': 'completed'}, 'usage': {'model_calls': 3},
                                        'spec': {'format': 'mp4' if video else 'png', 'width': 128, 'height': 96, 'duration': 3}})
                elif url.path == '/api/process':
                    route.fulfill(json={'steps': [{'id': '1', 'title': '规划作品', 'status': 'done', 'summary': '先建立构图，再渲染并观察结果。'}]})
                elif url.path == '/api/step':
                    route.fulfill(json={'summary': '先建立构图，再渲染并观察结果。', 'events': [{'tool': 'plan_creation'}], 'media': []})
                elif url.path == '/api/version':
                    route.fulfill(json={'artwork': {'revision': 2}, 'edit': {'source_run': 'image-example', 'instruction': posts[-1]['prompt']} if editing else None,
                                        'media': [{'path': 'result.mp4' if video else 'result.png', 'kind': 'export'}]})
                else:
                    route.fulfill(status=404, json={'error': 'unexpected fixture route'})

            page.route('**/api/**', respond)
            page.route('**/media?**', lambda route: route.fulfill(body=video_bytes if 'result.mp4' in route.request.url else image.getvalue(), content_type='video/mp4' if 'result.mp4' in route.request.url else 'image/png'))
            page.goto(base, wait_until='networkidle')
            expect(page.get_by_role('heading', name='让想象，成为画面。')).to_be_visible()
            assert page.locator('.welcome-logo img').evaluate('(img)=>img.complete && img.naturalWidth>0')
            page.locator('.history-row > button:first-child').filter(has_text='森林里的小屋').click()
            expect(page.locator('.artifact-grid img')).to_be_visible()
            page.locator('.process-panel > summary').click()
            page.locator('.process-step > summary').click()
            expect(page.get_by_text('先建立构图，再渲染并观察结果。')).to_be_visible()
            page.get_by_role('textbox', name='创作要求').fill('把背景改成蓝色')
            page.get_by_role('button', name='发送创作要求').click()
            expect(page.locator('.chat-turn')).to_have_count(2)
            assert posts[-1]['edit_from'] == 'image-example' and posts[-1]['revision'] == 2
            page.get_by_role('textbox', name='创作要求').fill('失败请求')
            page.get_by_role('button', name='发送创作要求').click()
            expect(page.get_by_role('alert')).to_contain_text('已有任务正在运行')
            expect(page.get_by_role('textbox', name='创作要求')).to_have_value('失败请求')
            page.locator('.history-row > button:first-child').filter(has_text='流动的色彩').click()
            expect(page.locator('video')).to_be_visible()
            assert 'result.mp4' in page.locator('video').get_attribute('src')
            page.wait_for_function('document.querySelector("video").videoWidth === 128')
            page.once('dialog', lambda dialog: dialog.dismiss())
            page.get_by_role('button', name='删除创作：流动的色彩', exact=True).click()
            expect(page.locator('video')).to_be_visible()
            assert any(r['id'] == 'video-example' for r in records)
            page.once('dialog', lambda dialog: dialog.accept())
            page.get_by_role('button', name='删除创作：流动的色彩', exact=True).click()
            expect(page.get_by_role('button', name='删除创作：流动的色彩', exact=True)).to_have_count(0)
            expect(page.get_by_role('heading', name='让想象，成为画面。')).to_be_visible()
            page.get_by_role('button', name='新建创作', exact=False).click()
            page.locator('input[type=file]').set_input_files({'name': 'reference.png', 'mimeType': 'image/png', 'buffer': image.getvalue()})
            expect(page.locator('.attachment')).to_contain_text('reference.png')
            page.get_by_role('button', name='移除参考图').click()
            expect(page.locator('.attachment')).to_have_count(0)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.wait_for_function('document.querySelector(".sidebar").getBoundingClientRect().right <= 1')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.get_by_role('button', name='打开会话列表').click()
            expect(page.locator('.sidebar')).to_have_class('sidebar open')
            page.get_by_role('button', name='关闭会话列表').click()
            page.locator('input[type=file]').set_input_files({'name': 'reference.png', 'mimeType': 'image/png', 'buffer': image.getvalue()})
            page.get_by_role('textbox', name='创作要求').fill('制作一段动画')
            page.get_by_role('button', name='发送创作要求').click()
            expect(page.get_by_role('button', name='发送创作要求')).to_be_disabled()
            expect(page.get_by_text('正在把你的想法变成画面')).to_be_visible()
            assert posts[-1]['kind'] == 'video' and posts[-1]['duration'] == 3
            assert posts[-1]['image'] and 'edit_from' not in posts[-1]
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
