import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from game_frames import GameFrames


class Frames(unittest.TestCase):
    def test_viewers_share_capture_and_do_not_spawn_processes(self):
        frames = GameFrames('1')
        calls = []
        def request(method, params=None):
            calls.append(method)
            time.sleep(.01)
            return {'pageId': '1', 'image': {'presentation': 5, 'dataUrl': 'data:image/png;base64,eA=='}}
        frames.request = request
        with ThreadPoolExecutor(max_workers=4) as pool:
            images = list(pool.map(lambda _: frames.frame(), range(4)))
        self.assertEqual(calls, ['ui.status', 'ui.screenshot'])
        self.assertTrue(all(image['presentation'] == 5 for image in images))

    def test_failure_clears_stale_frame_and_backs_off(self):
        frames = GameFrames('1')
        frames.image = {'presentation': 1}
        frames.request = lambda *args: (_ for _ in ()).throw(RuntimeError('offline'))
        with self.assertRaises(RuntimeError): frames.frame()
        self.assertIsNone(frames.image)
        with self.assertRaisesRegex(RuntimeError, 'reconnecting'): frames.frame()

    def test_replacement_rejects_old_presentation(self):
        frames = GameFrames('1')
        frames.request = lambda method, params=None: {'pageId': '1' if method == 'ui.status' else '2', 'image': {}}
        with self.assertRaisesRegex(RuntimeError, 'changed'): frames.frame()
        self.assertIsNone(frames.image)

    def test_read_transport_cannot_submit_game_actions(self):
        with self.assertRaises(ValueError): GameFrames('1').request('command')


if __name__ == '__main__': unittest.main()
