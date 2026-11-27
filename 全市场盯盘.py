"""沪深北 A 股全市场行情与开盘区间突破监测器（Python 标准库）。"""
import concurrent.futures as cf
import datetime as dt
import json
import queue
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.request import Request, urlopen

CN = dt.timezone(dt.timedelta(hours=8))
ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
LIST_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
QUOTE_URL = "https://qt.gtimg.cn/q="
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/day/query?code="
BATCH = 120

def read(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=8) as response:
        return response.read()

def main_board(symbol):
    if not re.fullmatch(r"(sh|sz)[0-9]{6}",symbol):
        return False
    code=symbol[2:]
    if symbol.startswith("sh"):
        return code.startswith(("600","601","603","605"))
    return "000001"<=code<="000999" or "001200"<=code<="004999"

def universe():
    result = {}
    for item in json.loads(read(LIST_URL))["stockList"]:
        code = item["code"]
        if item["category"] != "A股" or not re.fullmatch(r"\d{6}", code):
            continue
        market = {"gfsh":"sh", "gssh":"sh", "gssz":"sz", "gfsz":"sz", "gfbj":"bj", "gsbj":"bj"}.get(item["orgId"][:4])
        if not market:
            market = "sh" if code.startswith("6") else "sz" if code.startswith(("0","3")) else "bj" if code.startswith(("4","8","92")) else None
        if not market or not main_board(market+code):
            continue
        if market:
            result[market+code] = item["zwjc"]
    if not all(any(key.startswith(m) for key in result) for m in ("sh","sz")):
        raise ValueError("名录未覆盖沪深两市主板")
    return result

def quotes(symbols,book=False):
    raw=read(QUOTE_URL+",".join(symbols)).decode("gb18030", errors="replace")
    out={}
    for symbol, body in re.findall(r'v_([a-z]{2}\d{6})="([^"]*)"', raw):
        f=body.split("~")
        try:
            if symbol not in symbols or f[2]!=symbol[2:] or float(f[3])<=0:
                continue
            stamp=dt.datetime.strptime(f[30],"%Y%m%d%H%M%S").replace(tzinfo=CN)
            quote=(float(f[3]),float(f[32]),stamp,int(float(f[6])))
            if book:quote+=(float(f[9] or 0),int(float(f[10] or 0)),float(f[19] or 0),int(float(f[20] or 0)))
            out[symbol]=quote
        except (IndexError,ValueError):
            continue
    return out

def session(now):
    hm=now.strftime("%H%M")
    return now.weekday()<5 and ("0930"<=hm<="1130" or "1300"<=hm<="1500")

def minute_data(symbol):
    obj=json.loads(read(MINUTE_URL+symbol))
    return obj["data"][symbol]["data"]

def analyze(days, stamp):
    date=stamp.strftime("%Y%m%d")
    maps={}
    for day in days:
        if day["date"]>date:
            continue
        bars={}
        for line in day["data"]:
            t,p,v,a,*_=line.split()
            if "0930"<=t<="1130" or "1300"<=t<="1500":
                bars[t]=(float(p),float(v),float(a))
        maps[day["date"]]=bars
    bars=maps.get(date,{})
    opening=[f"09{m}" for m in range(30,45)]
    if any(t not in bars for t in opening) or stamp.strftime("%H%M")<"0945":
        return None,[]
    high=max(bars[t][0] for t in opening)
    past=sorted((d for d in maps if d<date),reverse=True)[:2]
    events=[]
    previous=None
    active=False
    confirmed=False
    for t,(p,v,a) in sorted(bars.items()):
        if t<"0945" or t>=stamp.strftime("%H%M") or not v:
            continue
        above=p>high and p>a/v/100
        minute=int(t[:2])*60+int(t[2:])
        adjacent=previous==minute-1
        if above and not (active and adjacent):
            events.append((t,"分钟突破",p))
            confirmed=False
        volumes=[maps[d][t][1] for d in past if t in maps[d]]
        if above and active and adjacent and not confirmed and len(volumes)==2 and sum(volumes)>0 and v>sum(volumes)/2:
            events.append((t,"连续确认",p))
            confirmed=True
        active=above
        previous=minute
    return high,events

