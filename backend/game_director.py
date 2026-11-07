"""Isolated game director: one writer, viewer priority, evidence-backed learning.

No Bilibili publishing or production persona/memory writes. Defaults paused.
"""
import json
import sqlite3
import threading
import time
import uuid
import queue
import copy
from game_context import DecisionGate, rows, receipt_summary
from game_tactics import Tactics, guard_targets
from game_strategy import Strategy
from game_objectives import Objectives, validate_command_visibility
from game_placement import Placement
from game_opportunities import Opportunities
from game_learning import Learning, event_summary
from review_jobs import ReviewJobs
from lesson_lifecycle import LessonLifecycle
from game_model_retry import ModelUnavailable, request_model
from live_coalesce import identity, merge_comments, take_same_viewer
from pathlib import Path
from contextlib import contextmanager


class GameStore:
    def __init__(self, path):
        self.db_lock = threading.RLock()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, kind TEXT, created REAL, data TEXT);
                CREATE INDEX IF NOT EXISTS events_created ON events(created);
                CREATE TABLE IF NOT EXISTS lessons (
                    id TEXT PRIMARY KEY, created REAL, text TEXT, evidence TEXT,
                    status TEXT NOT NULL DEFAULT 'candidate');
            ''')
        self.learning = Learning(self)

    @contextmanager
    def connect(self):
        # Serialize local writers and allow nested read helpers on the same thread.
        # WAL permits independent readers while a short write transaction commits.
        with self.db_lock:
            db=sqlite3.connect(self.path, timeout=30)
            db.execute('PRAGMA busy_timeout=30000')
            try:
                with db: yield db
            finally: db.close()

    def record(self, kind, data, event_id=None):
        event_id = event_id or uuid.uuid4().hex
        with self.connect() as db:
            import math
            last = db.execute('SELECT MAX(created) FROM events').fetchone()[0]
            created = max(time.time(), math.nextafter(last, math.inf) if last is not None else 0)
            result = db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?)',
                                (event_id, kind, created, json.dumps(data, ensure_ascii=False)))
        return event_id if result.rowcount else None

    def events(self, limit=50):
        with self.connect() as db:
            rows = db.execute('SELECT id,kind,created,data FROM events ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
        return [dict(id=r[0], kind=r[1], created=r[2], data=json.loads(r[3])) for r in reversed(rows)]

    def learn(self, text, evidence):
        if not isinstance(text, str) or not 1 <= len(text) <= 1200 or not evidence:
            raise ValueError('Invalid lesson')
        with self.connect() as db:
            for event_id in evidence:
                if not db.execute("SELECT 1 FROM events WHERE id=? AND kind='step'", (event_id,)).fetchone():
                    raise ValueError('Lesson requires recorded game-step evidence')
            lesson_id = uuid.uuid4().hex
            db.execute('INSERT INTO lessons VALUES (?,?,?,?,?)',
                       (lesson_id, time.time(), text, json.dumps(evidence), 'candidate'))
        return lesson_id

    def recall(self, view=None):
        return self.learning.recall(view)



def validate_plan(plan):
    """Reject arbitrary tool names, protocol injection, and unbounded command batches."""
    if not isinstance(plan, dict):
        raise ValueError('Expected game plan')
    method, params = plan.get('method'), plan.get('params', {})
    keys = {
        'observe': set(), 'look': {'cell'}, 'camera-follow': {'enabled','units'}, 'advance': {'units', 'cell'},
        'act': {'action', 'unit', 'units', 'type', 'target', 'cell'},
        'batch': {'orders', 'onRejected'},
        'place-building': {'type', 'cells'}, 'build': {'type', 'cells'}, 'check-placement': {'type', 'cells'},
    }
    if method not in keys or not isinstance(params, dict) or set(params) - keys[method]:
        raise ValueError('Unsupported game operation')
    if method == 'camera-follow':
        units = params.get('units', [])
        if type(params.get('enabled')) is not bool or not isinstance(units,list) or len(units)>64 or any(not isinstance(i,str) or not 1<=len(i)<=80 for i in units):
            raise ValueError('Camera follow requires enabled boolean and bounded unit IDs')
        if params['enabled'] and not units: raise ValueError('Choose units to follow')
        if not params['enabled'] and units: raise ValueError('Disabled follow must not include units')
    actions = {'produce', 'move', 'attack', 'capture', 'attack-move', 'guard', 'stop', 'deploy', 'repair', 'cancel'}
    orders = params.get('orders', []) if method == 'batch' else [params] if method == 'act' else []
    if method == 'batch' and (not isinstance(orders, list) or not 1 <= len(orders) <= 8):
        raise ValueError('Invalid batch')
    if params.get('onRejected', 'stop') not in {'stop', 'continue'}:
        raise ValueError('Invalid failure boundary')
    for order in orders:
        if not isinstance(order, dict) or set(order) - keys['act'] or order.get('action') not in actions:
            raise ValueError('Unsupported action')
        units = order.get('units', [order['unit']] if 'unit' in order else [])
        if not isinstance(units, list) or len(units) > 64 or any(not isinstance(v, str) or len(v) > 80 for v in units):
            raise ValueError('Invalid unit IDs')
    if method == 'advance':
        if not isinstance(params.get('units'), list) or not 1 <= len(params['units']) <= 64 or any(not isinstance(i, str) for i in params['units']) or 'cell' not in params:
            raise ValueError('Advance requires units and a destination cell')
    cells = params.get('cells', []) if method in ('place-building','build','check-placement') else []
    if method in ('place-building','build','check-placement') and (not isinstance(cells, list) or not 1 <= len(cells) <= 6):
        raise ValueError('Invalid placement candidates')
    if method in ('place-building','build','check-placement') and (not isinstance(params.get('type'), str) or not 1 <= len(params['type']) <= 80):
        raise ValueError('Invalid building type')
    for cell in [obj.get('cell') for obj in [params] + orders] + cells:
        if cell is not None and (not isinstance(cell, dict) or set(cell) != {'x','y'} or
                                any(type(v) is not int or not 0 <= v <= 512 for v in cell.values())):
            raise ValueError('Invalid map cell')
    return method, params


class UnsubmittedPlanError(ValueError):
    """Validation failed before any game write was attempted."""


class GameDirector:
    """Injected model/adapter make all scheduling and recovery behavior testable."""
    def __init__(self, store, model, adapter):
        self.store, self.model, self.adapter = store, model, adapter
        self.review_jobs = ReviewJobs(store,model) if hasattr(model,'reflect') else None
        self.placement = Placement(store)
        self.opportunities = Opportunities(store)
        self.model_failures = 0
        self.lock = threading.RLock()
        self.writer = threading.Lock()
        self.pending = []
        self.replies = []
        self.enabled = False
        self.mode = 'chat'
        self.epoch = 0
        self.session = None
        self.status = '已暂停'
        self.last_error = None
        self.stop_event = threading.Event()
        self.consumer = None
        self.consumer_until = 0
        self.steps_since_review = 0
        self.rounds = None
        self.gate = DecisionGate()
        self.tactics = Tactics()
        self.strategy = Strategy(store)
        self.objectives = Objectives(store)
        self.latest_observation = None
        self.latest_observed_at = None
        self.skill = None
        self.read_retry_at = 0
        self.read_failures = 0
        self.reply_jobs = queue.Queue(maxsize=40)

    def snapshot(self):
        with self.lock:
            return dict(enabled=self.enabled, mode=self.mode, status=self.status, queued=len(self.pending),
                        session=self.session, error=self.last_error, lessons=self.store.recall(),
                        campaignObjective=self.objectives.current(self.session) if self.session else None,
                        reviewJobs=self.review_jobs.status() if self.review_jobs else [],lessonReviews=LessonLifecycle(self.store).status(),gameKnowledge=self.store.learning.knowledge.context(),
                        localWaits=self.gate.skipped, skill=self.skill, tactics=self.tactics.context(), strategy=self.strategy.context(), autoNext=self.rounds.enabled() if self.rounds else False)

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Boolean required')
        with self.lock:
            self.enabled = enabled
            self.read_retry_at = 0
            self.read_failures = 0
            self.skill = None
            self.tactics.advance = None
            self.gate.previous = None
            if enabled: self.mode = 'game'
            self.epoch += 1
            self.status = '等待战况' if enabled else '已暂停'
        self.store.record('control', {'enabled': enabled})

    def chat_mode(self):
        self.set_enabled(False)
        with self.lock:
            self.mode = 'chat'
            for item in self.pending:
                self.reply(item, '已切回聊天直播，这条游戏指令不再执行。', 'cancelled')
            self.pending.clear()

    def receive_live(self, name, text, event_id, uid=None):
        if self.mode != 'game': return False
        try: self.submit(name, text, event_id, uid=uid)
        except ValueError:
            self.reply({'name':name,'text':text}, '游戏队列已满，暂时没有执行这条。', 'busy')
        return True

    def submit(self, name, text, event_id=None, uid=None):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
            raise ValueError('Invalid viewer message')
        with self.lock:
            if len(self.pending) >= 20:
                raise ValueError('指令队列已满，请稍后再试')
            event_id = self.store.record('viewer', {'name':str(name)[:40], 'text':text, 'uid':identity(uid)}, event_id)
            if event_id is None:
                return None
            # Reserve immediately, before classification; invalidate any in-flight autonomous plan.
            self.pending.append(dict(id=event_id, uid=identity(uid), name=str(name)[:40], text=text, received=time.time()))
            self.epoch += 1
        return event_id

    def reply(self, item, text, kind, **extra):
        result = dict(comment=item, text=str(text)[:700], kind=kind, **extra)
        self.store.record('reply', result)
        with self.lock:
            self.replies.append(result)
            self.replies = self.replies[-40:]

    def next_reply(self, consumer):
        uuid.UUID(consumer)
        with self.lock:
            now=time.time()
            if self.consumer != consumer and self.consumer_until > now: return None
            self.consumer, self.consumer_until = consumer, now + 120
            if not self.replies: return None
            first = self.replies.pop(0)
            selected = take_same_viewer(self.replies, first, lambda r:r['comment'].get('uid'),limit=3)
            if len(selected)==1: return first
            return dict(comment=merge_comments([r['comment'] for r in selected]),
                        text=' '.join(r['text'] for r in selected),kind='merged',
                        replies=selected,mergedCount=len(selected))

    def queue_reply(self, item, method, args, kind, **extra):
        job = copy.deepcopy((item, method, args, kind, extra))
        try:
            self.reply_jobs.put_nowait(job)
        except queue.Full:
            self.reply(item, '回复队列有点满，详细说明暂时省略；操作结果请以记录为准。', 'busy')

    def process_reply(self):
        try:
            item, method, args, kind, extra = self.reply_jobs.get_nowait()
        except queue.Empty:
            return False
        jobs = [(item,method,args,kind,extra)]
        # Merge only jobs not yet generated. Game orders remain independently executed once.
        with self.reply_jobs.mutex:
            same = take_same_viewer(self.reply_jobs.queue, jobs[0], lambda j:j[0].get('uid'))
            jobs = same
        try:
            if len(jobs)>1:
                item = merge_comments([j[0] for j in jobs])
                kind = 'merged'
                extra = dict(results=[dict(kind=j[3],**j[4]) for j in jobs])
                text = self.model.bundle_reply([dict(comment=j[0],method=j[1],args=j[2],kind=j[3],extra=j[4]) for j in jobs])
            else:
                text = getattr(self.model, method)(*args)
            self.reply(item, text, kind, **extra)
        except Exception as exc:
            self.store.record('reply-failure', {'error': str(exc)[:240], 'viewer': item.get('id')})
            self.reply(item, '这条回复没生成好，不能据此判断游戏操作成功或失败。', 'reply-error', **extra)
        finally:
            for _ in jobs: self.reply_jobs.task_done()
        return True

    def tick(self):
        if not self.writer.acquire(blocking=False):
            return
        reading_initial = False
        try:
            with self.lock:
                if not self.enabled and not self.pending:
                    return
                if time.monotonic() < self.read_retry_at:
                    return
                epoch = self.epoch
                item = self.pending.pop(0) if self.pending else None
            if item and time.time() - item['received'] > 90:
                self.reply(item, '这条指令已经过时，我没有补执行。', 'expired')
                return
            if item:
                classified = request_model(self.model.classify, item['text'])
                claims=classified.get('knowledgeClaims',[])
                if isinstance(claims,list):
                    for claim in claims[:2]:
                        if isinstance(claim,dict) and claim.get('topic') in ('capture','deploy','garrison','defense','tech','other') and isinstance(claim.get('text'),str) and 1<=len(claim['text'])<=600:
                            knowledge_id=self.store.learning.knowledge.add(claim['text'],{'kind':'viewer','uid':item.get('uid'),'quote':item['text'],'topic':claim['topic']},item.get('id') or str(item['received']))
                            self.store.record('knowledge-candidate',{'id':knowledge_id,'viewer':item.get('id'),'session':self.session})
                kind = classified.get('kind')
                if kind != 'command':
                    # Chat and uncertain intent never call the game adapter.
                    recent = [e for e in self.store.events(20) if e['kind']=='step']
                    context = {'enabled':self.enabled, 'session':self.session,
                               'latestObservation':self.latest_observation, 'observedAt':self.latest_observed_at,
                               'observationAgeSeconds':time.time()-self.latest_observed_at if self.latest_observed_at else None,
                               'lastRecordedStep':recent[-1] if recent else None}
                    self.queue_reply(item, 'chat', (item['text'], kind == 'uncertain', context), kind or 'uncertain')
                    self.model_failures = 0
                    self.last_error = None
                    self.read_retry_at = 0
                    return
                if not self.enabled:
                    self.reply(item, '游戏操作已暂停，这条先不执行。', 'paused')
                    return
            reading_initial = True
            if self.rounds and self.enabled and self.rounds.poll(self):
                if item: self.reply(item,'当前对局已经结束，这条指令没有执行。','cancelled')
                return
            before = self.adapter.observe()
            reading_initial = False
            if self.read_failures:
                self.store.record('read-recovered', {'attempts': self.read_failures})
            self.read_retry_at = 0
            self.read_failures = 0
            self.last_error = None
            session = before.get('sessionId')
            if not session:
                raise RuntimeError('游戏尚未进入对局')
            if self.session and session != self.session:
                raise RuntimeError('对局已更换，请在控制台重新确认后继续')
            self.session = session
            if self.tactics.session is None:
                prior = next((e for e in reversed(self.store.events(30))
                              if e['kind']=='step' and e['data'].get('before',{}).get('sessionId') == session
                              and time.time()-e['created'] < 300), None)
                if prior:
                    saved = prior['data']['before']
                    self.tactics.update(saved)
                    alert = saved.get('tactics',{}).get('baseAlert')
                    if alert:
                        self.tactics.base_alert = copy.deepcopy(alert)
                        self.tactics.alert_serial = alert['serial']
            self.tactics.update(before)
            if item:
                self.tactics.advance = None
                self.tactics.camera_follow = []
                self.tactics.camera_hold = time.monotonic() + 12
            opening_plan = None  # Strategic choices belong to the model, including the opening.
            camera = None if item or opening_plan else self.tactics.camera(before)
            if camera:
                with self.lock:
                    if not self.enabled or self.epoch != epoch or self.pending: return
                    camera_receipt = self.adapter.execute('look', camera['params'], session)
                self.tactics.camera_at = time.monotonic()
                if not camera_receipt.get("uncertain") and camera_receipt.get("exitCode", 0) == 0:
                    self.tactics.alert_camera_serial = self.tactics.alert_serial
                self.store.record('camera', dict(plan=camera, receipt=camera_receipt, session=session))
                if camera_receipt.get('uncertain'): raise RuntimeError('镜头移动结果未知，停止后续操作')
                before = self.adapter.observe()
                if before.get('sessionId') != session: raise RuntimeError('镜头切换期间对局已改变')
                self.tactics.update(before)
            before = {**before, 'tactics': copy.deepcopy(self.tactics.context())}
            strategy = self.strategy.update(before,before['tactics'])
            before['strategy'] = {k:v for k,v in strategy.items() if k in ('session','enemies','counts','victory','evidenceBoundary')}
            before['decisionPolicy'] = 'model-led-v2'
            before['campaign'] = self.objectives.context(before)
            before['opportunities'] = self.opportunities.update(before)
            before['gameKnowledge'] = {'game':'yr','verifiedCases':self.store.learning.knowledge.context()['verifiedCases']}
            self.latest_observation = copy.deepcopy(before)
            self.latest_observed_at = time.time()
            intent = item['text'] if item else None
            trigger = 'viewer' if item else ('damage' if self.tactics.alert_serial > self.tactics.alert_reviewed else self.gate.reason(before))
            if self.skill and (self.skill['session'] != session or item or trigger == 'damage'):
                self.store.record('skill-interrupted', self.skill)
                self.skill = None
            local_plan = opening_plan
            if self.skill:
                pending = next((p for p in before.get('production', []) if p.get('type') == self.skill['type']), None)
                if pending and pending.get('total') is not None and pending.get('progress') == pending['total']:
                    local_plan = {'method':'place-building','params':{'type':self.skill['type'],'cells':self.skill['cells']}}
                    self.skill = None
                elif time.monotonic() > self.skill['deadline']:
                    self.store.record('skill-timeout', {'type': self.skill['type']})
                    self.skill = None
                    trigger = 'skill-timeout'
                else:
                    self.status = '等待建筑生产（本地监测）'
                    self.gate.skipped += 1
                    return
            opportunity_plan = None
            # Opportunities are observations, never unsolicited scout/capture orders.
            if not item and not local_plan and not trigger:
                self.status = '本地监测战况'
                return
            self.status = '处理观众指令' if item else '自主决策'
            start = time.monotonic()
            recent = [dict(id=e['id'], plan=e['data']['plan'],
                           receipt=receipt_summary(e['data']['receipt']),
                           errors=[{k:v for k,v in event.items() if k in ('error','message','reason','event')}
                                   for event in e['data']['receipt'].get('events',[]) if 'error' in event])
                      for e in self.store.events(30) if e['kind']=='step' and e['data'].get('before',{}).get('sessionId') == session][-4:]
            recalled = [] if local_plan else self.store.recall(before)
            for attempt in range(2):
                plan = None
                try:
                    plan = local_plan or request_model(self.model.plan, before, intent, recalled, recent)
                    method, params = validate_plan(plan)
                    if not local_plan and hasattr(self.model,'reflect') and not {'lessonIds','expectedOutcome'}.issubset(plan):
                        raise ValueError('Return explicit lessonIds and expectedOutcome; use [] when no lesson is adopted')
                    if not local_plan and hasattr(self.model,'reflect'):
                        self.objectives.validate(plan.get('objective'),before['campaign']['objective'],before['campaign']['progress'].get('evidenceIds',[]))
                    adopted = [] if local_plan else plan.get('lessonIds', [])
                    if not isinstance(adopted,list) or len(adopted)>3 or any(i not in {x['id'] for x in recalled} for i in adopted):
                        raise ValueError('Adopted lesson IDs must come from recalled evidence, at most three')
                    if not isinstance(plan.get('expectedOutcome',''),str) or len(plan.get('expectedOutcome',''))>600:
                        raise ValueError('Invalid expected outcome')
                    if adopted and not plan.get('expectedOutcome','').strip():
                        raise ValueError('Adoption requires an observable expected outcome')
                    tests = [] if local_plan else plan.get('lessonTests',[])
                    if not isinstance(tests,list) or len(tests)>3:
                        raise ValueError('At most three lesson tests are allowed')
                    if not local_plan:
                        plan = {**plan, 'placementPolicy': 'model-selected-v1'}
                    fields=event_summary(dict(id='pending',kind='step',created=0,data={'plan':plan}))['modelControlledFields']
                    if any(not isinstance(t,dict) or t.get('lessonId') not in adopted or t.get('field') not in fields or not isinstance(t.get('expect'),str) or not 1<=len(t['expect'])<=600 for t in tests):
                        raise ValueError('Register lessonTests with lessonId, a model-controlled field, and observable expect; execution is tool-controlled; legacy placement cells are tool-controlled')
                    if set(t['lessonId'] for t in tests)!=set(adopted) or len(tests)!=len(set(adopted)):
                        raise ValueError('Register exactly one lessonTest per adopted lesson BEFORE execution')
                    if method == 'build' and not any(e.get('type') == params.get('type') and e.get('foundation') for e in rows(before.get('sidebar'))):
                        raise ValueError('建筑当前不可生产，未提交；选择 sidebar 中带 foundation 的建筑，或改做其他合法动作')
                    executed_plan = self.tactics.begin(params, before) if method == 'advance' else plan
                    self.tactics.strategy_guard(plan, before, autonomous=item is None, model_led=True)
                    if method == 'camera-follow':
                        owned = {e['id'] for e in rows(before.get('owned',before.get('entities'))) if e.get('owner') == before.get('player') and e.get('cell')}
                        if any(i not in owned for i in params.get('units',[])):
                            raise ValueError('Camera follow requires currently owned units with known positions')
                    if method != 'camera-follow':
                        validate_command_visibility(executed_plan, before)
                    guard_targets(executed_plan, before)
                    self.tactics.check_repeat(executed_plan, before)
                    method, params = validate_plan(executed_plan)
                    break
                except ValueError as exc:
                    rejected = {'plan':plan, 'error':str(exc), 'source':'viewer' if item else 'autonomous',
                                'notSubmitted':True, 'session':session}
                    self.store.record('invalid-plan', rejected)
                    if attempt: raise UnsubmittedPlanError(str(exc)) from exc
                    # Only regenerate BEFORE submission. Never retry an uncertain game write.
                    recent = recent + [rejected]
            model_seconds = 0 if local_plan else time.monotonic() - start
            if self.model_failures:
                self.store.record('model-recovered', {'attempts': self.model_failures})
                self.model_failures = 0
            self.gate.mark(before)
            self.tactics.alert_reviewed = self.tactics.alert_serial
            with self.lock:
                if not self.enabled:
                    if item: self.pending.insert(0, item)
                    return
                if item is None and (epoch != self.epoch or self.pending):
                    self.store.record('discarded', {'reason':'viewer-priority', 'plan':plan})
                    return
                # Hold arbitration lock through submission; viewer arrivals wait only for this command.
                self.status = '执行观众指令' if item else '执行自主操作'
                if method == 'camera-follow':
                    receipt = self.tactics.configure_camera(params, before)
                elif method == 'check-placement':
                    receipt = self.placement.check(before, params['type'], params['cells'])
                elif method == 'build':
                    available = next((e for e in rows(before.get('sidebar')) if e.get('type') == params.get('type') and e.get('foundation')), None)
                    if not available: raise UnsubmittedPlanError('建筑当前不可生产，未提交')
                    if any(p.get('type') == params['type'] for p in before.get('production', [])):
                        receipt = {'uncertain':False, 'exitCode':0, 'status':'接续已存在的生产，没有重复购买'}
                    else:
                        receipt = self.adapter.execute('act', {'action':'produce','type':params['type']}, session)
                else:
                    receipt = self.adapter.execute(method, params, session)
            self.tactics.record(executed_plan, before)
            after = self.adapter.observe()
            if after.get('sessionId') != session:
                raise RuntimeError('执行期间对局发生变化，结果待核对')
            self.tactics.accepted(plan, executed_plan, after, receipt)
            self.opportunities.accepted(plan, after, receipt, local=bool(opportunity_plan))
            if method == 'build' and not receipt.get('uncertain') and receipt.get('exitCode', 0) == 0:
                if any(p.get('type') == params['type'] for p in after.get('production', [])):
                    self.skill = dict(session=session, type=params['type'], cells=params['cells'], deadline=time.monotonic()+120)
            event_id = self.store.record('step', dict(source='viewer' if item else 'autonomous',
                viewer=item, before=before, plan=plan, receipt=receipt, after=after,
                modelSeconds=round(model_seconds,3), trigger=trigger, localSkill=bool(local_plan),
                recalledLessonIds=[x['id'] for x in recalled],adoptedLessonIds=list(dict.fromkeys(adopted)),
                expectedOutcome=plan.get('expectedOutcome'),lessonTests=tests))
            if not local_plan and plan.get('objective') and not receipt.get('uncertain'):
                self.objectives.accept(plan['objective'],before['campaign']['objective'],session,event_id)
            if receipt.get('uncertain'):
                raise RuntimeError('操作结果不确定，已暂停，禁止自动重试')
            if item:
                # Immutable evidence goes to a separate worker; never hold up the next order.
                self.queue_reply(item, 'report', (intent, plan, receipt, before, after), 'command', evidence=event_id)
            self.steps_since_review += 1
            if self.steps_since_review >= 12 and not self.pending:
                self.status = '整理刚才的经验'
                try:
                    self._review_steps()
                    self.steps_since_review = 0
                except Exception as exc:
                    self.store.record('review-failure', {'error':str(exc)[:240]})
                    self.steps_since_review = 0
            self.status = '等待下一步'
        except Exception as exc:
            if isinstance(exc, ModelUnavailable):
                with self.lock:
                    if self.epoch == epoch:
                        self.model_failures += 1
                        delay = min(60, 5 * 2 ** min(self.model_failures - 1, 4))
                        self.read_retry_at = time.monotonic() + delay
                        self.last_error = str(exc)
                        self.status = f'模型连接中断，{delay} 秒后重新观察并重试'
                        self.gate.previous = None
                        # Retain the original timestamp: expired viewer requests must not execute later.
                        if item: self.pending.insert(0, item)
                        self.store.record('model-retry', {'delay': delay, 'notSubmitted': True,
                                                         'viewerId': item.get('id') if item else None})
                return
            if isinstance(exc, UnsubmittedPlanError):
                with self.lock:
                    if self.enabled and self.epoch == epoch:
                        self.read_retry_at = time.monotonic() + 3
                        self.last_error = str(exc)
                        self.status = '计划校验未通过，3 秒后重新观察决策'
                        self.gate.previous = None
                        self.store.record('plan-retry', {'error': str(exc), 'notSubmitted': True})
                        if item: self.reply(item, '刚才的操作方案没通过检查，没有执行。我会重新观察，这条指令请稍后再发。', 'rejected')
                return
            # Only retry failed pre-action reads. Post-action uncertainty still pauses.
            if reading_initial and str(exc) in ('无法读取当前战况', '无法确认原生对局状态'):
                with self.lock:
                    if self.enabled and self.epoch == epoch:
                        self.read_failures += 1
                        delay = min(60, 5 * 2 ** min(self.read_failures - 1, 4))
                        self.read_retry_at = time.monotonic() + delay
                        self.last_error = str(exc)
                        self.status = f'战况连接中断，{delay} 秒后自动重连'
                        self.store.record('read-retry', {'error': str(exc), 'delay': delay})
                        if item:
                            self.reply(item, '刚才没读到战况，这条还没有执行。我正在重连，恢复后你可以再指挥我。', 'connection')
                return
            if self.rounds and self.enabled:
                try:
                    if self.rounds.poll(self): return
                except Exception:
                    pass
            self.last_error = str(exc)[:240]
            self.set_enabled(False)
            self.store.record('failure', {'error':self.last_error})
            if 'item' in locals() and item:
                self.reply(item, '这一步没有完成，我已经暂停操作，不能说执行成功。', 'error')
        finally:
            self.writer.release()

    def review(self):
        if not self.writer.acquire(blocking=False):
            raise ValueError('正在执行操作，请稍后复盘')
        try:
            if self.enabled:
                raise ValueError('先暂停游戏操作再复盘')
            return self._review_steps()
        finally:
            self.writer.release()

    def _review_steps(self):
        if hasattr(self.model, 'reflect'):
            self.review_jobs.enqueue(self.session)
            return []
        steps = [e for e in self.store.events(60) if e['kind']=='step'
                 and (not self.session or e['data'].get('before', {}).get('sessionId') == self.session)][-12:]
        if not steps: raise ValueError('还没有可复盘的实机记录')
        allowed = {e['id'] for e in steps}
        lessons = self.model.review(steps)
        ids = []
        for lesson in lessons[:5]:
            evidence = lesson.get('evidence', [])
            if not isinstance(evidence, list) or not evidence or any(e not in allowed for e in evidence):
                raise ValueError('复盘引用了本次记录以外的证据')
            ids.append(self.store.learn(lesson['text'], evidence))
        self.store.record('review', {'lessons':ids})
        return ids

    def start(self):
        def replies():
            while not self.stop_event.is_set():
                if not self.process_reply(): self.stop_event.wait(.1)
        def loop():
            while not self.stop_event.wait(1):
                self.tick()
        def reviews():
            while not self.stop_event.wait(2):
                try:
                    self.review_jobs.run_one()
                except Exception as exc:
                    self.store.record('reflection-worker-error', {'error':str(exc)[:240]})
                    self.stop_event.wait(15)
        if self.review_jobs:
            threading.Thread(target=reviews, daemon=True, name='game-reflections').start()
        threading.Thread(target=loop, daemon=True, name='game-director').start()
        threading.Thread(target=replies, daemon=True, name='game-replies').start()
