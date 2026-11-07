"""Fog-safe tactical state. Own tracking never implies hidden enemy knowledge."""
import copy
import json
import math
import time
from game_context import rows


def orders(plan):
    if plan.get('method') == 'batch': return plan['params']['orders']
    if plan.get('method') == 'act': return [plan['params']]
    return []


def own(view):
    return {e['id']: e for e in rows(view.get('owned', view.get('entities')))
            if e.get('owner') == view.get('player')}


def center(entities):
    cells = [e['cell'] for e in entities if e.get('cell')]
    if not cells: return None
    return {k: round(sum(c[k] for c in cells) / len(cells)) for k in ('x', 'y')}


def distance(a, b):
    return math.hypot(a['x'] - b['x'], a['y'] - b['y'])


def guard_targets(plan, view):
    visible = {e['id']: e for e in rows(view.get('entities'))}
    for order in orders(plan):
        if order.get('action') == 'capture':
            if view.get('exploration',{}).get('source') != 'native-local-shroud':
                raise ValueError('Capture adapter not loaded; refresh the game before using this action')
            target = visible.get(order.get('target'))
            unit = visible.get(order.get('unit'))
            if order.get('units') is not None or not unit or unit.get('owner') != view.get('player') or unit.get('kind') != 'infantry' or unit.get('type') not in ('ENGINEER','SENGINEER','YENGINEER'):
                raise ValueError('Capture requires one visible owned engineer')
            if not target or target.get('relation') != 'neutral' or target.get('type') != 'CAOILD' or target.get('kind') != 'building':
                raise ValueError('Capture requires a visible neutral oil derrick')
        if order.get('action') == 'attack':
            target = visible.get(order.get('target'))
            if not target or target.get('relation') != 'enemy':
                raise ValueError('Attack rejected: target must be a currently visible confirmed enemy; neutral/ally/unknown is not an enemy')


