"""Bounded local site search. Only the native game decides placement legality."""
import time
from game_context import rows


def overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class Placement:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    def candidates(self, view, kind):
        sizes = {e['type']: e['foundation'] for e in rows(view.get('sidebar')) if e.get('foundation')}
        size = sizes.get(kind)
        if not size: return []
        w, h = size['width'], size['height']
        entities = {e['id']: e for e in rows(view.get('owned'))}
        entities.update({e['id']: e for e in rows(view.get('entities'))})
        obstacles, anchors = [], []
        for e in entities.values():
            cell = e.get('cell')
            if not cell: continue
            bounds = e.get('foundation') or sizes.get(e.get('type')) or {'width': 1, 'height': 1}
            rect = (cell['x'], cell['y'], cell['x']+bounds['width'], cell['y']+bounds['height'])
            obstacles.append(rect)
            if e.get('kind') == 'building' and e.get('owner') == view.get('player') and (e.get('foundation') or e.get('type') in sizes):
                anchors.append(rect)
        # Persisted step receipts survive controller restarts. A cooldown permits changed terrain/occupancy.
        rejected = set()
        for event in self.store.events(500):
            data = event['data']
            if event['kind'] != 'step' or data.get('before', {}).get('sessionId') != view.get('sessionId'): continue
            if self.clock()-event['created'] > 120: continue
            for receipt in data.get('receipt', {}).get('events', []):
                if receipt.get('type') == kind and receipt.get('event') in ('placement-rejected', 'placement-not-confirmed'):
                    cell = receipt.get('cell', {})
                    rejected.add((cell.get('x'), cell.get('y')))
        scores = {}
        for ax, ay, bx, by in anchors:
            for x in range(max(0, ax-w-3), min(512-w, bx+3)+1):
                for y in range(max(0, ay-h-3), min(512-h, by+3)+1):
                    rect = (x, y, x+w, y+h)
                    if (x, y) in rejected or any(overlaps(rect, other) for other in obstacles): continue
                    gap = max(ax-x-w, x-bx, 0) + max(ay-y-h, y-by, 0)
                    # Prefer close side adjacency over corner-only extensions; this is a ranking, not legality.
                    side = min(abs(x+w-ax), abs(x-bx), abs(y+h-ay), abs(y-by))
                    score = (gap, side, abs(x+w/2-(ax+bx)/2)+abs(y+h/2-(ay+by)/2), x, y)
                    scores[(x, y)] = min(score, scores.get((x, y), score))
        return [{'x': x, 'y': y} for x, y in sorted(scores, key=scores.get)[:6]]

    def check(self, view, kind, cells):
        """Read-only geometry checks of model-selected cells; never move or place."""
        size = next((e.get('foundation') for e in rows(view.get('sidebar')) if e.get('type') == kind), None)
        entities = {e['id']: e for e in rows(view.get('owned')) + rows(view.get('entities'))}
        results = []
        for cell in cells:
            blockers = []
            if size:
                rect = (cell['x'], cell['y'], cell['x']+size['width'], cell['y']+size['height'])
                for e in entities.values():
                    c = e.get('cell')
                    if not c: continue
                    extent = e.get('foundation') or {'width':1,'height':1}
                    if overlaps(rect, (c['x'],c['y'],c['x']+extent['width'],c['y']+extent['height'])):
                        blockers.append(e['id'])
            results.append(dict(cell=cell, status='blocked' if blockers else 'unknown', blockers=blockers,
                reason='已知占地冲突' if blockers else '未确认原生地形和建造范围，不能保证可放置'))
        return dict(exitCode=0, uncertain=False, events=results,
            status='仅检查指定坐标，未摆放、未替换坐标；最终合法性由游戏判断')
