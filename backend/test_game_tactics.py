import unittest
from game_tactics import Tactics, guard_targets


def view(x=10, relation='enemy', session='one'):
    tank=dict(id='tank',owner=0,kind='vehicle',type='MTNK',cell=dict(x=x,y=10),health=300)
    enemy=dict(id='target',owner=2,kind='building',type='ANY',relation=relation,cell=dict(x=30,y=10))
    return dict(sessionId=session,player=0,entities=[tank,enemy],owned=[tank])

class TacticTests(unittest.TestCase):
    def test_guard_is_relation_based_and_blocks_entire_batch(self):
        plan=dict(method='batch',params=dict(orders=[dict(action='attack',unit='tank',target='target')]))
        for relation in ('neutral','ally','unknown',None):
            with self.assertRaises(ValueError): guard_targets(plan,view(relation=relation))
        guard_targets(plan,view())

    def test_repeat_progress_and_session_reset(self):
        t=Tactics(); v=view(); t.update(v)
        p=dict(method='act',params=dict(action='attack',unit='tank',target='target'))
        for _ in range(2): t.check_repeat(p,v); t.record(p,v)
        with self.assertRaises(ValueError): t.check_repeat(p,v)
        t.check_repeat(p,view(x=11))
        t.update(view(session='two')); t.check_repeat(p,v)

    def test_advance_tracks_progress_and_stall_without_resending(self):
        now=[0]; t=Tactics(lambda:now[0]); v=view(); t.update(v)
        p=dict(method='advance',params=dict(units=['tank'],cell=dict(x=30,y=10)))
        executed=t.begin(p['params'],v)
        self.assertEqual(executed['params']['action'],'attack-move')
        t.accepted(p,executed,v,{'exitCode':0})
        now[0]=30; t.update(view(x=20)); self.assertIsNotNone(t.advance)
        now[0]=60; t.update(view(x=20)); self.assertIsNotNone(t.advance)
        now[0]=66; t.update(view(x=20)); self.assertIsNone(t.advance)
        self.assertIn('stalled',t.notice)

    def test_no_rejected_skill_and_no_miners(self):
        t=Tactics(); v=view(); t.update(v)
        p=dict(method='advance',params=dict(units=['tank'],cell=dict(x=30,y=10)))
        t.accepted(p,t.begin(p['params'],v),v,{'exitCode':1}); self.assertIsNone(t.advance)
        v['owned'][0]['type']='CMIN'
        with self.assertRaises(ValueError): t.begin(p['params'],v)

    def test_camera_tracks_owned_offscreen_and_honors_hold(self):
        t=Tactics(lambda:30); v=view(); t.update(v); t.main=['tank']; t.camera_follow=['tank']; v['entities']=[]
        self.assertEqual(t.camera(v)['params']['cell'],{'x':10,'y':10})
        t.camera_hold=40; self.assertIsNone(t.camera(v))

class OptionalCameraTests(unittest.TestCase):
    def test_follow_is_opt_in_and_explicit_look_stops_it(self):
        t=Tactics(lambda:30); v=view(); t.update(v); t.main=['tank']; v['entities']=[]
        self.assertIsNone(t.camera(v))
        t.configure_camera({'enabled':True,'units':['tank']},v)
        self.assertIsNotNone(t.camera(v))
        p={'method':'look','params':{'cell':{'x':5,'y':5}}}
        t.accepted(p,p,v,{'exitCode':0})
        self.assertEqual(t.camera_follow,[])
        self.assertIsNone(t.camera(v))
        t.configure_camera({'enabled':True,'units':['tank']},v)
        t.configure_camera({'enabled':False},v)
        self.assertIsNone(t.camera(v))

    def test_follow_rejects_non_owned_and_resets_on_new_match(self):
        t=Tactics(lambda:30);v=view();t.update(v)
        with self.assertRaises(ValueError):t.configure_camera({'enabled':True,'units':['enemy']},v)
        t.configure_camera({'enabled':True,'units':['tank']},v)
        v['sessionId']='new';t.update(v)
        self.assertEqual(t.camera_follow,[])

class OpeningTests(unittest.TestCase):
    def test_mcv_first_and_viewer_override(self):
        t=Tactics(); v=view(); v['owned'].append(dict(id='mcv',type='AMCV',owner=0,kind='vehicle',cell=dict(x=5,y=5)))
        t.update(v)
        self.assertEqual(t.opening(v)['method'],'look')
        v['entities'].append(v['owned'][-1])
        p=t.opening(v)
        self.assertEqual(p['params'],dict(action='deploy',unit='mcv'))
        army=dict(method='advance',params=dict(units=['tank'],cell=dict(x=40,y=40)))
        with self.assertRaises(ValueError): t.strategy_guard(army,v)
        t.strategy_guard(army,v,autonomous=False)
        v['owned'][-1]['type']='GACNST'
        self.assertIsNone(t.opening(v))
        t.strategy_guard(army,v)

    def test_bounded_deploy_and_active_advance(self):
        now=[0]; t=Tactics(lambda:now[0]); v=view(relation='neutral')
        t.update(v)
        p=dict(method='advance',params=dict(units=['tank'],cell=dict(x=10,y=10)))
        t.strategy_guard(p,v)
        p['params']['cell']=dict(x=30,y=30); t.strategy_guard(p,v)
        t.accepted(p,t.begin(p['params'],v),v,{})
        p['params']['cell']=dict(x=40,y=40)
        with self.assertRaises(ValueError): t.strategy_guard(p,v)
        v['owned'].append(dict(id='mcv',type='AMCV',owner=0,kind='vehicle',cell=dict(x=5,y=5)))
        v['entities'].append(v['owned'][-1]); t.update(v)
        p=t.opening(v); t.accepted(p,p,v,{})
        self.assertEqual(t.opening(v)['method'],'observe')
        now[0]=21; p=t.opening(v); t.accepted(p,p,v,{})
        now[0]=42; self.assertIsNone(t.opening(v))
        self.assertIn('two deploy attempts',t.notice)

