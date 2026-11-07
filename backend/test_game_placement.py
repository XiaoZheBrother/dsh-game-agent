import copy
import tempfile
import unittest
from pathlib import Path
from game_director import GameStore, GameDirector
from game_placement import Placement, overlaps
from test_game_director import Model, Adapter


def view():
    return {'sessionId': 's1', 'player': 0, 'frame': 100,
            'sidebar': [{'type': 'FACTORY', 'foundation': {'width': 5, 'height': 3}}],
            'production': [{'type': 'FACTORY', 'progress': 54, 'total': 54}],
            'entities': [{'id': 'base', 'owner': 0, 'kind': 'building', 'cell': {'x': 30, 'y': 30},
                          'foundation': {'width': 4, 'height': 4}}]}


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = GameStore(Path(self.tmp.name)/'db')
        self.search = Placement(self.store)

    def test_full_footprint_avoids_own_and_neutral_obstacles(self):
        state = view()
        state['entities'].append({'id': 'neutral', 'owner': 2, 'cell': {'x': 25, 'y': 30},
                                  'foundation': {'width': 4, 'height': 4}})
        cells = self.search.candidates(state, 'FACTORY')
        self.assertEqual(len(cells), 6)
        for c in cells:
            rect = (c['x'], c['y'], c['x']+5, c['y']+3)
            self.assertFalse(overlaps(rect, (30, 30, 34, 34)))
            self.assertFalse(overlaps(rect, (25, 30, 29, 34)))

    def test_rejections_survive_new_planner_but_not_other_match(self):
        state = view()
        first = self.search.candidates(state, 'FACTORY')
        self.store.record('step', {'before': state, 'receipt': {'events': [
            {'event': 'placement-rejected', 'type': 'FACTORY', 'cell': c} for c in first]}})
        second = Placement(self.store).candidates(state, 'FACTORY')
        self.assertTrue(second)
        self.assertFalse(any(c in first for c in second))
        state['sessionId'] = 'new'
        self.assertEqual(Placement(self.store).candidates(state, 'FACTORY'), first)

    def test_no_enemy_anchors_or_unknown_dimensions(self):
        state = view()
        state['entities'][0]['owner'] = 1
        self.assertEqual(self.search.candidates(state, 'FACTORY'), [])
        self.assertEqual(self.search.candidates(view(), 'UNKNOWN'), [])

    def test_finished_building_does_not_override_model(self):
        adapter = Adapter()
        adapter.observe = lambda: copy.deepcopy(view())
        model = Model()
        model.plan = lambda *args: {'method':'observe','params':{}}
        director = GameDirector(self.store, model, adapter)
        director.set_enabled(True); director.tick()
        self.assertEqual([w[0] for w in adapter.writes], ['observe'])

    def test_model_cells_are_preserved(self):
        adapter = Adapter(); adapter.observe = lambda: copy.deepcopy(view())
        model = Model(); cells = [{'x':45,'y':36}]
        model.plan = lambda *args: {'method':'place-building','params':{'type':'FACTORY','cells':cells}}
        director = GameDirector(self.store, model, adapter)
        director.set_enabled(True); director.tick()
        self.assertEqual(adapter.writes[0][1]['cells'],cells)
        from game_learning import event_summary
        step = [e for e in self.store.events() if e['kind']=='step'][-1]
        self.assertIn('params.cells',event_summary(step)['modelControlledFields'])

    def test_follow_tool_can_select_owned_offscreen_without_game_write(self):
        adapter = Adapter()
        state = view(); state['owned'] = state['entities']; state['entities'] = []
        adapter.observe = lambda: copy.deepcopy(state)
        model = Model()
        model.plan = lambda *args: {'method':'camera-follow','params':{'enabled':True,'units':['base']}}
        director = GameDirector(self.store, model, adapter)
        director.set_enabled(True); director.tick()
        self.assertEqual(adapter.writes,[])
        self.assertEqual(director.tactics.camera_follow,['base'])
        step = [e for e in self.store.events() if e['kind']=='step'][-1]
        self.assertEqual(step['data']['receipt']['exitCode'],0)

    def test_optional_check_never_submits_or_moves_cells(self):
        adapter = Adapter(); adapter.observe = lambda: copy.deepcopy(view())
        model = Model(); cells = [{'x':30,'y':30},{'x':45,'y':36}]
        model.plan = lambda *args: {'method':'check-placement','params':{'type':'FACTORY','cells':cells}}
        director = GameDirector(self.store, model, adapter)
        director.set_enabled(True); director.tick()
        self.assertEqual(adapter.writes,[])
        step = [e for e in self.store.events() if e['kind']=='step'][-1]
        events = step['data']['receipt']['events']
        self.assertEqual([e['cell'] for e in events],cells)
        self.assertEqual([e['status'] for e in events],['blocked','unknown'])


if __name__ == '__main__': unittest.main()
