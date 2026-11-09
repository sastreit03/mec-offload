import http.client
import time
import threading

from mec_ue.preview import Preview


def wait_for(condition):
    for _ in range(200):
        if condition():
            return
        time.sleep(.01)
    raise AssertionError('condition timed out')


def test_http_preview_only_encodes_while_watched():
    calls = []
    p = Preview('linux1', port=0, encode=lambda image: calls.append(image) or b'fake-jpeg')
    p.start()
    port = p.server.server_address[1]
    try:
        p.begin('video', 'clip.mov', {'fps': 30})
        p.offer('video', 0, 'unwatched', {})
        assert not calls and p.pending is None
        page = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        page.request('GET', '/')
        assert b'Linux UE outgoing video' in page.getresponse().read()
        page.close()
        stream = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        stream.request('GET', '/preview.mjpg')
        response = stream.getresponse()
        wait_for(lambda: p.viewers == 1)
        p.offer('video', 1, 'watched', {})
        assert response.readline() == b'--frame\r\n'
        assert calls == ['watched']
        response.close()
        stream.close()
        wait_for(lambda: p.viewers == 0)
        p.offer('video', 2, 'unwatched again', {})
        time.sleep(.25)
        assert calls == ['watched'] and p.jpeg is None
    finally:
        p.close()
    assert not p.worker.is_alive() and not p.http.is_alive()


def test_latest_only_and_stale_encoding_discarded():
    entered, release = threading.Event(), threading.Event()
    def encode(image):
        entered.set()
        release.wait(2)
        return b'old-video'
    p = Preview('linux1', port=0, encode=encode)
    p.start()
    try:
        p.begin('old', 'old.mov', {})
        p.subscribe()
        p.offer('old', 1, object(), {})
        assert entered.wait(1)
        for frame in range(2, 100):
            p.offer('old', frame, object(), {})
        assert p.pending[1] == 99
        p.begin('new', 'new.mov', {})
        release.set()
        time.sleep(.25)
        assert p.jpeg is None and p.pending is None
        p.end('old')
        assert p.video_id == 'new'
        p.end('new')
        assert p.video_id is None
        p.unsubscribe()
    finally:
        release.set()
        p.close()
