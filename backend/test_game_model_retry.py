import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import aiohttp
from game_director import GameDirector, GameStore
from game_model_retry import request_model, ModelUnavailable
from test_game_director import Model, Adapter


def unavailable(*args):
    raise aiohttp.ClientConnectionError('connection failed')


class ModelRetryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = GameStore(Path(temp.name)/'db')
        self.model, self.adapter = Model(), Adapter()
        self.director = GameDirector(self.store, self.model, self.adapter)
        self.director.set_enabled(True)

    def test_plan_retry_backoff_then_fresh_observation(self):
        original = self.model.plan
        self.model.plan = unavailable
        with patch('game_director.time.monotonic', return_value=100):
            self.director.tick()
            self.assertEqual(self.director.read_retry_at, 105)
            reads = self.adapter.reads
            self.director.tick()
            self.assertEqual(self.adapter.reads, reads)
        with patch('game_director.time.monotonic', return_value=105):
            self.director.tick()
            self.assertEqual(self.director.read_retry_at, 115)
        self.assertTrue(self.director.enabled)
        self.assertFalse(self.adapter.writes)
        self.model.plan = original
        with patch('game_director.time.monotonic', return_value=115): self.director.tick()
        self.assertEqual(len(self.adapter.writes), 1)
        self.assertEqual(self.director.model_failures, 0)

    def test_classification_retry_keeps_request_and_expiry(self):
        self.director.submit('viewer', '造电厂')
        received = self.director.pending[0]['received']
        self.model.classify = unavailable
        self.director.tick()
        self.assertEqual(len(self.director.pending), 1)
        self.assertEqual(self.director.pending[0]['received'], received)
        self.assertFalse(self.adapter.writes)
        self.director.pending[0]['received'] -= 100
        self.director.read_retry_at = 0
        self.director.tick()
        self.assertFalse(self.director.pending)
        self.assertFalse(self.adapter.writes)

    def test_pause_during_model_request_is_not_undone(self):
        def stop(*args):
            self.director.set_enabled(False)
            unavailable()
        self.model.plan = stop
        self.director.tick()
        self.assertFalse(self.director.enabled)
        self.assertEqual(self.director.model_failures, 0)

    def test_game_write_timeout_still_pauses(self):
        self.adapter.execute = lambda *args: (_ for _ in ()).throw(TimeoutError())
        self.director.tick()
        self.assertFalse(self.director.enabled)
        self.assertFalse(any(e['kind'] == 'model-retry' for e in self.store.events()))

    def test_auth_errors_not_retried_but_rate_limit_is(self):
        for status, expected in ((401, aiohttp.ClientResponseError), (429, ModelUnavailable)):
            def fail(): raise aiohttp.ClientResponseError(None, (), status=status)
            with self.assertRaises(expected): request_model(fail)


if __name__ == '__main__': unittest.main()
