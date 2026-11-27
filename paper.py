"""Local forward-only paper account; money and prices in integer fen."""
import datetime as dt
import json
import sqlite3
from 全市场盯盘 import main_board
from decimal import Decimal, ROUND_HALF_UP

def fen(price):
    return int((Decimal(str(price))*100).quantize(Decimal("1"),rounding=ROUND_HALF_UP))

def commission(gross):
    return max(500,(gross+1000)//2000)

def live(t):
    return t.weekday()<5 and ("0930"<=t.strftime("%H%M")<"1130" or "1300"<=t.strftime("%H%M")<"1457")

class Account:
    def __init__(self,path,capital=500000):
        if type(capital) is not int or capital<=0:raise ValueError("本金必须为正整数分")
        self.db=sqlite3.connect(path,timeout=3)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS paper(id INTEGER PRIMARY KEY,state TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,stamp TEXT,symbol TEXT,side TEXT,price INTEGER,shares INTEGER,fee INTEGER,reason TEXT,detected TEXT)")
            self.db.execute("INSERT OR IGNORE INTO paper VALUES(1,?)",(json.dumps(dict(capital=capital,cash=capital,position=None,pending=None,last='',note='等待盘中连续确认')),))
    def state(self):
        return json.loads(self.db.execute("SELECT state FROM paper WHERE id=1").fetchone()[0])
    def update(self,fn):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            s=self.state()
            if fn(s):self.db.execute("UPDATE paper SET state=? WHERE id=1",(json.dumps(s,ensure_ascii=False),))
    def signal(self,day,hm,symbol,found):
        if not main_board(symbol):return
        if not live(found) or day!=found.strftime("%Y%m%d"):return
        end=found.replace(hour=int(hm[:2]),minute=int(hm[2:]),second=0,microsecond=0)+dt.timedelta(minutes=1)
        if not 0<=(found-end).total_seconds()<=90:return
        key=day+hm+symbol
        def stage(s):
            if s["position"] or s["pending"] or s["last"]==key:return False
            s.update(last=key,pending=dict(side="买入",symbol=symbol,time=found.isoformat(),reason=hm+" 连续确认"),note="等待后续卖一报价")
            return True
        self.update(stage)
    def scan(self,quotes,now):
        def step(s):
            pos=s["position"];order=s["pending"]
            sym=(pos or order or {}).get("symbol")
            if not sym:return False
            if not main_board(sym):
                if order:
                    s.update(pending=None,note="交易范围已限定沪深主板，原候单取消")
                    return True
                return False
            q=quotes.get(sym)
            if order and order["side"]=="买入" and (now-dt.datetime.fromisoformat(order["time"])).total_seconds()>90:
                s.update(pending=None,note="买入候单超时");return True
            if not q:return False
            last,_,stamp,vol,bid,bids,ask,asks=q
            if pos and stamp.isoformat()>=pos["marked"]:
                pos.update(mark=fen(last),marked=stamp.isoformat())
            if not(live(now) and live(stamp) and vol and 0<=(now-stamp).total_seconds()<=60):return bool(pos)
            if pos and not order and stamp.date().isoformat()>pos["day"]:
                change=fen(last)/pos["price"]-1
                reason="回撤止损" if change<=-.03 else "达到止盈" if change>=.06 else "次日尾盘退出" if stamp.strftime("%H%M")>="1450" else ""
                if reason:s["pending"]=dict(side="卖出",symbol=sym,time=now.isoformat(),reason=reason)
                return True
            if not order or stamp<=dt.datetime.fromisoformat(order["time"]):return bool(pos)
            buy=order["side"]=="买入"
            price=fen(ask if buy else bid)
            if price<=0:return bool(pos)
            qty=(min(s["cash"]//price,int(asks)*100)//100)*100 if buy else pos["shares"]
            if buy:
                while qty and qty*price+commission(qty*price)>s["cash"]:qty-=100
                if qty<(200 if sym.startswith("sh688") else 100):
                    if int(asks)>0:s.update(pending=None,note="可用资金或盘口数量不足");return True
                    return False
            elif bids*100<qty:return True
            gross=qty*price;fee=commission(gross)+(0 if buy else (gross+1000)//2000)
            s["cash"]+=(-gross if buy else gross)-fee
            s["position"]=dict(symbol=sym,shares=qty,price=price,day=stamp.date().isoformat(),mark=fen(last),marked=stamp.isoformat()) if buy else None
            s.update(pending=None,note=order["reason"])
            self.db.execute("INSERT INTO fills(stamp,symbol,side,price,shares,fee,reason,detected) VALUES(?,?,?,?,?,?,?,?)",(stamp.isoformat(),sym,order["side"],price,qty,fee,order["reason"],order["time"]))
            return True
        self.update(step)
