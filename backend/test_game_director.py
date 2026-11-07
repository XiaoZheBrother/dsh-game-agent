import tempfile
import unittest
import threading
from pathlib import Path
from game_director import GameDirector, GameStore, validate_plan


class Model:
    kind = 'command'
    on_plan = None
    def classify(self, text): return {'kind': self.kind}
    def chat(self, text, uncertain, context=None):
        self.chat_context=context
        return '请说明目标' if uncertain else '你好呀'
    def plan(self, before, intent, lessons, recent=None):
        self.recent=recent
        if self.on_plan: self.on_plan()
        return {'say':'移动','method':'act','params':{'action':'move','unit':'u1','cell':{'x':2,'y':3}}}
    def report(self, *args): return '已提交，正在核对结果'
    def review(self, steps): return [{'text':'先检查可用生产项','evidence':[steps[-1]['id']]}]


class Adapter:
    def __init__(self): self.writes=[]; self.session='s1'; self.reads=0; self.uncertain=False
    def observe(self):
        self.reads+=1
        return {'sessionId':self.session, 'frame':self.reads, 'entities':[]}
    def execute(self, method, params, session):
        self.writes.append((method,params,session))
        return {'uncertain':self.uncertain}


class Tests(unittest.TestCase):
    def test_review_does_not_mix_rounds_or_recall_superseded_lessons(self):
        old = self.store.record('step', {'before': {'sessionId': 'old'}})
        self.director.session = 'new'
        with self.assertRaises(ValueError): self.director.review()
        current = self.store.record('step', {'before': {'sessionId': 'new'}})
        ids = self.director.review()
        self.assertEqual(self.store.recall()[0]['evidence'], [current])
        with self.store.connect() as db:
            db.execute("UPDATE lessons SET status='superseded' WHERE id=?", (ids[0],))
        self.assertEqual(self.store.recall(), [])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=GameStore(Path(self.tmp.name)/'game.sqlite3')
        self.model=Model(); self.adapter=Adapter()
        self.director=GameDirector(self.store,self.model,self.adapter)
    def tearDown(self): self.tmp.cleanup()
    def test_unavailable_build_replans_without_writes_or_pause(self):
        self.model.plan=lambda *args: {'method':'build','params':{'type':'MISSING','cells':[{'x':1,'y':1}]}}
        self.director.set_enabled(True); self.director.tick()
        self.assertTrue(self.director.enabled)
        self.assertEqual(self.adapter.writes,[])
        self.assertGreater(self.director.read_retry_at,0)

    def test_model_owns_opening_and_opportunities(self):
        original = self.adapter.observe
        def opening_view():
            v = original()
            v.update(player=0, owned=[dict(id='mcv',type='AMCV',kind='vehicle',owner=0,cell={'x':1,'y':1})])
            return v
        self.adapter.observe = opening_view
        self.director.tactics.opening = lambda view: self.fail('Opening must not auto-execute')
        self.director.opportunities.plan = lambda view: self.fail('Opportunity must not auto-execute')
        seen=[]
        def decide(before,*args):
            seen.append(before)
            return {'method':'observe','params':{},'say':'先观察'}
        self.model.plan=decide
        self.director.set_enabled(True);self.director.tick()
        self.assertEqual(len(seen),1)
        self.assertEqual(seen[0]['decisionPolicy'],'model-led-v2')
        self.assertNotIn('nextAction',seen[0]['strategy'])
        self.assertNotIn('phase',seen[0]['strategy'])
        self.assertEqual([w[0] for w in self.adapter.writes],['observe'])
        step=[e for e in self.store.events() if e['kind']=='step'][-1]
        self.assertFalse(step['data']['localSkill'])

    def test_model_can_move_army_before_deploying_mcv(self):
        view={'owned':[dict(id='m',type='AMCV',owner=0,cell={'x':1,'y':1})], 'player':0}
        plan={'method':'act','params':{'action':'move','unit':'tank','cell':{'x':5,'y':5}}}
        self.director.tactics.strategy_guard(plan,view,model_led=True)

    def test_defaults_paused(self):
        self.director.tick(); self.assertEqual(self.adapter.reads,0)
    def test_initial_read_recovers_without_writing_while_offline(self):
        original = self.adapter.observe
        def offline(): raise RuntimeError('无法读取当前战况')
        self.adapter.observe = offline
        self.director.set_enabled(True)
        self.director.tick()
        self.assertTrue(self.director.enabled)
        self.assertEqual(self.adapter.writes, [])
        self.assertGreater(self.director.read_retry_at, 0)
        self.adapter.observe = original
        self.director.tick()
        self.assertEqual(self.adapter.writes, [])
        self.director.read_retry_at = 0
        self.director.tick()
        self.assertEqual(len(self.adapter.writes), 1)
        self.assertIsNone(self.director.last_error)
    def test_manual_pause_cancels_read_recovery(self):
        def offline(): raise RuntimeError('无法读取当前战况')
        self.adapter.observe = offline
        self.director.set_enabled(True)
        self.director.tick()
        self.director.set_enabled(False)
        self.director.tick()
        self.assertFalse(self.director.enabled)
        self.assertEqual(self.director.read_retry_at, 0)
    def test_post_action_read_failure_is_not_auto_retried(self):
        original = self.adapter.observe
        def read():
            if self.adapter.writes: raise RuntimeError('无法读取当前战况')
            return original()
        self.adapter.observe = read
        self.director.set_enabled(True)
        self.director.tick()
        self.assertFalse(self.director.enabled)
        self.director.tick()
        self.assertEqual(len(self.adapter.writes), 1)
    def test_chat_and_uncertain_never_touch_game(self):
        self.director.set_enabled(True)
        for kind in ['chat','uncertain','garbage']:
            self.model.kind=kind
            self.director.submit('a','你好'); self.director.tick()
            self.director.process_reply()
        self.assertEqual(self.adapter.reads,0); self.assertEqual(self.adapter.writes,[])
        self.assertEqual(len(self.director.replies),3)
    def test_viewer_interrupts_pending_autonomy(self):
        self.director.set_enabled(True)
        self.model.on_plan=lambda:self.director.submit('a','移动到右边','one')
        self.director.tick()
        self.assertEqual(self.adapter.writes,[])
        self.model.on_plan=None
        self.director.tick()
        self.assertEqual(len(self.adapter.writes),1)
        steps=[e for e in self.store.events() if e['kind']=='step']
        self.assertEqual(steps[0]['data']['source'],'viewer')
    def test_pause_invalidates_inflight_plan(self):
        self.director.set_enabled(True)
        self.model.on_plan=lambda:self.director.set_enabled(False)
        self.director.tick(); self.assertEqual(self.adapter.writes,[])
    def test_dedup_survives_restart(self):
        self.director.submit('a','造电厂','stable')
        store=GameStore(self.store.path)
        other=GameDirector(store,self.model,self.adapter)
        self.assertIsNone(other.submit('a','造电厂','stable'))
        self.assertEqual(other.pending,[])
    def test_ambiguous_write_pauses_without_retry(self):
        self.adapter.uncertain=True; self.director.set_enabled(True)
        self.director.tick(); self.director.tick()
        self.assertFalse(self.director.enabled); self.assertEqual(len(self.adapter.writes),1)
    def test_new_session_stops(self):
        self.director.set_enabled(True); self.director.tick()
        self.adapter.session='s2'; self.director.tick()
        self.assertFalse(self.director.enabled); self.assertEqual(len(self.adapter.writes),1)
    def test_expired_command_not_replayed(self):
        self.director.set_enabled(True); self.director.submit('a','造电厂')
        self.director.pending[0]['received']-=100
        self.director.tick(); self.assertEqual(self.adapter.writes,[])
        self.assertEqual(self.director.replies[0]['kind'],'expired')
    def test_chat_mode_cancels_pending_orders(self):
        self.director.set_enabled(True); self.director.submit('a','造电厂')
        self.director.chat_mode(); self.director.tick()
        self.assertEqual(self.director.pending,[]); self.assertEqual(self.adapter.writes,[])
    def test_learning_requires_evidence_and_stays_candidate(self):
        with self.assertRaises(ValueError): self.store.learn('可以作弊',['invented'])
        self.director.set_enabled(True); self.director.tick(); self.director.set_enabled(False)
        self.director.review()
        recalled=GameStore(self.store.path).recall()
        self.assertEqual(recalled[0]['status'],'candidate')
        self.assertTrue(recalled[0]['evidence'])
    def test_rejects_shell_and_protocol_overrides(self):
        for plan in [
            {'method':'exec','params':{'cmd':'anything'}},
            {'method':'act','params':{'action':'exit-battle'}},
            {'method':'act','params':{'action':'move','sessionId':'other'}},
            {'method':'batch','params':{'orders':[{'action':'sell'}]}},
            {'method':'look','params':{'cell':{'x':-1,'y':0}}},
            {'method':'place-building','params':{'type':'A','cells':[{'x':1,'y':2}]*7}},
        ]:
            with self.assertRaises(ValueError): validate_plan(plan)
    def test_single_writer(self):
        self.director.set_enabled(True)
        self.director.writer.acquire()
        try: self.director.tick()
        finally: self.director.writer.release()
        self.assertEqual(self.adapter.reads,0)

    def test_paused_still_chats_but_rejects_commands(self):
        self.model.kind='chat'
        self.director.submit('a','你好'); self.director.tick()
        self.model.kind='command'
        self.director.submit('a','造电厂'); self.director.tick()
        self.assertEqual(self.adapter.reads,0)
        self.assertEqual(self.director.replies[-1]['kind'],'paused')

    def test_periodic_self_review(self):
        self.model.plan = lambda *args: {'method':'observe','params':{}}
        self.director.set_enabled(True)
        for _ in range(12):
            self.director.gate.last -= 16
            self.director.tick()
        self.assertEqual(len(self.store.recall()),1)
        self.assertTrue(self.director.enabled)

    def test_chat_uses_recorded_battle_without_new_game_read(self):
        self.director.set_enabled(True); self.director.tick()
        reads=self.adapter.reads
        self.model.kind='chat'; self.director.submit('a','打得怎样'); self.director.tick()
        self.director.process_reply()
        self.assertEqual(self.adapter.reads,reads)
        self.assertEqual(self.model.chat_context['lastRecordedStep']['data']['after']['sessionId'],'s1')

    def test_next_plan_receives_last_operation_receipt(self):
        self.director.set_enabled(True); self.director.tick()
        self.director.gate.last -= 16
        self.director.tick()
        self.assertEqual(len(self.model.recent),1)
        self.assertEqual(self.model.recent[0]['receipt'],{'uncertain':False})

    def test_invalid_plan_regenerated_before_any_write(self):
        original=self.model.plan
        calls=[]
        def plan(*args):
            calls.append(args)
            return {'type':'json_object'} if len(calls)==1 else original(*args)
        self.model.plan=plan; self.director.set_enabled(True); self.director.tick()
        self.assertEqual(len(calls),2)
        self.assertEqual(len(self.adapter.writes),1)
        self.assertTrue(calls[1][3][-1]['notSubmitted'])

    def test_repeated_invalid_plan_backs_off_without_writing(self):
        self.model.plan=lambda *args:{'type':'json_object'}
        self.director.set_enabled(True); self.director.tick()
        self.assertTrue(self.director.enabled)
        self.assertGreater(self.director.read_retry_at, 0)
        self.assertEqual(self.adapter.writes,[])
        self.assertEqual(len([e for e in self.store.events() if e['kind']=='invalid-plan']),2)

    def test_slow_report_does_not_block_next_command(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set(); release.wait(5)
            return '按记录回复'
        self.model.report = slow
        self.director.set_enabled(True)
        self.director.submit('a','第一条'); self.director.tick()
        worker = threading.Thread(target=self.director.process_reply)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.director.submit('a','第二条'); self.director.tick()
            self.assertEqual(len(self.adapter.writes),2)
            self.assertTrue(self.director.enabled)
        finally:
            release.set(); worker.join(2)

    def test_reply_failure_does_not_pause_game(self):
        def fail(*args): raise RuntimeError('model unavailable')
        self.model.report = fail
        self.director.set_enabled(True)
        self.director.submit('a','移动'); self.director.tick()
        self.director.process_reply()
        self.assertTrue(self.director.enabled)
        self.assertEqual(len(self.adapter.writes),1)
        self.assertEqual(self.director.replies[-1]['kind'],'reply-error')

if __name__=='__main__': unittest.main()
