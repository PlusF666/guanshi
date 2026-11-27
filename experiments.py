"""Forward-only comparisons of the same timely confirmed candidates."""
import datetime as dt
import json
from paper import Account, live

GROUPS = ("发现顺序", "量价排序", "排序加公告筛选")


class Experiments:
    def __init__(self, root, db):
        self.root, self.db = root, db
        self.accounts = {g: Account(root / ("实验_" + g + ".sqlite3")) for g in GROUPS}
        db.executescript("""
        CREATE TABLE IF NOT EXISTS candidates(day,hm,sym,found,volume,breakout,spread,evidence,risk,PRIMARY KEY(day,hm,sym));
        CREATE TABLE IF NOT EXISTS choices(bucket,groupname,day,sym,reason,PRIMARY KEY(bucket,groupname));
        CREATE TABLE IF NOT EXISTS experiment_marks(day,groupname,equity,peak,dd,PRIMARY KEY(day,groupname));
        """)
        self.version = None
        self.feed = {}

    def evidence(self, sym, now):
        path = self.root / '公司情报.js'
        if not path.exists():path = self.root / '研究台' / path.name
        try:
            version = path.stat().st_mtime_ns
            if self.version != version:
                self.feed = json.loads(path.read_text('utf8').split('=',1)[1].rstrip(';\n '))
                self.version = version
        except (OSError, ValueError, IndexError):
            return [], False
        records = []
        risk = False
        for kind, part in self.feed.get(sym[2:], {}).items():
            for pub,title,summary,url in part.get('rows', []):
                seen = part.get('seen',{}).get('|'.join((pub,title,url)), '')
                try:
                    first = dt.datetime.fromisoformat(seen)
                    published = dt.datetime.fromisoformat(pub)
                    if first.tzinfo is None or first>now:continue
                    if published.tzinfo is None:
                        published=published.replace(tzinfo=now.tzinfo)
                        if len(pub)==10:published+=dt.timedelta(days=1)
                    if not 0 <= (now-published).total_seconds() <= 30*86400:continue
                except ValueError:continue
                records.append([kind,pub,seen,title,url])
                if kind=='公告' and '预亏' in title and '更正' not in title:risk=True
        return records,risk

    def candidate(self, day, hm, sym, found, volume, breakout, quote):
        if not live(found) or found.strftime('%Y%m%d')!=day:return
        end=found.replace(hour=int(hm[:2]),minute=int(hm[2:]),second=0,microsecond=0)+dt.timedelta(minutes=1)
        if not 0 <= (found-end).total_seconds() <= 90:return
        spread=None
        if quote and quote[4]>0 and quote[6]>=quote[4] and 0<=(found-quote[2]).total_seconds()<=60:
            spread=(quote[6]-quote[4])/quote[4]
        evidence,risk=self.evidence(sym,found)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?,?,?,?,?)",(day,hm,sym,found.isoformat(),volume,breakout,spread,json.dumps(evidence,ensure_ascii=False),int(risk)))

    def scan(self, quotes, now):
        day=now.strftime('%Y%m%d')
        lower=(now-dt.timedelta(seconds=90)).isoformat()
        minute=now.replace(second=0,microsecond=0).isoformat()
        rows=self.db.execute("SELECT hm,sym,found,volume,breakout,spread,risk FROM candidates WHERE day=? AND found>=? AND found<? ORDER BY found,sym",(day,lower,minute)).fetchall()
        batches={}
        for r in rows:batches.setdefault(r[2][:16],[]).append(r)
        for group,a in self.accounts.items():
            for bucket,batch in batches.items():
                if self.db.execute("SELECT 1 FROM choices WHERE bucket=? AND groupname=?",(bucket,group)).fetchone():continue
                eligible=[r for r in batch if r[5] is not None and (group!=GROUPS[2] or not r[6])]
                if group!=GROUPS[0]:eligible.sort(key=lambda r:(-r[3],-r[4],r[5],r[1]))
                sym=None;reason="无符合条件的候选"
                if eligible:
                    hm,sym,*_=eligible[0]
                    state=a.state()
                    a.signal(day,hm,sym,now)
                    order=a.state()['pending']
                    reason="候单" if order and order['time']==now.isoformat() else "持仓或候单占用" if state['position'] or state['pending'] else "超出有效期"
                with self.db:self.db.execute("INSERT OR IGNORE INTO choices VALUES(?,?,?,?,?)",(bucket,group,day,sym,reason))
            a.scan(quotes,now)
            s=a.state();p=s['position']
            eq=s['cash']+(p['mark']*p['shares'] if p else 0)
            prev=self.db.execute("SELECT peak,dd FROM experiment_marks WHERE groupname=? ORDER BY day DESC LIMIT 1",(group,)).fetchone() or (500000,0)
            peak=max(eq,prev[0])
            with self.db:self.db.execute("INSERT OR REPLACE INTO experiment_marks VALUES(?,?,?,?,?)",(day,group,eq,peak,max(prev[1],1-eq/peak)))

    def summary(self,day):
        lines=[]
        for g,a in self.accounts.items():
            mark=self.db.execute("SELECT equity,dd FROM experiment_marks WHERE day=? AND groupname=?",(day,g)).fetchone()
            if mark:
                iso=f'{day[:4]}-{day[4:6]}-{day[6:]}'
                n=a.db.execute("SELECT count(*) FROM fills WHERE substr(stamp,1,10)=?",(iso,)).fetchone()[0]
                lines.append(f"{g}  当日成交 {n} 笔，累计盈亏 {(mark[0]-500000)/100:+.2f} 元，最大回撤 {mark[1]:.2%}")
        ranked=self.accounts[GROUPS[1]]
        avoided=missed=0
        opening=None
        for stamp,sym,side,price,qty,fee,detected in ranked.db.execute("SELECT stamp,symbol,side,price,shares,fee,detected FROM fills WHERE replace(substr(stamp,1,10),'-','')<=? ORDER BY id",(day,)):
            if side=="买入":
                decision=self.db.execute("SELECT bucket FROM choices WHERE groupname=? AND sym=? AND day=? AND reason='候单' AND bucket<=? ORDER BY bucket DESC LIMIT 1",(GROUPS[1],sym,stamp[:10].replace('-',''),detected[:16])).fetchone()
                blocked=decision and self.db.execute("SELECT max(risk) FROM candidates WHERE sym=? AND substr(found,1,16)=?",(sym,decision[0])).fetchone()[0]
                opening=(price*qty+fee,blocked)
            elif opening:
                if opening[1]:
                    pnl=price*qty-fee-opening[0]
                    avoided+=pnl<0
                    missed+=pnl>0
                opening=None
        if lines:lines.append(f"未筛选排序组已平仓预亏样本：亏损 {avoided} 笔 / 盈利 {missed} 笔")
        return '　|　'.join(lines) or '该日没有前向实验记录'
