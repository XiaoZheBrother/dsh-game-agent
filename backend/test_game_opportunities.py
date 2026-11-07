import tempfile
import unittest
from pathlib import Path
from game_director import GameStore, validate_plan
from game_opportunities import Opportunities
from game_tactics import guard_targets


def battlefield():
    units = [dict(id='tank',owner=0,kind='vehicle',type='MTNK',health=300,cell={'x':10,'y':10})]
    oil = dict(id='oil',owner=2,relation='neutral',kind='building',type='CAOILD',cell={'x':15,'y':10})
    return dict(sessionId='s',player=0,owned=units,entities=units+[oil],cash=1500,production=[],
        sidebar=[{'type':'ENGINEER'}],strategy={'phase':'scout'},
        exploration={'source':'native-local-shroud','frontiers':[{'x':25,'y':10}]})


class OpportunityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=GameStore(Path(self.tmp.name)/'state.db'); self.now=1000
        self.o=Opportunities(self.store,lambda:self.now)
    def tearDown(self): self.tmp.cleanup()
    def engineer(self,v):
        e=dict(id='engineer',owner=0,kind='infantry',type='ENGINEER',health=100,cell={'x':11,'y':10})
        v['owned'].append(e); v['entities'].append(e)
    def test_train_once_and_restart_keeps_cooldown(self):
        v=battlefield(); self.o.update(v); p=self.o.plan(v)
        self.assertEqual(p['params']['type'],'ENGINEER')
        self.o.accepted(p,v,{'exitCode':0})
        other=Opportunities(self.store,lambda:self.now); other.update(v)
        self.assertNotEqual(other.plan(v).get('params',{}).get('action'),'produce')
    def test_pending_production_does_not_buy_another(self):
        v=battlefield(); v['production']=[{'type':'ENGINEER'}]; self.o.update(v)
        self.assertNotEqual(self.o.plan(v)['params']['action'],'produce')
    def test_capture_needs_actual_owned_confirmation(self):
        v=battlefield(); self.engineer(v); self.o.update(v); p=self.o.plan(v)
        validate_plan(p); guard_targets(p,v)
        self.assertEqual(p['params']['action'],'capture')
        self.o.accepted(p,v,{'exitCode':0}); self.o.update(v)
        self.assertEqual(self.o.state['oils']['oil']['status'],'pending')
        v['owned'].append({**v['entities'][1],'owner':0,'relation':'self'})
        self.o.update(v); self.assertEqual(self.o.state['oils']['oil']['status'],'captured')
    def test_capture_rejects_tank_enemy_and_group(self):
        v=battlefield(); self.engineer(v)
        p=dict(method='act',params=dict(action='capture',unit='tank',target='oil'))
        with self.assertRaises(ValueError): guard_targets(p,v)
        p['params']['unit']='engineer'; v['entities'][1]['relation']='enemy'
        with self.assertRaises(ValueError): guard_targets(p,v)
        v['entities'][1]['relation']='neutral'; p['params']['units']=['engineer']
        with self.assertRaises(ValueError): guard_targets(p,v)
    def test_scout_uses_frontier_and_stall_changes_region(self):
        v=battlefield(); v['entities']=v['owned'][:]; self.o.update(v)
        p=self.o.plan(v); self.assertEqual(p['params']['cell'],{'x':25,'y':10})
        self.o.accepted(p,v,{'exitCode':0},local=True)
        self.o.update(v); self.assertIsNone(self.o.plan(v))
        self.now+=36; self.o.update(v); self.assertIsNone(self.o.plan(v))
        v['exploration']['frontiers'].append({'x':10,'y':30})
        self.assertEqual(self.o.plan(v)['params']['cell'],{'x':10,'y':30})
    def test_no_fake_frontier_and_defense_priority(self):
        v=battlefield(); v['entities']=v['owned'][:]; v.pop('exploration'); self.o.update(v)
        self.assertIsNone(self.o.plan(v))
        v=battlefield(); self.o.update(v); v['tactics']={'baseAlert':{'cell':{'x':10,'y':10}}}
        self.assertIsNone(self.o.plan(v))
    def test_fog_disappearance_is_not_capture_and_new_match_resets(self):
        v=battlefield(); self.engineer(v); self.o.update(v)
        self.o.accepted(self.o.plan(v),v,{'exitCode':0})
        v['entities']=v['owned'][:]; self.now+=91; self.o.update(v)
        self.assertEqual(self.o.state['oils']['oil']['status'],'unconfirmed')
        v['sessionId']='next'; self.o.update(v); self.assertEqual(self.o.state['oils'],{})

if __name__=='__main__': unittest.main()
