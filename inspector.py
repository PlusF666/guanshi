"""Single-stock intraday review. Prices are yuan and source volume is cumulative lots."""
import json
import queue
import threading
from pathlib import Path

from PyQt5 import QtCore as C, QtGui as G, QtWidgets as W


def series(lines):
    rows = {}
    for line in lines:
        hm, price, volume, amount, *_ = line.split()
        if "0930" <= hm <= "1130" or "1300" <= hm <= "1500":
            rows[hm] = float(price), float(volume), float(amount)
    result = []
    previous = None
    for hm, (price, volume, amount) in sorted(rows.items()):
        minute = int(hm[:2]) * 60 + int(hm[2:])
        consecutive = previous and (minute == previous[0]+1 or (previous[0],minute)==(690,780))
        delta = volume if hm=="0930" else volume-previous[1] if consecutive else None
        result.append((hm, minute-570-(90 if minute>=780 else 0), price,
                       amount/(volume*100) if volume else None,
                       delta if delta is not None and delta>=0 else None))
        previous = minute, volume
    return result


class Plot(W.QWidget):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.marks = {}
        self.setMinimumHeight(240)
        self.setMouseTracking(True)

    def mouseMoveEvent(self, event):
        if self.rows:
            x = (event.x()-10)*240/max(1,self.width()-76)
            hm, _, price, avg, vol = min(self.rows,key=lambda r:abs(r[1]-x))
            a = f"{avg:.2f}" if avg is not None else "—"
            v = f"{vol:.0f}" if vol is not None else "—"
            W.QToolTip.showText(event.globalPos(),f"{hm[:2]}:{hm[2:]}  价格 {price:.2f}  均价 {a}  分钟量 {v} 手",self)

    clicked=C.pyqtSignal(str)
    def mousePressEvent(self,event):
        if self.rows:
            x=(event.x()-10)*240/max(1,self.width()-76)
            self.clicked.emit(min(self.rows,key=lambda row:abs(row[1]-x))[0])

    def paintEvent(self,event):
        p = G.QPainter(self)
        p.fillRect(self.rect(),G.QColor("#142825"))
        p.setRenderHint(G.QPainter.Antialiasing)
        p.setPen(G.QColor("#c4d9d1"))
        if not self.rows:
            p.drawText(self.rect(),C.Qt.AlignCenter,"暂无该日分时数据")
            return
        width, height = self.width()-76, self.height()
        bottom, base = int(height*.63),height-24
        prices = [v for r in self.rows for v in r[2:4] if v is not None]
        lo,hi = min(prices)-.005,max(prices)+.005
        peak = max([r[4] or 0 for r in self.rows]+[1])
        for frac in (0,.5,1):
            y = round(18+frac*(bottom-18))
            p.setPen(G.QColor("#34524a"));p.drawLine(10,y,width+10,y)
            p.setPen(G.QColor("#c4d9d1"));p.drawText(width+14,y+4,f"{hi-frac*(hi-lo):.2f}")
        for index,color in ((2,"#71e2bb"),(3,"#f4c76d")):
            path = G.QPainterPath()
            last = None
            for row in self.rows:
                value = row[index]
                x = 10+width*row[1]/240
                if value is None:
                    last = None
                    continue
                y = bottom-(value-lo)/(hi-lo)*(bottom-18)
                if last is None or row[1]-last>1:path.moveTo(x,y)
                else:path.lineTo(x,y)
                last = row[1]
                if index==2 and row[0] in self.marks:
                    p.setPen(G.QColor("#ffffff"));p.drawText(int(x),int(y)-3,self.marks[row[0]])
            p.setPen(G.QPen(G.QColor(color),1.7));p.drawPath(path)
        p.setPen(G.QColor("#80afa2"))
        for _,offset,_,_,volume in self.rows:
            if volume:
                x = round(10+width*offset/240)
                p.drawLine(x,base,x,base-round((base-bottom-22)*volume/peak))
        p.drawText(10,bottom+16,"分钟成交量（手）")
        for offset,title in ((0,"09:30"),(120,"11:30 / 13:00"),(240,"15:00")):
            p.drawText(round(10+width*offset/240),height-5,title)