def run(stop, updates,book=False):
    with sqlite3.connect(ROOT/"全市场盯盘记录.sqlite3") as db, cf.ThreadPoolExecutor(max_workers=5) as pool:
        db.execute("CREATE TABLE IF NOT EXISTS events(day TEXT, symbol TEXT, hm TEXT, kind TEXT, price REAL, PRIMARY KEY(day,symbol,hm,kind))")
        names=universe()
        symbols=sorted(names)
        batches=[symbols[i:i+BATCH] for i in range(0,len(symbols),BATCH)]
        history=deque()
        levels={}
        above={}
        day=""
        reported=set()
        def log(symbol,hm,kind,price,metrics=None):
            cur=db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)",(day,symbol,hm,kind,price))
            first_found=dt.datetime.now(CN)
            key=(symbol,hm,kind)
            if book and kind in ("分钟突破","连续确认") and key not in reported:
                reported.add(key)
                if metrics and cur.rowcount and kind=="连续确认":updates.put(("candidate",(day,hm,symbol,first_found,*metrics)))
                updates.put(("review_signal",(day,hm,symbol,kind,first_found,bool(cur.rowcount))))
            if book and kind=="连续确认":
                found=first_found
                if session(found):
                    end=found.replace(hour=int(hm[:2]),minute=int(hm[2:]),second=0,microsecond=0)+dt.timedelta(minutes=1)
                    if 0<=(found-end).total_seconds()<=90:updates.put(("paper_signal",(day,hm,symbol,found)))
            if cur.rowcount:
                updates.put(("event",(day,hm,symbol,names[symbol],kind,price)))
        def inspect(symbol,days,now):
            level,events=analyze(days,now)
            if level is not None:levels[symbol]=level
            if book:updates.put(("checked",(day,symbol,dt.datetime.now(CN).isoformat(),int(level is not None))))
            prior=sorted((d for d in days if d["date"]<day),key=lambda d:d["date"],reverse=True)[:2]
            today=next((d for d in days if d["date"]==day),{})
            def volumes(d):return {parts[0]:float(parts[2]) for parts in (line.split() for line in d.get("data",[]))}
            history_volume=[volumes(d) for d in prior]
            volume=volumes(today)
            for hm,kind,price in events:
                metrics=None
                if kind=="连续确认" and level:
                    baseline=sum(v[hm] for v in history_volume)/2
                    if baseline:metrics=(volume[hm]/baseline,price/level-1)
                log(symbol,hm,kind,price,metrics)
        last_round=None
        while not stop.is_set():
            now=dt.datetime.now(CN)
            if last_round and not session(now) and last_round.date()==now.date() and (now.weekday()>=5 or last_round.hour>=15 or now.hour<9 or (11,31)<=(last_round.hour,last_round.minute)<(13,0) and now.hour<13):
                stop.wait(5);continue
            start=time.monotonic()
            if now.strftime("%Y%m%d")!=day:
                day=now.strftime("%Y%m%d");reported.clear();history.clear();levels.clear();above.clear()
            data={}
            errors=0
            for task in cf.as_completed([pool.submit(quotes,b,book) for b in batches]):
                try:data.update(task.result())
                except Exception:errors+=1
            last_round=now
            current={s:q for s,q in data.items() if q[2].date()==now.date() and q[3]>0}
            fresh=0
            for s,q in current.items():
                price,_,ts,_=q[:4]
                age=(dt.datetime.now(CN)-ts).total_seconds()
                if not session(now) or not 0<=age<=90:continue
                fresh+=1
                if s in levels:
                    is_above=price>levels[s]
                    if is_above and above.get(s) is False:
                        log(s,ts.strftime("%H%M%S"),"快照越过区间",price)
                    above[s]=is_above
            updates.put(("quotes",(names,data,len(current),fresh,errors,time.monotonic()-start)))
            # 分时接口每请求只能读取一只，轮转全部当日有成交的证券。
            if not history:history.extend(sorted(current))
            if now.strftime("%H%M")>="0945":
                block=[history.popleft() for _ in range(min(12,len(history)))]
                jobs={pool.submit(minute_data,s):s for s in block}
                for task in cf.as_completed(jobs):
                    s=jobs[task]
                    try:
                        inspect(s,task.result(),now)
                    except (OSError,ValueError,KeyError,TypeError,IndexError):continue
            db.commit()
            updates.put(("progress",f"已取得开盘区间 {len(levels)} 只 · 本次分时轮转待查 {len(history)} 只"))
            if not session(now) and now.hour>=15:
                # 收盘后继续完成一轮历史信号核对，避免再次访问全市场报价。
                history=deque(sorted(current));total=len(history);success=0
                if book:updates.put(("review_progress",(day,total,0,0)))
                while history and not stop.is_set():
                    block=[history.popleft() for _ in range(min(12,len(history)))]
                    for s,task in [(s,pool.submit(minute_data,s)) for s in block]:
                        try:
                            inspect(s,task.result(),now);success+=1
                        except (OSError,ValueError,KeyError,IndexError,TypeError):continue
                    db.commit()
                    if book:updates.put(("review_progress",(day,total,total-len(history),success)))
                    updates.put(("progress",f"收盘复核 · 已取得区间 {len(levels)} 只 · 待查 {len(history)} 只"))
                    stop.wait(1)
            stop.wait(5)

