"""Persistent, bounded scouting and neutral oil capture from own/visible evidence."""
import copy
import json
import time
from game_context import rows
from game_tactics import own, distance

ENGINEERS = {'ENGINEER', 'SENGINEER', 'YENGINEER'}
SCOUTS = {'E1', 'E2', 'DOG', 'ADOG', 'MTNK', 'HTNK', 'LTNK', 'YTNK'}


class Opportunities:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        self.session, self.state = None, {}
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS game_opportunities (session TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def save(self):
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO game_opportunities VALUES (?,?)',
                       (self.session, json.dumps(self.state)))

    def update(self, view):
        if self.session != view['sessionId']:
            self.session = view['sessionId']
            with self.store.connect() as db:
                row = db.execute('SELECT data FROM game_opportunities WHERE session=?', (self.session,)).fetchone()
            self.state = json.loads(row[0]) if row else dict(oils={}, scout=None, visited={}, lastTrain=0)
        now, units = self.clock(), own(view)
        for e in rows(view.get('entities')):
            if e.get('type') != 'CAOILD': continue
            old = self.state['oils'].get(e['id'], {})
            if old.get('status') == 'captured' and e.get('relation') != 'self': old = {}
            self.state['oils'][e['id']] = {**old, 'id':e['id'], 'cell':e['cell'],
                'relation':e.get('relation'), 'lastSeen':now}
        for key, oil in self.state['oils'].items():
            if key in units and units[key].get('type') == 'CAOILD':
                if oil.get('status') != 'captured':
                    self.store.record('oil-captured', dict(session=self.session, target=key, evidence='owned-observation'))
                oil.update(status='captured', relation='self')
            elif oil.get('status') in ('pending','approaching') and now-oil['sentAt'] > 90:
                oil.update(status='unconfirmed', retryAfter=now+180)
        scout = self.state.get('scout')
        if scout:
            unit = units.get(scout['unit'])
            if not unit or distance(unit['cell'], scout['cell']) <= 3 or now-scout['progressAt'] > 35:
                self.state['visited'][self.key(scout['cell'])] = now+180
                self.store.record('scout-finished', dict(session=self.session, scout=scout,
                    reason='unit-missing' if not unit else 'arrived' if distance(unit['cell'],scout['cell'])<=3 else 'stalled'))
                self.state['scout'] = None
            elif distance(unit['cell'], scout['last']) >= 2:
                scout.update(last=unit['cell'], progressAt=now)
        self.state['visited'] = {k:v for k,v in self.state['visited'].items() if v>now}
        self.save()
        return copy.deepcopy(self.state)

    @staticmethod
    def key(cell):
        return f"{cell['x']//6},{cell['y']//6}"

    def plan(self, view):
        now, units = self.clock(), own(view)
        visible = {e['id']:e for e in rows(view.get('entities'))}
        enemies = [e for e in visible.values() if e.get('relation') == 'enemy']
        if view.get('tactics', {}).get('baseAlert'): return None
        if view.get('exploration',{}).get('source') != 'native-local-shroud': return None
        # One engineer assignment at a time. A receipt never establishes capture.
        pending = any(o.get('status') == 'pending' for o in self.state['oils'].values())
        oil_options = [o for o in self.state['oils'].values() if o.get('relation')=='neutral'
            and o.get('status')!='captured' and o.get('retryAfter',0)<=now
            and not any(distance(e['cell'],o['cell'])<14 for e in enemies)]
        engineers = [e for e in units.values() if e.get('type') in ENGINEERS]
        if oil_options and not pending:
            anchor = engineers[0]['cell'] if engineers else next(iter(units.values()),{}).get('cell')
            oil = min(oil_options, key=lambda o:distance(o['cell'],anchor)) if anchor else oil_options[0]
            if engineers:
                engineer = min(engineers, key=lambda e:distance(e['cell'],oil['cell']))
                if engineer['id'] not in visible:
                    return dict(method='look',params={'cell':engineer['cell']})
                if distance(engineer['cell'],oil['cell']) > 6:
                    if oil.get('status') == 'approaching': return None
                    return dict(method='act',params={'action':'move','unit':engineer['id'],'cell':oil['cell']}, oilTarget=oil['id'])
                if oil['id'] not in visible:
                    if now-oil.get('checkedAt',0)>30:
                        oil['checkedAt'] = now; self.save()
                        return dict(method='look',params={'cell':oil['cell']})
                else:
                    return dict(method='act',params={'action':'capture','unit':engineer['id'],'target':oil['id']})
            elif (not any(p.get('type') in ENGINEERS for p in view.get('production',[]))
                  and view.get('cash',0)>=1000 and now-self.state['lastTrain']>=120):
                option = next((e['type'] for e in rows(view.get('sidebar')) if e.get('type') in ENGINEERS),None)
                if option: return dict(method='act',params={'action':'produce','type':option})
        if (self.state.get('scout') or enemies or view.get('strategy',{}).get('phase')!='scout'):
            return None
        exploration = view.get('exploration',{})
        if exploration.get('source') != 'native-local-shroud': return None
        frontiers = [c for c in exploration.get('frontiers',[]) if self.key(c) not in self.state['visited']]
        scouts = [e for e in units.values() if e.get('type') in SCOUTS and e.get('health',0)>0]
        if not frontiers or not scouts: return None
        unit, goal = min(((u,c) for u in scouts for c in frontiers), key=lambda pair:distance(pair[0]['cell'],pair[1]))
        if unit['id'] not in visible: return dict(method='look',params={'cell':unit['cell']})
        return dict(method='act',params={'action':'move','unit':unit['id'],'cell':goal})

    def accepted(self, plan, view, receipt, local=False):
        if receipt.get('uncertain') or receipt.get('exitCode',0): return
        if plan.get('method') != 'act': return
        p, now = plan['params'], self.clock()
        if p.get('action')=='produce' and p.get('type') in ENGINEERS:
            self.state['lastTrain'] = now
        elif p.get('action')=='capture' and p.get('target') in self.state['oils']:
            self.state['oils'][p['target']].update(status='pending',sentAt=now,engineer=p['unit'])
        elif plan.get('oilTarget') and p.get('action')=='move':
            self.state['oils'][plan['oilTarget']].update(status='approaching',sentAt=now,engineer=p['unit'])
        elif local and p.get('action')=='move' and p.get('unit') in own(view):
            self.state['scout'] = dict(unit=p['unit'],cell=p['cell'],last=own(view)[p['unit']]['cell'],progressAt=now)
        self.save()
