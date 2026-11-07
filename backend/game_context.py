"""Loss-aware model context and local decision wakeups. Raw evidence stays in SQLite."""
import time


def rows(value):
    if isinstance(value, list): return value
    if isinstance(value, dict) and 'columns' in value:
        extras = value.get('extra') or {}
        return [dict(dict(zip(value['columns'], row)), **(extras.get(str(i), extras.get(i, {})) if isinstance(extras, dict) else extras[i] if i < len(extras) else {}))
                for i, row in enumerate(value.get('rows', []))]
    return []


def table(items):
    if not items: return []
    keys = list(dict.fromkeys(k for item in items for k in item))
    return {'columns': keys, 'rows': [[item.get(k) for k in keys] for item in items]}


def compact(view):
    result = dict(view)
    result['entities'] = table([{k:v for k,v in e.items() if k not in ('position', 'screen')}
                                for e in rows(view.get('entities'))])
    if 'owned' in view: result['owned'] = table(rows(view['owned']))
    result['sidebar'] = table(rows(view.get('sidebar')))
    return result


def receipt_summary(value):
    if isinstance(value, list): return [receipt_summary(v) for v in value]
    if isinstance(value, dict):
        return {k:receipt_summary(v) for k,v in value.items()
                if k not in ('observation', 'before', 'after', 'screenshot', 'view')}
    return value


def step_summary(event):
    data = event['data']
    before, after = data.get('before', {}), data.get('after', {})
    old = {e.get('id'):e for e in rows(before.get('entities'))}
    new = {e.get('id'):e for e in rows(after.get('entities'))}
    return dict(id=event['id'], session=before.get('sessionId'), plan=data.get('plan'),
                receipt=receipt_summary(data.get('receipt', {})),
                cash=[before.get('cash'), after.get('cash')],
                production=[before.get('production'), after.get('production')],
                changedEntities=table([{k:v for k,v in e.items() if k not in ('screen','position')}
                                       for key,e in new.items() if old.get(key)!=e]),
                previousChangedEntities=table([{k:v for k,v in e.items() if k not in ('screen','position')}
                                       for key,e in old.items() if new.get(key)!=e]),
                leftView=[key for key in old if key not in new],
                note='Only visible changes; leaving view is not death. Accepted is not completed.')


class DecisionGate:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.previous = None
        self.last = 0
        self.skipped = 0

    def reason(self, view):
        previous = self.previous
        if previous is None or previous.get('sessionId') != view.get('sessionId'): return 'new-session'
        old = {e.get('id'): e for e in rows(previous.get('owned', previous.get('entities')))}
        new = {e.get('id'): e for e in rows(view.get('owned', view.get('entities')))}
        for key, e in new.items():
            if e.get('owner') == view.get('player') and key in old:
                health, prior = e.get('health'), old[key].get('health')
                if isinstance(health, (int,float)) and isinstance(prior, (int,float)) and health < prior: return 'damage'
        if self.clock()-self.last >= 3:
            if set(old) != set(new): return 'visible-roster-change'
            ready = lambda v: sorted(p['type'] for p in v.get('production', []) if p.get('total') is not None and p.get('progress') == p['total'])
            if ready(previous) != ready(view): return 'production-ready'
            if previous.get('supportPowers') != view.get('supportPowers'): return 'support-change'
            if abs(view.get('cash', 0)-previous.get('cash', 0)) >= 1000: return 'economy-change'
            if {e.get('type') for e in rows(previous.get('sidebar'))} != {e.get('type') for e in rows(view.get('sidebar'))}: return 'production-options'
        if self.clock()-self.last >= 15: return 'periodic-check'
        self.skipped += 1
        return None

    def mark(self, view):
        self.previous = view
        self.last = self.clock()