class Tactics:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.session = None
        self.advance = None
        self.main = []
        self.camera_follow = []
        self.base = None
        self.camera_at = 0
        self.camera_hold = 0
        self.history = {}
        self.routes = {}
        self.notice = None
        self.visited = {}
        self.deploy_attempts = {}
        self.previous_owned = {}
        self.base_alert = None
        self.defense_recovery = None
        self.alert_serial = 0
        self.alert_reviewed = 0
        self.alert_camera_serial = 0

    def update(self, view):
        if self.session != view.get('sessionId'):
            clock = self.clock
            self.__init__(clock)
            self.session = view.get('sessionId')
        units = own(view)
        hits = []
        for key, entity in units.items():
            previous = self.previous_owned.get(key)
            if not previous or not isinstance(entity.get('health'), (int,float)) or not isinstance(previous.get('health'), (int,float)): continue
            critical = entity.get('kind') == 'building' or entity.get('type') in ('AMCV','SMCV','PCV','CMIN','HARV','HORV')
            if critical and entity['health'] < previous['health']:
                hits.append(dict(id=key,type=entity['type'],cell=copy.deepcopy(entity['cell']),
                                 previousHealth=previous['health'],health=entity['health']))
        self.previous_owned = copy.deepcopy(units)
        if hits:
            self.defense_recovery = None
            hits.sort(key=lambda e: (e['type'] not in ('GACNST','NACNST','YACNST'), e['health']))
            self.alert_serial += 1
            self.base_alert = dict(serial=self.alert_serial, hits=hits, cell=hits[0]['cell'], until=self.clock()+25)
            self.notice = 'Owned base/economy is losing health. Choose whether to inspect, defend, repair or continue your objective. Damage does not identify the attacker; no camera or army order was changed.'
        elif self.base_alert and self.clock() > self.base_alert['until']:
            area = self.base_alert['cell']
            visible = rows(view.get('entities'))
            nearby = [e for e in visible if e.get('cell') and distance(e['cell'],area) <= 14]
            inspected = any(e.get('owner') == view.get('player') and e.get('cell') and distance(e['cell'],area) <= 6 for e in nearby)
            if inspected and not any(e.get('relation') == 'enemy' for e in nearby):
                self.defense_recovery = dict(phase='locally-stable', cell=area, checkedAt=self.clock(),
                    evidence='No new base/economy damage for at least 25 seconds and no visible enemy near the inspected attack location. Local stabilization, not confirmed kills or match victory.')
                self.base_alert = None
                self.notice = 'Local attack area is stable for now. Choose your next objective; enemy retreat/destruction is not confirmed.'
            else:
                self.notice = 'Damage has stopped but the attack area is not verified clear. Inspect it before declaring the wave held.'
        for entity in units.values():
            if entity.get('type') in ('MTNK','HTNK','LTNK','TNKD','E1','E2','YTNK','LCRF'):
                cell = entity.get('cell')
                if cell:
                    key = (cell['x']//6, cell['y']//6)
                    self.visited[key] = dict(cell=dict(x=key[0]*6+3,y=key[1]*6+3), seenAt=self.clock())
        self.visited = dict(list(self.visited.items())[-256:])
        bases = [e for e in units.values() if e.get('type') in ('GACNST', 'NACNST', 'YACNST', 'AMCV', 'SMCV', 'PCV')]
        if bases: self.base = copy.deepcopy(bases[0]['cell'])
        if self.advance:
            active = [units[i] for i in self.advance['units'] if i in units]
            current = center(active)
            if not current:
                self.notice = 'Advance interrupted: owned squad no longer available'
                self.advance = None
            elif distance(current, self.advance['goal']) <= 3:
                self.notice = 'Squad reached the ordered area; this is not evidence of destroying enemies'
                self.advance = None
            elif distance(current, self.advance['last']) >= 2:
                self.advance['last'] = current
                self.advance['progressAt'] = self.clock()
            elif self.clock() - self.advance['progressAt'] > 35:
                self.notice = 'Advance stalled for 35 seconds: inspect route or combat before choosing a different waypoint; do not repeat the same order'
                self.advance = None

    def context(self):
        return dict(cameraFollow=list(self.camera_follow), mainSquad=self.main, base=self.base, advance=self.advance, notice=self.notice, baseAlert=self.base_alert, defenseRecovery=self.defense_recovery,
                    visitedSectors=list(self.visited.values()),
                    visitedMeaning='Own combat units passed through these sectors. Not proof every cell is revealed or still safe.')

    def opening(self, view):
        units = own(view)
        if any(e.get('type') in ('GACNST','NACNST','YACNST') for e in units.values()): return None
        mcv = next((e for e in units.values() if e.get('type') in ('AMCV','SMCV','PCV')), None)
        if not mcv: return None
        if mcv['id'] not in {e['id'] for e in rows(view.get('entities'))}:
            return dict(method='look',params=dict(cell=mcv['cell']),say='先回到基地车，建立生产基础')
        attempts = self.deploy_attempts.get(mcv['id'], [])
        if attempts and self.clock()-attempts[-1] < 20:
            return dict(method='observe',params={},say='等待基地车展开结果，不重复提交')
        if len(attempts) >= 2:
            self.notice = 'MCV failed to become a construction yard after two deploy attempts. Move MCV a short distance to clear terrain, then retry. No army expedition before establishing production.'
            return None
        return dict(method='act',params=dict(action='deploy',unit=mcv['id']),say='先展开基地车，回读确认建造厂后再发展')

    def strategy_guard(self, plan, view, autonomous=True, model_led=False):
        if not autonomous or model_led: return
        scout = view.get('opportunities',{}).get('scout')
        if scout and not self.base_alert:
            for order in ([plan['params']] if plan.get('method')=='advance' else orders(plan)):
                ids = order.get('units',[order['unit']] if 'unit' in order else [])
                if scout['unit'] in ids:
                    raise ValueError('Scout already has an active frontier; preserve its order until arrival or stall')
        reserved = {o.get('engineer') for o in view.get('opportunities',{}).get('oils',{}).values() if o.get('status') in ('approaching','pending')}
        if not self.base_alert:
            for order in orders(plan):
                ids = order.get('units',[order['unit']] if 'unit' in order else [])
                if reserved.intersection(ids) and order.get('action') in ('move','attack','attack-move','stop','guard'):
                    raise ValueError('Engineer is assigned to oil capture; preserve its order until verification or timeout')
        if self.base_alert:
            destinations = ([plan['params']] if plan.get('method') == 'advance' else
                            [o for o in orders(plan) if o.get('action') in ('move','attack-move')])
            for order in destinations:
                target = order.get('cell')
                if target and distance(target,self.base_alert['cell']) > 14:
                    raise ValueError('Base under attack: suspend expedition and inspect/defend attacked area; do not send forces farther away')
        units = own(view)
        mcvs = {e['id'] for e in units.values() if e.get('type') in ('AMCV','SMCV','PCV')}
        has_yard = any(e.get('type') in ('GACNST','NACNST','YACNST') for e in units.values())
        if mcvs and not has_yard:
            if plan.get('method') == 'advance':
                raise ValueError('Opening incomplete: deploy MCV and confirm construction yard before army expedition')
            for order in orders(plan):
                ids = set(order.get('units',[order['unit']] if 'unit' in order else []))
                if order.get('action') in ('move','attack-move','attack') and not ids.issubset(mcvs):
                    raise ValueError('Opening incomplete: establish construction yard before sending army out')
        destinations = ([plan['params']] if plan.get('method') == 'advance' else
                        [o for o in orders(plan) if o.get('action') in ('move','attack-move')])
        enemies = [e for e in rows(view.get('entities')) if e.get('relation') == 'enemy']
        damaged = any(e.get('health',1000) < 150 for e in units.values() if e['id'] in self.main)
        # Visited terrain is navigation evidence, not a forbidden zone. Only suppress
        # rapid order reversals when the same unit has made no observable progress.
        if not self.base_alert and not enemies:
            for dest in destinations:
                ids = dest.get('units',[dest['unit']] if 'unit' in dest else [])
                target = dest.get('cell')
                if not target: continue
                for key in ids:
                    entity = units.get(key)
                    if not entity or not entity.get('cell') or key in mcvs: continue
                    history = [r for r in self.routes.get(key, []) if self.clock()-r['at'] < 60][-3:]
                    if len(history) < 3: continue
                    candidate = dict(target=target,cell=entity['cell'],health=entity.get('health'))
                    route = history + [candidate]
                    unchanged = all(distance(r['cell'],candidate['cell']) <= 2 and r['health']==candidate['health'] for r in history)
                    alternating = (distance(route[0]['target'],route[2]['target']) <= 2 and
                                   distance(route[1]['target'],target) <= 2 and distance(route[0]['target'],target) >= 5)
                    if unchanged and alternating:
                        raise ValueError('Same unit received alternating destinations without moving or health change. Inspect its route; visited terrain itself is allowed')
        if self.advance and plan.get('method') == 'advance' and not enemies and not damaged and not self.base_alert:
            selected = set(plan['params'].get('units', []))
            overlap = selected.intersection(self.advance['units'])
            if overlap and distance(plan['params']['cell'],self.advance['goal']) > 3:
                raise ValueError('These units already have an active destination. Wait for arrival/stall or command another group; old terrain remains allowed')

    def begin(self, params, view):
        units = own(view)
        selected = params['units']
        if not selected or len(set(selected)) != len(selected) or any(i not in units for i in selected):
            raise ValueError('Advance requires distinct currently owned units')
        if any(units[i].get('kind') not in ('vehicle', 'infantry') or units[i].get('type') in
               ('AMCV','SMCV','PCV','HARV','HORV','CMIN','ENGINEER','SENGINEER','YENGINEER') for i in selected):
            raise ValueError('Advance requires ground combat units; protect miners, MCVs and engineers')
        visible = {e['id'] for e in rows(view.get('entities'))}
        if any(i not in visible for i in selected):
            raise ValueError('Look at the tracked squad cell before issuing an advance')
        return {'method':'act','params':{'action':'attack-move','units':selected,'cell':params['cell']}}

    def accepted(self, original, executed, view, receipt):
        if receipt.get('uncertain') or receipt.get('exitCode', 0): return
        moving = []
        units = own(view)
        for order in orders(executed):
            if order.get('action') == 'deploy':
                self.deploy_attempts.setdefault(order.get('unit'), []).append(self.clock())
            if order.get('action') == 'move' and order.get('unit') in self.deploy_attempts:
                self.deploy_attempts.pop(order['unit'], None)
            if order.get('action') in ('move', 'attack-move', 'attack'):
                ids = order.get('units', [order['unit']] if 'unit' in order else [])
                units = own(view)
                mobile = [i for i in ids if i in units and units[i].get('kind') != 'building']
                moving.extend(mobile)
                if order.get('cell'):
                    for key in mobile:
                        entry = dict(target=copy.deepcopy(order['cell']),cell=copy.deepcopy(units[key]['cell']),
                                     health=units[key].get('health'),at=self.clock())
                        self.routes[key] = (self.routes.get(key, []) + [entry])[-3:]
        self.routes = {key:entries for key,entries in self.routes.items() if key in units and self.clock()-entries[-1]['at'] < 60}
        if moving: self.main = list(dict.fromkeys(moving))
        if original.get('method') == 'advance':
            params = original['params']
            current = center([own(view)[i] for i in params['units'] if i in own(view)])
            if current:
                self.advance = dict(units=params['units'], goal=params['cell'], last=current, progressAt=self.clock())
                self.notice = None
        if original.get('method') == 'look':
            self.camera_follow = []
            self.camera_hold = self.clock() + 12

    def configure_camera(self, params, view):
        selected = params['units'] if params['enabled'] else []
        owned = own(view)
        if any(i not in owned or not owned[i].get('cell') for i in selected):
            raise ValueError('Camera follow requires currently owned units with known positions')
        self.camera_follow = list(selected)
        self.camera_hold = 0
        return dict(exitCode=0, uncertain=False, status='镜头跟随已开启' if selected else '镜头跟随已关闭', units=list(selected))

    def camera(self, view):
        if not self.camera_follow: return None
        if self.clock() < self.camera_hold or self.clock() - self.camera_at < 6: return None
        units = own(view)
        active = [units[i] for i in self.camera_follow if i in units]
        if not active: return None
        # Follow an actual squad member near its center, never an empty midpoint between split forces.
        middle = center(active)
        anchor = min(active, key=lambda e: distance(e['cell'], middle))
        screen = next((e.get('screen') for e in rows(view.get('entities')) if e['id'] == anchor['id']), None)
        viewport = view.get('viewport', {})
        if screen and viewport.get('width') and viewport.get('height'):
            if viewport['width']*.25 < screen['x'] < viewport['width']*.75 and viewport['height']*.25 < screen['y'] < viewport['height']*.75:
                return None
        return {'method':'look','params':{'cell':anchor['cell']}}

    def check_repeat(self, plan, view):
        visible = {e['id']:e for e in rows(view.get('entities'))}
        entities = {**own(view), **visible}
        for order in orders(plan):
            if order.get('action') not in ('attack','attack-move','move','guard','stop'): continue
            ids = order.get('units', [order['unit']] if 'unit' in order else [])
            key = json.dumps({**order, 'units':sorted(ids)}, sort_keys=True)
            progress = json.dumps([(i, entities.get(i, {}).get('cell'), entities.get(i, {}).get('health'))
                                   for i in sorted(set(ids + ([order['target']] if order.get('target') else [])))], sort_keys=True)
            previous = self.history.get(key)
            if previous and previous['progress'] == progress and previous['count'] >= 2 and self.clock()-previous['at'] < 90:
                raise ValueError('Repeated unchanged order blocked: no positional/known-health progress; inspect route, target relation or choose a different plan')

    def record(self, plan, view):
        entities = {**own(view), **{e['id']:e for e in rows(view.get('entities'))}}
        for order in orders(plan):
            if order.get('action') not in ('attack','attack-move','move','guard','stop'): continue
            ids = order.get('units', [order['unit']] if 'unit' in order else [])
            key = json.dumps({**order, 'units':sorted(ids)}, sort_keys=True)
            progress = json.dumps([(i, entities.get(i, {}).get('cell'), entities.get(i, {}).get('health'))
                                   for i in sorted(set(ids + ([order['target']] if order.get('target') else [])))], sort_keys=True)
            previous = self.history.get(key, {})
            count = previous.get('count', 0)+1 if previous.get('progress') == progress else 1
            self.history[key] = dict(progress=progress,count=count,at=self.clock())
        self.history = {k:v for k,v in self.history.items() if self.clock()-v['at'] < 90}