class Inspector(W.QWidget):
    def __init__(self,terminal,market):
        super().__init__(terminal)
        self.setWindowTitle("查这只股 · 分时与决策")
        self.setMinimumWidth(300)
        layout = W.QVBoxLayout(self)
        controls = W.QHBoxLayout()
        layout.addLayout(controls)
        code = W.QLineEdit(terminal.symbol)
        code.setPlaceholderText("代码或股票简称")
        date = W.QDateEdit(C.QDate.currentDate())
        date.setCalendarPopup(True)
        button = W.QPushButton("查询")
        for widget in (code,date,button):controls.addWidget(widget)
        title = W.QLabel()
        layout.addWidget(title)
        plot = Plot()
        layout.addWidget(plot,2)
        layout.addWidget(W.QLabel("绿色  分时价     金色  成交均价 VWAP     突 / 确  信号发生分钟     买 / 卖  模拟成交分钟"))
        log = W.QPlainTextEdit()
        log.setReadOnly(True)
        layout.addWidget(log,1)
        detail = W.QLabel()
        detail.setWordWrap(True)
        layout.addWidget(detail)
        evidence = {}
        previous_volumes=[]
        def explain(hm):
            rows={r[0]:r for r in plot.rows}
            row=rows[hm]
            opening=[rows.get(f"09{m}") for m in range(30,45)]
            upper=max(r[2] for r in opening) if all(opening) else None
            above=lambda r: bool(r and upper and r[3] and r[2]>max(upper,r[3]))
            m=int(hm[:2])*60+int(hm[2:])-1
            prev=rows.get(f"{m//60:02}{m%60:02}")
            baseline=[v[hm] for v in previous_volumes if hm in v]
            ratio="历史量不足"
            if len(baseline)==2 and sum(baseline)>0 and row[3]:
                ratio=f"{today_volume.get(hm,0)*2/sum(baseline):.2f} 倍（须大于 1）"
            if hm<'0945':conditions="开盘区间形成中"
            elif upper is None:conditions="开盘区间缺分钟"
            else:
                conditions=f"区间上沿 {upper:.2f}，高于上沿 {'是' if row[2]>upper else '否'}；高于均价 {('是' if row[2]>row[3] else '否') if row[3] else '未知'}；连续两分钟站上 {'是' if above(prev) and above(row) and hm!='0945' else '否'}"
            detail.setText(f"{hm[:2]}:{hm[2:]} 行情复核 · {conditions} · 同时点累计量比 {ratio}。"+' '.join(evidence.get(hm,[])))
        plot.clicked.connect(explain)
        today_volume={}
        inbox = queue.Queue()
        generation = 0

        def search():
            nonlocal generation
            generation += 1
            token = generation
            query = code.text().strip().lower()
            sym = next((s for s,n in terminal.names.items() if n==query),query)
            if len(sym)==6 and sym.isdigit():sym=("sh" if sym.startswith("6") else "sz")+sym
            day = date.date().toString("yyyyMMdd")
            plot.rows=[];plot.marks={};plot.update();log.clear();detail.clear();evidence.clear();previous_volumes.clear();today_volume.clear()
            if len(sym)!=8 or not market.main_board(sym):
                title.setText("请输入沪深主板代码，或使用行情已同步的股票简称。")
                return
            prefix = f"{terminal.names.get(sym,sym)} · {sym} · {day} · 账户 {terminal.active_id}"
            title.setText(prefix+" · 正在读取分时")
            notes = []
            for hm,rule,found,outcome in terminal.review.db.execute("SELECT hm,rule,found,outcome FROM signals WHERE sym=? AND day=? ORDER BY hm,rule",(sym,day)):
                plot.marks[hm]="确" if rule=="连续确认" else "突"
                notes.append(f"{hm[:2]}:{hm[2:]}  {rule}  |  首次发现 {found or '未知'}  |  对照账户 {outcome}")
                evidence.setdefault(hm,[]).append(notes[-1])
                action=terminal.review.db.execute("SELECT outcome FROM account_actions WHERE profile=? AND sym=? AND found=?",(terminal.active_id,sym,found)).fetchone()
                if action and rule=="连续确认":notes.append(f"{hm[:2]}:{hm[2:]}  本账户 · {action[0]}");evidence[hm].append(notes[-1])
            for stamp,side,price,qty in terminal.account.db.execute("SELECT stamp,side,price,shares FROM fills WHERE symbol=? AND substr(stamp,1,10)=? ORDER BY stamp",(sym,date.date().toString("yyyy-MM-dd"))):
                plot.marks[stamp[11:16].replace(":","")]=side[0]
                notes.append(f"{stamp[11:19]}  当前账户{side}  {qty} 股 × {price/100:.2f} 元")
                evidence.setdefault(stamp[11:16].replace(":",""),[]).append(notes[-1])
            check=terminal.review.db.execute("SELECT count(*),min(stamp),max(stamp) FROM checks WHERE day=? AND sym=?",(day,sym)).fetchone()
            notes.append(f"个股扫描 {check[0]} 次 · "+(f"{check[1][11:19]} 至 {check[2][11:19]}" if check[0] else "覆盖未知"))
            for hm,raw,risk in terminal.review.db.execute("SELECT hm,evidence,risk FROM candidates WHERE day=? AND sym=?",(day,sym)):
                items=json.loads(raw)
                msg="公告筛选命中预亏" if risk else "未命中预亏" if items else "当时缺少可用资料"
                notes.append(f"{hm[:2]}:{hm[2:]}  {msg}")
                for kind,pub,seen,headline,url in items:
                    notes.append(f"{hm[:2]}:{hm[2:]}  {kind} {headline} · 公开 {pub} · 取得 {seen} · {url}")
            log.setPlainText("\n".join(sorted(notes)) or "本地没有该股当日的信号或本账户成交记录，个股监控覆盖情况未知。")
            def fetch():
                days=[];data=None
                try:
                    days=market.minute_data(sym)
                    data=next((d for d in days if d["date"]==day),None)
                    rows=series(data["data"]) if data else []
                    status="分钟行情" if rows else "行情源未返回该日数据"
                except Exception:
                    rows=[];status="分时读取失败，可重新查询"
                if not rows and sym=="sz000711" and day=="20261116":
                    sample=Path(__file__).with_name("711minute.json")
                    if sample.exists():
                        with sample.open(encoding="utf8") as f:rows=series(json.load(f)["data"])
                        status="本地历史分时样本"
                def volumes(d):return {v[0]:float(v[2]) for v in (line.split() for line in d.get("data",[]))}
                past=sorted((d for d in days if d["date"]<day),key=lambda d:d["date"],reverse=True)[:2]
                inbox.put((token,prefix+" · "+status,rows,[volumes(d) for d in past],volumes(data or {})))
            threading.Thread(target=fetch,daemon=True).start()

        def receive():
            while not inbox.empty():
                token,caption,rows,prior,volumes=inbox.get_nowait()
                if token==generation:
                    title.setText(caption);plot.rows=rows;previous_volumes[:]=prior;today_volume.update(volumes);plot.update()
        timer=C.QTimer(self);timer.timeout.connect(receive);timer.start(100)
        button.clicked.connect(search);code.returnPressed.connect(search)
        self.code,self.date,self.search=code,date,search
        refresh=C.QTimer(self);refresh.setInterval(60000);refresh.timeout.connect(lambda: search() if self.isVisible() and date.date()==C.QDate.currentDate() else None);refresh.start()
        date.dateChanged.connect(search)
        search()

    def select_stock(self,symbol,date=None):
        self.code.setText(symbol)
        if date is not None:
            self.date.blockSignals(True);self.date.setDate(date);self.date.blockSignals(False)
        self.search()