class BaseAlertTests(unittest.TestCase):
    def test_damage_informs_without_overriding_camera_or_advance(self):
        now=[30]; t=Tactics(lambda:now[0]); v=view()
        base=dict(id='base',type='GACNST',owner=0,kind='building',cell=dict(x=5,y=5),health=1000)
        v['owned'].append(base); t.update(v)
        self.assertIsNone(t.base_alert)
        t.main=['tank']; t.camera_follow=['tank']; t.camera_hold=999; t.camera_at=30
        t.advance=dict(units=['tank'],goal=dict(x=40,y=40),last=dict(x=10,y=10),progressAt=30)
        base['health']=800; t.update(v)
        self.assertIsNotNone(t.advance)
        self.assertIsNone(t.camera(v))
        self.assertEqual(t.base_alert['hits'][0]['previousHealth'],1000)
        self.assertNotIn('base',[e['id'] for e in v['entities']])
        t.camera_at=30; t.alert_camera_serial=t.alert_serial
        self.assertIsNone(t.camera(v))
        now[0]=56; v['entities']=[]; t.update(v); self.assertIsNotNone(t.base_alert)
        v['entities']=[base]; t.update(v); self.assertIsNone(t.base_alert)
        self.assertEqual(t.defense_recovery['phase'],'locally-stable')
        base['health']=700; t.update(v); self.assertIsNone(t.defense_recovery)
        self.assertIsNotNone(t.base_alert)

    def test_defense_can_revisit_but_expedition_is_blocked(self):
        t=Tactics(); v=view(relation='neutral'); b=dict(id='b',type='GAPOWR',owner=0,kind='building',cell=dict(x=10,y=10),health=750)
        v['owned'].append(b); t.update(v); b['health']=600; t.update(v)
        defend=dict(method='advance',params=dict(units=['tank'],cell=dict(x=10,y=10)))
        t.strategy_guard(defend,v)
        defend['params']['cell']=dict(x=100,y=100)
        with self.assertRaises(ValueError): t.strategy_guard(defend,v)
        t.strategy_guard(defend,v,autonomous=False)

    def test_new_session_and_healing_do_not_signal_damage(self):
        t=Tactics(); v=view(); b=dict(id='b',type='GAPOWR',owner=0,kind='building',cell=dict(x=10,y=10),health=500)
        v['owned'].append(b); t.update(v); b['health']=600; t.update(v); self.assertIsNone(t.base_alert)
        b['health']=400; v['sessionId']='new'; t.update(v); self.assertIsNone(t.base_alert)

    def test_decision_gate_wakes_on_offscreen_owned_damage(self):
        from game_context import DecisionGate
        import copy
        gate=DecisionGate(); v=view(); v['owned'].append(dict(id='b',owner=0,kind='building',health=1000))
        gate.mark(copy.deepcopy(v)); v['owned'][-1]['health']=950
        self.assertEqual(gate.reason(v),'damage')

if __name__=='__main__': unittest.main()

class TargetRaceTests(unittest.TestCase):
    def test_target_disappears_before_submission_is_safe_replan(self):
        from game_adapter import Ra2Adapter
        from game_director import UnsubmittedPlanError
        adapter=object.__new__(Ra2Adapter)
        adapter.observe=lambda: dict(sessionId='s',entities=[])
        adapter.call=lambda *args: self.fail('Must not submit a stale attack')
        with self.assertRaises(UnsubmittedPlanError):
            adapter.execute('act',dict(action='attack',unit='tank',target='gone'),'s')

class RouteTests(unittest.TestCase):
    def test_visited_sector_allows_rally_reinforcement_and_old_route(self):
        t=Tactics(); v=view(relation='neutral'); t.update(v)
        p=dict(method='batch',params=dict(orders=[dict(action='produce',type='MTNK'),dict(action='attack-move',unit='tank',cell=dict(x=10,y=10))]))
        t.strategy_guard(p,v)

    def test_alternating_targets_only_block_if_same_unit_did_not_move(self):
        now=[0]; t=Tactics(lambda:now[0]); v=view(relation='neutral'); t.update(v)
        def plan(x): return dict(method='act',params=dict(action='attack-move',unit='tank',cell=dict(x=x,y=10)))
        for x in (30,50,30):
            t.strategy_guard(plan(x),v); t.accepted(plan(x),plan(x),v,{}); now[0]+=2
        with self.assertRaises(ValueError): t.strategy_guard(plan(50),v)
        t.strategy_guard(plan(50),view(x=14,relation='neutral'))
        now[0]=100; t.strategy_guard(plan(50),v)
        now[0]=10; t.strategy_guard(plan(50),v,autonomous=False)

    def test_rejected_move_is_not_route_history_and_batch_preserves_squad(self):
        import copy
        t=Tactics(); v=view(relation='neutral'); second=copy.deepcopy(v['owned'][0]); second['id']='two'; v['owned'].append(second)
        t.update(v)
        p=dict(method='batch',params=dict(orders=[dict(action='move',unit=i,cell=dict(x=30,y=10)) for i in ('tank','two')]))
        t.accepted(p,p,v,{'exitCode':1}); self.assertEqual(t.routes,{})
        t.accepted(p,p,v,{}); self.assertEqual(t.main,['tank','two'])
