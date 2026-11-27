"""Forward-only paired paper accounts and a durable daily review."""
import datetime as dt
import sqlite3
from experiments import Experiments
from paper import Account, live
from 全市场盯盘 import CN

RULES={"连续确认":"现行 · 连续确认","分钟突破":"候选 · 首次突破"}
class Review:
    def __init__(self,root):
        self.db=sqlite3.connect(root/"观市每日复盘.sqlite3",timeout=5)
        self.accounts={rule:Account(root/("对照_"+rule+".sqlite3")) for rule in RULES}
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS coverage(day TEXT,hm TEXT,quotes INTEGER DEFAULT 0,PRIMARY KEY(day,hm));
        CREATE TABLE IF NOT EXISTS signals(day TEXT,hm TEXT,sym TEXT,rule TEXT,found TEXT,delay REAL,outcome TEXT,
          PRIMARY KEY(day,hm,sym,rule));
        CREATE TABLE IF NOT EXISTS actual(found TEXT,sym TEXT,outcome TEXT,PRIMARY KEY(found,sym));
        CREATE TABLE IF NOT EXISTS marks(day TEXT,rule TEXT,equity INTEGER,peak INTEGER,drawdown REAL,
          PRIMARY KEY(day,rule));
        """)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS progress(day PRIMARY KEY,total,done,success);
        CREATE TABLE IF NOT EXISTS checks(day,sym,stamp,ok,PRIMARY KEY(day,sym,stamp));
        CREATE INDEX IF NOT EXISTS signal_lookup ON signals(day,sym,hm);
        CREATE TABLE IF NOT EXISTS account_actions(profile,found,sym,outcome,PRIMARY KEY(profile,found,sym));
        """)
        self.experiments=Experiments(root,self.db)
        self.profile=1
        self.minute=None
    def heartbeat(self,now,quotes=False):
        day,hm=now.strftime("%Y%m%d"),now.strftime("%H%M")
        if now.weekday()>=5 or not ("0930"<=hm<"1130" or "1300"<=hm<"1500"):return
        if self.minute==(day,hm) and not quotes:return
        self.minute=(day,hm)
        with self.db:
            self.db.execute("INSERT INTO coverage VALUES(?,?,?) ON CONFLICT(day,hm) DO UPDATE SET quotes=max(quotes,excluded.quotes)",(day,hm,int(quotes)))
    def signal(self,day,hm,sym,rule,found,new):
        end=dt.datetime.strptime(day+hm,"%Y%m%d%H%M").replace(tzinfo=CN)+dt.timedelta(minutes=1)
        lag=(found-end).total_seconds()
        account=self.accounts[rule]
        with self.db:
            args=(day,hm,sym,rule)
            if self.db.execute("SELECT 1 FROM signals WHERE day=? AND hm=? AND sym=? AND rule=?",args).fetchone():return
            if not new:reason="旧记录：首次发现未知"
            elif not live(found) or lag>90 or lag<0:reason="盘后或超时发现"
            else:
                s=account.state()
                if s["position"]:reason="已有持仓"
                elif s["pending"]:reason="已有候单"
                else:
                    account.signal(day,hm,sym,found)
                    reason="候单" if account.state()["pending"] else "未接受"
            self.db.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?)",(*args,found.isoformat() if new else None,lag if new else None,reason))
    def scan(self,data,now,fresh):
        self.heartbeat(now,bool(fresh))
        self.experiments.scan(data,now)
        day=now.strftime("%Y%m%d")
        with self.db:
            for rule,a in self.accounts.items():
                before=a.state();a.scan(data,now);after=a.state()
                order=before["pending"]
                if order and order["side"]=="买入" and not after["pending"]:
                    outcome="已模拟成交" if after["position"] else after["note"]
                    self.db.execute("UPDATE signals SET outcome=? WHERE rule=? AND found=? AND sym=?",(outcome,rule,order["time"],order["symbol"]))
                pos=after["position"]
                eq=after["cash"]+(pos["shares"]*pos["mark"] if pos else 0)
                prev=self.db.execute("SELECT peak,drawdown FROM marks WHERE rule=? AND day<=? ORDER BY day DESC LIMIT 1",(rule,day)).fetchone() or (500000,0)
                peak=max(eq,prev[0]);dd=max(prev[1],1-eq/peak)
                self.db.execute("INSERT OR REPLACE INTO marks VALUES(?,?,?,?,?)",(day,rule,eq,peak,dd))
    def primary_signal(self,account,args):
        day,hm,sym,found=args
        key=(found.isoformat(),sym)
        state=account.state()
        account.signal(*args)
        pending=account.state()["pending"]
        reason="候单" if pending and pending["time"]==key[0] else "已有持仓" if state["position"] else "已有候单" if state["pending"] else "未接受"
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO account_actions VALUES(?,?,?,?)",(self.profile,*key,reason))
            self.db.execute("INSERT OR IGNORE INTO actual VALUES(?,?,?)",(*key,reason))
    def primary_scan(self,account,data,now):
        pending=account.state()["pending"]
        account.scan(data,now)
        if pending and pending["side"]=="买入":
            state=account.state()
            if not state["pending"]:
                with self.db:
                    self.db.execute("UPDATE account_actions SET outcome=? WHERE profile=? AND found=? AND sym=?",("已模拟成交" if state["position"] else state["note"],self.profile,pending["time"],pending["symbol"]))
                    self.db.execute("UPDATE actual SET outcome=? WHERE found=? AND sym=?",("已模拟成交" if state["position"] else state["note"],pending["time"],pending["symbol"]))
    def report(self,day,category="全部"):
        coverage=self.db.execute("SELECT count(*),coalesce(sum(quotes),0) FROM coverage WHERE day=?",(day,)).fetchone()
        stats=[]
        for rule,name in RULES.items():
            total,timely,bought=self.db.execute("SELECT count(*),count(CASE WHEN outcome NOT IN ('旧记录：首次发现未知','盘后或超时发现') THEN 1 END),count(CASE WHEN outcome='已模拟成交' THEN 1 END) FROM signals WHERE day=? AND rule=?",(day,rule)).fetchone()
            mark=self.db.execute("SELECT equity,drawdown FROM marks WHERE day=? AND rule=?",(day,rule)).fetchone()
            stats.append((name,str(total),str(timely),str(bought),f"{100*bought/timely:.1f}%" if timely else "—",f"{(mark[0]-500000)/100:+.2f}" if mark else "—",f"{100*mark[1]:.2f}%" if mark else "—"))
        clause={"全部":"","本账户已成交":" AND a.outcome='已模拟成交'","盘中未成交":" AND s.found IS NOT NULL AND s.delay BETWEEN 0 AND 90 AND s.outcome!='盘后或超时发现' AND coalesce(a.outcome,'')!='已模拟成交'","盘后或超时":" AND s.outcome='盘后或超时发现'"}[category]
        rows=self.db.execute("SELECT s.hm,s.sym,s.rule,s.found,s.delay,s.outcome,a.outcome FROM signals s LEFT JOIN account_actions a ON a.profile=? AND s.found=a.found AND s.sym=a.sym AND s.rule='连续确认' AND s.outcome NOT IN ('旧记录：首次发现未知','盘后或超时发现') WHERE s.day=?"+clause+" ORDER BY s.hm DESC,s.sym LIMIT 500",(self.profile,day))
        detail=[(hm,sym,rule,t[11:19] if t else "未知",f"{delay:.0f}" if delay is not None else "—",reason,actual or (reason if rule=="连续确认" and reason in ("盘后或超时发现","旧记录：首次发现未知") else "—")) for hm,sym,rule,t,delay,reason,actual in rows]
        return coverage,stats,detail
