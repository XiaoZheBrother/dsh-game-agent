"""Native-result-driven round lifecycle, with durable once-only restart claims."""
import json
import time


class GameRounds:
    def __init__(self, store, adapter, clock=time.time):
        self.store, self.adapter, self.clock = store, adapter, clock
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS game_rounds (session TEXT PRIMARY KEY, phase TEXT, result TEXT, due REAL, next_session TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS game_settings (key TEXT PRIMARY KEY, value TEXT)')

    def enabled(self):
        with self.store.connect() as db:
            row=db.execute("SELECT value FROM game_settings WHERE key='auto_next'").fetchone()
        return bool(row and row[0]=='true')

    def configure(self, enabled):
        if type(enabled) is not bool: raise ValueError('Boolean required')
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO game_settings VALUES ('auto_next',?)", (json.dumps(enabled),))

    def poll(self, director):
        status=self.adapter.native_status()
        runtime=status.get('runtime') or {}
        battlefield=runtime.get('battlefield') or {}
        with self.store.connect() as db:
            pending=db.execute("SELECT session,phase,due FROM game_rounds WHERE phase IN ('ended','waiting','requested','failed') ORDER BY due LIMIT 1").fetchone()
        if pending and pending[1] in ('requested','failed'):
            session= battlefield.get('sessionId')
            if battlefield.get('ready') and session and session!=pending[0]:
                with self.store.connect() as db:
                    db.execute("UPDATE game_rounds SET phase='running',next_session=? WHERE session=?",(session,pending[0]))
                director.session=session; director.last_error=None
                self.store.record('next-round-ready',{'previous':pending[0],'session':session})
                return False
            if pending[1]=='failed':
                # A user may be importing resources or loading a manual replacement.
                # Keep observing readiness; never replay the old restart command.
                director.status='等待恢复后的新对局就绪（不会重复开局）'
                return True
            if status.get('failure') or self.clock()>pending[2]+180:
                detail=dict(session=pending[0],elapsedSeconds=round(self.clock()-pending[2],2),
                    failure=status.get('failure'),pageId=status.get('pageId'),phase=status.get('phase'),
                    starting=status.get('starting'),hasResources=status.get('hasResources'),
                    hasRuntime=status.get('hasRuntime'),runtimePhase=runtime.get('phase'),
                    runtimePage=runtime.get('page'),battlefield=battlefield)
                with self.store.connect() as db:
                    db.execute("UPDATE game_rounds SET phase='failed' WHERE session=? AND phase='requested'",(pending[0],))
                self.store.record('next-round-failed',detail)
                reason=str(status.get('failure') or '180 秒内未确认新战场')[:500]
                raise RuntimeError('下一局启动失败：'+reason+'；已保存诊断，不会重复开局')
            director.status='等待下一局载入'; return True
        result=runtime.get('battleResult') or {}
        if result.get('source')!='native-game-loop' or result.get('outcome') not in ('victory','defeat'):
            return False
        session=result.get('sessionId')
        if not session or battlefield.get('ready'): return False
        if director.session and director.session!=session:
            raise RuntimeError('结束结果不属于当前对局')
        director.session=session
        with self.store.connect() as db:
            inserted=db.execute("INSERT OR IGNORE INTO game_rounds VALUES (?,'ended',?,?,NULL)",
                                (session,json.dumps(result),self.clock()+15)).rowcount
            phase=db.execute('SELECT phase FROM game_rounds WHERE session=?',(session,)).fetchone()[0]
        if inserted:
            self.store.record('native-game-end',result,event_id='native-end-'+session)
            with director.lock:
                for item in director.pending: director.reply(item,'这局已经结束，这条指令没有带到下一局。','cancelled')
                director.pending.clear()
            director.status='本局结束，整理经验'
            try: director._review_steps()
            except Exception as exc: self.store.record('review-failure',{'error':str(exc)[:240],'session':session})
            with self.store.connect() as db:
                db.execute("UPDATE game_rounds SET phase='waiting',due=? WHERE session=?",(self.clock()+15,session))
            return True
        if not self.enabled():
            director.set_enabled(False); director.status='本局结束（自动续局关闭）'; return True
        if phase not in ('ended','waiting'): return True
        if 'replay-native-settings-v1' not in status.get('features',[]):
            raise RuntimeError('当前游戏页面尚未支持保留速度的续局，请使用本地适配版')
        with self.store.connect() as db:
            due=db.execute('SELECT due FROM game_rounds WHERE session=?',(session,)).fetchone()[0]
        if self.clock()<due:
            director.status='本局已结束，准备下一局'; return True
        with director.lock:
            if not director.enabled or not self.enabled(): return True
            with self.store.connect() as db:
                claimed=db.execute("UPDATE game_rounds SET phase='requested',due=? WHERE session=? AND phase IN ('ended','waiting')",(self.clock(),session)).rowcount
            if claimed:
                # Persist BEFORE sending; ambiguous replies are observed, never blindly resent.
                self.store.record('next-round-requested',{'session':session})
                try:
                    receipt=self.adapter.next_battle(status, session, 'next-'+session)
                    self.store.record('next-round-submitted',dict(session=session,pageId=status.get('pageId'),receipt=receipt))
                except Exception as exc: self.store.record('next-round-uncertain',{'session':session,'error':str(exc)[:240]})
        director.status='等待下一局载入'; return True
