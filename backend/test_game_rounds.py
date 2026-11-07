import tempfile
import unittest
from pathlib import Path
from game_director import GameDirector, GameStore
from game_rounds import GameRounds
from test_game_director import Model, Adapter


class NativeAdapter(Adapter):
    def __init__(self):
        super().__init__(); self.starts=[]
        self.status={'pageId':'p1','features':['replay-native-settings-v1'],
                     'runtime':{'battlefield':{'ready':True,'sessionId':'s1'}}}
    def native_status(self): return self.status
    def next_battle(self,*args): self.starts.append(args)
    def end(self,outcome='defeat'):
        self.status['runtime']={'battlefield':{'ready':False},'battleResult':
            {'sessionId':'s1','outcome':outcome,'source':'native-game-loop'}}


class RoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.now=100
        self.store=GameStore(Path(self.tmp.name)/'game.sqlite3'); self.adapter=NativeAdapter()
        self.director=GameDirector(self.store,Model(),self.adapter)
        self.rounds=GameRounds(self.store,self.adapter,lambda:self.now); self.director.rounds=self.rounds
        self.director.set_enabled(True); self.rounds.configure(True)
    def tearDown(self): self.tmp.cleanup()
    def test_active_or_unknown_is_not_restart_permission(self):
        self.assertFalse(self.rounds.poll(self.director))
        self.adapter.end('unknown'); self.assertFalse(self.rounds.poll(self.director))
        self.assertEqual(self.adapter.starts,[])
    def test_native_end_records_once_then_starts_once_after_delay(self):
        self.adapter.end(); self.assertTrue(self.rounds.poll(self.director))
        self.rounds.poll(self.director); self.assertEqual(self.adapter.starts,[])
        self.now+=16; self.rounds.poll(self.director); self.rounds.poll(self.director)
        self.assertEqual(len(self.adapter.starts),1)
        self.assertEqual(len([e for e in self.store.events() if e['kind']=='native-game-end']),1)
        self.adapter.status['runtime']={'battlefield':{'ready':True,'sessionId':'s2'}}
        self.assertFalse(self.rounds.poll(self.director)); self.assertEqual(self.director.session,'s2')
    def test_restart_claim_survives_controller_recreation(self):
        self.adapter.end(); self.rounds.poll(self.director); self.now+=16; self.rounds.poll(self.director)
        again=GameRounds(self.store,self.adapter,lambda:self.now); again.poll(self.director)
        self.assertEqual(len(self.adapter.starts),1)
    def test_pause_or_switch_off_prevents_start(self):
        self.adapter.end(); self.rounds.poll(self.director); self.now+=16
        self.director.set_enabled(False); self.director.tick(); self.assertEqual(self.adapter.starts,[])
        self.rounds.configure(False); self.rounds.poll(self.director); self.assertEqual(self.adapter.starts,[])
    def test_uncertain_start_not_repeated(self):
        def uncertain(*args): self.adapter.starts.append(args); raise TimeoutError()
        self.adapter.next_battle=uncertain; self.adapter.end(); self.rounds.poll(self.director)
        self.now+=16; self.rounds.poll(self.director); self.rounds.poll(self.director)
        self.assertEqual(len(self.adapter.starts),1)
        self.now+=181
        with self.assertRaisesRegex(RuntimeError,'180 秒'): self.rounds.poll(self.director)
    def test_failure_retains_diagnostics_and_manual_loading_can_recover(self):
        self.adapter.end();self.rounds.poll(self.director);self.now+=16;self.rounds.poll(self.director)
        self.adapter.status['failure']='Saved game settings unavailable'
        with self.assertRaisesRegex(RuntimeError,'Saved game settings unavailable'):self.rounds.poll(self.director)
        event=[e for e in self.store.events() if e['kind']=='next-round-failed'][-1]
        self.assertEqual(event['data']['failure'],'Saved game settings unavailable')
        self.adapter.status['failure']=None
        self.adapter.status['runtime']={'phase':'loading','battlefield':{'ready':False}}
        self.now+=300
        self.assertTrue(self.rounds.poll(self.director))
        self.assertEqual(len(self.adapter.starts),1)
        self.adapter.status['runtime']={'battlefield':{'ready':True,'sessionId':'manual-new'}}
        self.assertFalse(self.rounds.poll(self.director));self.assertEqual(self.director.session,'manual-new')
        self.assertEqual(len(self.adapter.starts),1)
    def test_new_ready_battle_wins_over_old_startup_failure(self):
        self.adapter.end();self.rounds.poll(self.director);self.now+=16;self.rounds.poll(self.director)
        self.adapter.status['failure']='old failure'
        self.adapter.status['runtime']={'battlefield':{'ready':True,'sessionId':'new'}}
        self.assertFalse(self.rounds.poll(self.director));self.assertEqual(self.director.session,'new')
    def test_old_round_pending_orders_are_not_carried_over(self):
        self.director.submit('viewer','造电厂'); self.adapter.end(); self.rounds.poll(self.director)
        self.assertEqual(self.director.pending,[])
        self.assertEqual(self.director.replies[-1]['kind'],'cancelled')

if __name__=='__main__': unittest.main()
