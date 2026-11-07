"""Session-scoped strategic objectives derived only from owned/visible evidence."""
import copy
import json
import time
from collections import Counter
from game_context import rows
from game_tactics import own, center, distance

YARDS = {'GACNST','NACNST','YACNST'}
MINERS = {'CMIN','HARV','HORV'}
FACTORIES = {'GAWEAP','NAWEAP','YAWEAP'}
NONCOMBAT = MINERS | {'AMCV','SMCV','PCV','ENGINEER','SENGINEER','YENGINEER'}

class Strategy:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        self.session = None
        self.state = {}
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS game_strategy (session TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def load(self, session):
        self.session = session
        with self.store.connect() as db:
            saved = db.execute('SELECT data FROM game_strategy WHERE session=?',(session,)).fetchone()
            if saved:
                self.state = json.loads(saved[0]); return
            # Bootstrap this match's previously seen enemies, never another match's knowledge.
            prior = db.execute("SELECT created,json_extract(data,'$.before.entities'),json_extract(data,'$.after.entities') FROM events WHERE kind='step' AND json_extract(data,'$.before.sessionId')=? ORDER BY created DESC LIMIT 1000",(session,)).fetchall()
        self.state = dict(session=session, enemies={}, objective=None, phase=None, resumePhase=None)
        for created, before, after in reversed(prior):
            for raw in (before,after):
                self.remember(rows(json.loads(raw)) if raw else [], created)

    def remember(self, entities, now):
        for e in entities:
            if e.get('relation') != 'enemy': continue
            self.state['enemies'][e['id']] = dict(id=e['id'],type=e.get('type'),kind=e.get('kind'),
                cell=copy.deepcopy(e['cell']),lastSeenAt=now,status='last-seen')
        self.state['enemies'] = dict(sorted(self.state['enemies'].items(), key=lambda kv:kv[1]['lastSeenAt'])[-128:])

    def update(self, view, tactics):
        if view.get('sessionId') != self.session: self.load(view['sessionId'])
        now = self.clock()
        self.remember(rows(view.get('entities')), now)
        units = own(view)
        counts = Counter(e.get('type') for e in units.values())
        army = [e for e in units.values() if e.get('kind') in ('vehicle','infantry') and e.get('type') not in NONCOMBAT]
        tanks = [e for e in army if e.get('kind') == 'vehicle' and e.get('health',0) >= 150]
        # Choose a spatially coherent force rather than pretending scattered units are one squad.
        groups = [[e for e in tanks if distance(e['cell'],anchor['cell']) <= 12] for anchor in tanks]
        squad = max(groups,key=len,default=[])
        previous = self.state.get('objective') or {}
        old = (self.state.get('phase'),previous.get('id'),previous.get('cell'))
        visible = {e['id'] for e in rows(view.get('entities')) if e.get('relation')=='enemy'}
        target = self.state.get('objective')
        if target and target.get('id') in self.state['enemies']:
            target = copy.deepcopy(self.state['enemies'][target['id']])
            # Absence in a nearby inspected view means re-scout, not a confirmed kill.
            nearby = any(e.get('owner')==view.get('player') and distance(e['cell'],target['cell']) <= 4 for e in rows(view.get('entities')))
            if target['id'] not in visible and nearby and now-target['lastSeenAt'] > 15:
                self.state['enemies'][target['id']]['status']='not-visible-on-revisit'
                target = None
        if not target:
            buildings = [e for e in self.state['enemies'].values() if e.get('kind')=='building' and e['status']=='last-seen']
            buildings.sort(key=lambda e:(e['type'] not in YARDS,e['type'] not in FACTORIES,-e['lastSeenAt']))
            target = copy.deepcopy(buildings[0]) if buildings else None
        self.state['objective'] = target
        if tactics.get('baseAlert'):
            if self.state.get('phase') != 'defend': self.state['resumePhase'] = self.state.get('phase')
            phase, action = 'defend', 'Defend the attacked base area. Preserve the offensive objective; after local stabilization resume the campaign.'
        elif not any(counts[t] for t in YARDS):
            phase, action = 'establish-base', 'Deploy MCV or recover construction capability; do not substitute wandering for building a base.'
        elif not any(counts[t] for t in MINERS) or not any(counts[t] for t in FACTORIES):
            phase, action = 'restore-economy', 'Restore mining and a vehicle factory using currently legal production. Already-produced troops can defend or scout.'
        elif not target:
            phase, action = 'scout', 'Locate an enemy production/base structure with a small scout force. Keep the bulk of tanks grouped. Empty viewport is not victory.'
        elif len(squad) < 6:
            phase, action = 'rally', 'Gather surviving tanks into a coherent squad toward the remembered enemy objective. Existing routes are allowed; do not wait for unlimited production.'
        else:
            phase, action = 'attack', 'Advance the coherent squad toward the remembered enemy objective, reobserve it, then attack confirmed visible enemies. Continue reinforcement instead of endless base construction.'
        self.state.update(phase=phase, nextAction=action, updatedAt=now,
            force=dict(totalCombat=len(army),healthyTanks=len(tanks),coherentTanks=len(squad),
                       squad=[e['id'] for e in squad[:24]],rallyCell=center(squad)),
            counts=dict(counts),
            victory=dict(authority='native-game-loop',shortGame='unknown',
                         goal='Defeat hostile players; enemy base/production is a strategic target, not a victory trigger.',
                         completion='Only native victory ends the campaign. A defended wave, absent enemy, or destroyed building alone does not establish match victory.'))
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO game_strategy VALUES (?,?)',(self.session,json.dumps(self.state,ensure_ascii=False)))
        if old != (phase,(target or {}).get('id'),(target or {}).get('cell')):
            self.store.record('strategy-transition',dict(session=self.session,phase=phase,objective=target,nextAction=action))
        return self.context()

    def context(self):
        result=copy.deepcopy(self.state)
        result['enemies']=list(result.get('enemies',{}).values())
        result['evidenceBoundary']='Enemy locations are frozen sightings, not live hidden positions. Re-scout before attack; do not infer destruction from absence.'
        return result
