"""Bounded, read-only preview transport over the existing loopback control API."""
import http.client
import json
import threading
import time
import uuid


class GameFrames:
    def __init__(self, channel, port=15180):
        self.channel = channel
        self.connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        self.lock = threading.Lock()
        self.image = None
        self.captured = 0
        self.retry_after = 0

    def request(self, method, params=None):
        # This transport deliberately exposes only presentation reads, never commands.
        if method not in ('ui.status', 'ui.screenshot'):
            raise ValueError('Read-only frame transport')
        body = json.dumps({'id': uuid.uuid4().hex, 'method': method, 'params': params})
        self.connection.request('POST', f'/channels/{self.channel}/request', body,
                                {'Content-Type': 'application/json'})
        response = self.connection.getresponse()
        payload = json.loads(response.read())
        if response.status != 200 or 'error' in payload:
            raise RuntimeError('Game frame unavailable')
        return payload['result']

    def frame(self):
        # All viewers share one capture; no process spawn, background producer or backlog.
        with self.lock:
            now = time.monotonic()
            if now < self.retry_after:
                raise RuntimeError('Frame transport reconnecting')
            if self.image and now - self.captured < 0.1:
                return self.image
            try:
                status = self.request('ui.status')
                result = self.request('ui.screenshot', {'expectedPageId': status['pageId']})
                if result.get('pageId') != status['pageId']:
                    raise RuntimeError('Game page changed')
                self.image = result['image']
                self.captured = time.monotonic()
                return self.image
            except Exception:
                self.image = None
                self.connection.close()
                self.retry_after = time.monotonic() + 1
                raise