def main():
    import tkinter as tk
    from tkinter import ttk
    root=tk.Tk();root.title("沪深北 A 股盯盘");root.geometry("1020x680")
    messages=queue.Queue();stop=threading.Event()
    def worker():
        while not stop.is_set():
            try:run(stop,messages)
            except Exception as exc:
                messages.put(("status",f"连接失败 · {exc} · 30秒后重试"))
                stop.wait(30)
    bg, panel, surface = "#10151d", "#18212e", "#202d3c"
    white, muted, red, green, gold = "#edf2f8", "#a5b7c9", "#ff7980", "#58dba7", "#ffcf77"
    root.configure(bg=bg)
    root.minsize(840,540)
    root.option_add("*Font", ("Microsoft YaHei UI",10))
    style=ttk.Style(root)
    style.theme_use("clam")
    style.configure("TNotebook",background=bg,borderwidth=0)
    style.configure("TNotebook.Tab",padding=(18,8),background=panel,foreground=muted)
    style.map("TNotebook.Tab",background=[("selected",surface)],foreground=[("selected",gold)])
    style.configure("Treeview",background=bg,fieldbackground=bg,foreground=white,rowheight=29,borderwidth=0,font=("Microsoft YaHei UI",10))
    style.map("Treeview",background=[("selected","#344e67")],foreground=[("selected",white)])
    style.configure("Treeview.Heading",background=surface,foreground=muted,borderwidth=0,padding=(8,6),font=("Microsoft YaHei UI",9,"bold"))
    style.map("Treeview.Heading",background=[("active",surface)])
    style.configure("Vertical.TScrollbar",background=surface,troughcolor=bg,arrowcolor=muted,borderwidth=0)
    def label(parent,text,color=white,size=10,bold=False,background=bg):
        return tk.Label(parent,text=text,bg=background,fg=color,anchor="w",
                        font=("Microsoft YaHei UI",size,"bold" if bold else "normal"))
    header=tk.Frame(root,bg=bg)
    header.pack(fill="x",padx=18,pady=(14,8))
    label(header,"沪深北行情",white,19,True).pack(side="left")
    label(header,"全市场盯盘",gold,10).pack(side="left",padx=12)
    status=tk.StringVar(value="正在连接行情源…")
    progress=tk.StringVar(value="正在读取股票名录")
    tk.Label(header,textvariable=status,bg=bg,fg=muted).pack(side="right")
    cards=tk.Frame(root,bg=bg)
    cards.pack(fill="x",padx=18)
    metrics=[]
    for index,(title,color) in enumerate([("有效报价",white),("当日上涨",red),("当日下跌",green),("当日平盘",muted)]):
        cards.columnconfigure(index,weight=1,uniform="market")
        card=tk.Frame(cards,bg=panel,padx=12,pady=5)
        card.grid(row=0,column=index,sticky="ew",padx=(0,5 if index<3 else 0))
        label(card,title,muted,9,background=panel).pack(anchor="w")
        number=label(card,"—",color,17,True,panel)
        number.pack(anchor="w");metrics.append(number)
    search=tk.StringVar()
    bar=tk.Frame(root,bg=bg);bar.pack(fill="x",padx=18,pady=10)
    label(bar,"查找股票",muted).pack(side="left")
    tk.Entry(bar,textvariable=search,width=22,bg=panel,fg=white,insertbackground=gold,
             relief="flat",highlightthickness=1,highlightbackground=surface,
             highlightcolor=gold).pack(side="left",padx=8,ipady=4)
    detail=tk.StringVar(value="当日涨跌家数按有成交的股票统计")
    tk.Label(bar,textvariable=detail,bg=bg,fg=muted).pack(side="right")
    tabs=ttk.Notebook(root);tabs.pack(fill="both",expand=True,padx=18)
    def grid(title,cols):
        frame=tk.Frame(tabs,bg=bg);tabs.add(frame,text=title)
        tree=ttk.Treeview(frame,columns=cols,show="headings",selectmode="browse")
        for col in cols:
            anchor="e" if col in ("最新价","涨跌幅","价格") else "center"
            tree.heading(col,text=col,anchor=anchor);tree.column(col,width=115,minwidth=60,anchor=anchor)
        scroll=ttk.Scrollbar(frame,command=tree.yview);tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right",fill="y");tree.pack(fill="both",expand=True)
        return tree
    prices=grid("行情排行",("市场","代码","简称","最新价","涨跌幅","报价时间"))
    events=grid("策略信号 · 可延迟补录",("日期","行情时刻","代码","简称","信号","价格"))
    for parity,shade in enumerate([bg,"#141b25"]):
        for direction,color in [("up",red),("down",green),("flat",muted)]:
            prices.tag_configure(direction+str(parity),foreground=color,background=shade)
    events.tag_configure("confirm",foreground=gold)
    tk.Label(root,textvariable=progress,bg=bg,fg=muted,anchor="w").pack(fill="x",padx=18,pady=6)
    latest={}
    def render(*_):
        position=prices.yview()
        selection=prices.selection()
        prices.delete(*prices.get_children())
        term=search.get().strip().lower()
        count=0
        for symbol,(name,price,change,stamp) in latest.items():
            if term not in symbol and term not in name:continue
            market={"sh":"沪市","sz":"深市","bj":"北交所"}[symbol[:2]]
            direction="up" if change>0 else "down" if change<0 else "flat"
            prices.insert("","end",iid=symbol,values=(market,symbol[2:],name,f"{price:.2f}",f"{change:+.2f}%",stamp),tags=(direction+str(count%2),))
            count+=1
        if selection and prices.exists(selection[0]):prices.selection_set(selection[0])
        if position:prices.yview_moveto(0 if _ else position[0])
    search.trace_add("write",render)
    def tick():
        nonlocal latest
        for _ in range(300):
            try:kind,val=messages.get_nowait()
            except queue.Empty:break
            if kind=="quotes":
                names,data,current,fresh,errors,elapsed=val
                latest={s:(names[s],q[0],q[1],q[2].strftime("%m-%d %H:%M:%S")) for s,q in sorted(data.items(),key=lambda kv:-kv[1][1])}
                today=dt.datetime.now(CN).date()
                changes=[q[1] for q in data.values() if q[2].date()==today and q[3]>0]
                counts=[len(data),sum(c>0 for c in changes),sum(c<0 for c in changes),sum(c==0 for c in changes)]
                for widget,count in zip(metrics,counts):widget.configure(text=f"{count:,}")
                render()
                status.set(f"行情扫描 {elapsed:.1f} 秒  ·  失败批次 {errors}")
                detail.set(f"名录 {len(names):,}  ·  当日成交 {current:,}  ·  盘中及时报价 {fresh:,}")
            elif kind=="event":
                events.insert("",0,values=val,tags=("confirm",) if val[4]=="连续确认" else ())
                if len(events.get_children())>500:events.delete(events.get_children()[-1])
            elif kind=="progress":progress.set(val)
            else:status.set(val)
        root.after(400,tick)
    def close():
        stop.set();root.destroy()
    root.protocol("WM_DELETE_WINDOW",close)
    threading.Thread(target=worker,daemon=True).start();root.after(100,tick);root.mainloop()

if __name__=="__main__":main()
