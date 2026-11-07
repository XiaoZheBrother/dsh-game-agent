import tempfile
import unittest
from pathlib import Path
from game_director import GameStore
from game_strategy import Strategy


def battlefield(session='s',enemy=True,scattered=False):
    own=[dict(id=t,owner=0,kind='building' if t!='CMIN' else 'vehicle',type=t,health=1000,cell=dict(x=10,y=10)) for t in ('GACNST','GAWEAP','CMIN')]
    own += [dict(id=str(i),owner=0,kind='vehicle',type='MTNK',health=300,cell=dict(x=10+i*(20 if scattered else 1),y=12)) for i in range(8)]
    return dict(sessionId=session,player=0,owned=own,entities=own+([dict(id='enemy',relation='enemy',owner=1,kind='building',type='NACNST',cell=dict(x=70,y=70))] if enemy else []))

class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=GameStore(Path(self.tmp.name)/'state.db')
    def tearDown(self): self.tmp.cleanup()
    def test_attack_requires_coherent_force_not_total_count(self):
        s=Strategy(self.store); result=s.update(battlefield(scattered=True),{})
        self.assertEqual(result['phase'],'rally')
        result=s.update(battlefield(),{}); self.assertEqual(result['phase'],'attack')
        self.assertEqual(len(result['force']['squad']),8)
    def test_defense_preserves_objective_and_restart_recovers_it(self):
        s=Strategy(self.store); s.update(battlefield(),{})
        state=s.update(battlefield(enemy=False),dict(baseAlert={'cell':{'x':10,'y':10}}))
        self.assertEqual(state['phase'],'defend'); self.assertEqual(state['objective']['id'],'enemy')
        restarted=Strategy(self.store); state=restarted.update(battlefield(enemy=False),{})
        self.assertEqual(state['phase'],'attack'); self.assertEqual(state['objective']['id'],'enemy')
        self.assertEqual(restarted.update(battlefield(session='different',enemy=False),{})['phase'],'scout')
    def test_revisit_absence_is_not_kill_or_victory(self):
        now=[0]; s=Strategy(self.store,lambda:now[0]); s.update(battlefield(),{})
        now[0]=30; v=battlefield(enemy=False); v['entities'][0]['cell']=dict(x=70,y=70)
        state=s.update(v,{})
        self.assertIsNone(state['objective']); self.assertEqual(state['phase'],'scout')
        self.assertEqual(state['enemies'][0]['status'],'not-visible-on-revisit')
        self.assertEqual(state['victory']['shortGame'],'unknown')
    def test_bootstrap_only_confirmed_enemy_sightings_this_session(self):
        v=battlefield(); v['entities'][-1]['relation']='neutral'
        self.store.record('step',dict(before=v,after=v))
        self.store.record('step',dict(before=battlefield(session='other'),after=battlefield(session='other')))
        s=Strategy(self.store); self.assertIsNone(s.update(battlefield(enemy=False),{})['objective'])
    def test_old_match_sighting_restored_without_hidden_updates(self):
        self.store.record('step',dict(before=battlefield(),after=battlefield()))
        s=Strategy(self.store); result=s.update(battlefield(enemy=False),{})
        self.assertEqual(result['objective']['cell'],dict(x=70,y=70))
        before=result['objective']['lastSeenAt']; result=s.update(battlefield(enemy=False),{})
        self.assertEqual(result['objective']['lastSeenAt'],before)

if __name__=='__main__': unittest.main()
