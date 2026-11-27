import datetime as dt
import queue
import sqlite3
import sys
import threading
from pathlib import Path
from PyQt5 import QtCore as C, QtGui as G, QtWidgets as W
import 全市场盯盘 as market
from paper import Account, fen, commission
from review import Review
from inspector import Inspector

UP, DOWN, MINT = "#ba424b", "#17795e", "#73e9bb"
STYLE = """
QWidget {font-family:'Microsoft YaHei UI';font-size:13px;color:#172b30}
QMainWindow,#workspace {background:#eff2ef}
#rail,#rail QLabel {background:#132729;color:#cce0d8}
#brand {font-size:22px;font-weight:bold}
#rail QPushButton {color:#b7cbc6;background:transparent;border:0;text-align:left;padding:12px;border-radius:6px}
#rail QPushButton:checked {background:#295149;color:#a1f3cb;font-weight:bold}
#headline {font-size:27px;font-weight:bold}
#muted {color:#536e69;font-size:12px}
#card,#detail,#listPanel {background:#ffffff;border-radius:10px}
#cardTitle {font-size:12px;color:#536e69}
#number {font: bold 25px 'Segoe UI'}
#hero,#hero QLabel {background:#173a37;color:#d2e8de}
#hero {border-radius:10px}
#heroTitle {font-size:19px;font-weight:bold}
#price {font: bold 36px 'Segoe UI'}
QLineEdit {background:#f6f8f6;border:1px solid #c7d6d0;border-radius:6px;padding:7px}
QComboBox {background:#fff;border:1px solid #c7d6d0;border-radius:5px;padding:6px}
QTableView {background:white;alternate-background-color:#f5f8f6;border:0;selection-background-color:#d8eee4;selection-color:#102c25;outline:0}
QHeaderView::section {background:#eef4ef;color:#47645d;border:0;padding:6px;font-size:12px}
QSplitter::handle {background:transparent}
"""
def label(text="", name=""):
    obj=W.QLabel(text);obj.setObjectName(name);return obj

def panel(name):
    obj=W.QFrame();obj.setObjectName(name); layout=W.QVBoxLayout(obj)
    layout.setContentsMargins(15,12,15,12);layout.setSpacing(6)
    return obj,layout

class Rows(C.QAbstractTableModel):
    def __init__(self,headers):
        super().__init__();self.headers=headers;self.rows=[]
    def replace(self,rows):
        self.beginResetModel();self.rows=rows;self.endResetModel()
    def rowCount(self,parent=C.QModelIndex()):return len(self.rows)
    def columnCount(self,parent=C.QModelIndex()):return len(self.headers)
    def headerData(self,i,orientation,role):
        if orientation==C.Qt.Horizontal and role==C.Qt.DisplayRole:return self.headers[i]
    def data(self,index,role):
        if not index.isValid():return None
        value=self.rows[index.row()][index.column()]
        if role==C.Qt.DisplayRole:return value
        if role==C.Qt.TextAlignmentRole:return int(C.Qt.AlignCenter)
        if role==C.Qt.ForegroundRole and value.endswith("%"):
            return G.QColor(UP if value.startswith("+") else DOWN if value.startswith("-") else "#536e69")

def table(model):
    view=W.QTableView();view.setModel(model);view.verticalHeader().hide()
    view.setSelectionBehavior(W.QAbstractItemView.SelectRows);view.setSelectionMode(W.QAbstractItemView.SingleSelection)
    view.setShowGrid(False);view.setAlternatingRowColors(True)
    view.horizontalHeader().setSectionResizeMode(W.QHeaderView.Stretch)
    view.verticalHeader().setDefaultSectionSize(32)
    return view

class Chart(W.QWidget):
    def __init__(self):
        super().__init__();self.points=[];self.message="等待行情";self.setMinimumHeight(165)
    def paintEvent(self,event):
        p=G.QPainter(self);p.setRenderHint(G.QPainter.Antialiasing)
        left,right,top,bottom=8,self.width()-50,14,self.height()-30
        p.setPen(G.QColor("#a8c3b9"))
        if len(self.points)<2:
            p.drawText(self.rect(),C.Qt.AlignCenter,self.message);return
        lo=min(x[1] for x in self.points);hi=max(x[1] for x in self.points)
        pad=max((hi-lo)*.10,.005);lo-=pad;hi+=pad
        for i in range(4):
            y=top+(bottom-top)*i/3
            p.setPen(G.QPen(G.QColor("#35544f"),1,C.Qt.DotLine));p.drawLine(left,int(y),right,int(y))
            p.setPen(G.QColor("#b6cec2"));p.drawText(right+3,int(y)+4,f"{hi-(hi-lo)*i/3:.2f}")
        path=G.QPainterPath()
        for i,(minute,price) in enumerate(self.points):
            x=left+(right-left)*minute/240;y=bottom-(price-lo)/(hi-lo)*(bottom-top)
            (path.moveTo if i==0 else path.lineTo)(x,y)
        area=G.QPainterPath(path);area.lineTo(x,bottom);area.lineTo(left+(right-left)*self.points[0][0]/240,bottom);area.closeSubpath()
        grad=G.QLinearGradient(0,top,0,bottom);grad.setColorAt(0,G.QColor(93,228,168,100));grad.setColorAt(1,G.QColor(93,228,168,0))
        p.fillPath(area,grad);p.setPen(G.QPen(G.QColor(MINT),2));p.drawPath(path)
        p.setPen(G.QColor("#b6cec2"))
        for x,t in [(left,"09:30"),((left+right)//2-15,"11:30"),(right-30,"15:00")]:p.drawText(x,bottom+20,t)

class Terminal(W.QMainWindow):
    def __init__(self):
        super().__init__();self.setWindowTitle("观市 · 沪深主板模拟终端");self.resize(1190,750);self.setMinimumSize(920,600)
        self.review=Review(market.ROOT);self.lastreview=0;self.init_accounts();self.bus=queue.Queue();self.stop=threading.Event();self.data={};self.names={};self.symbol="sz000711";self.pending=False;self.loaded=None;self.lastchart=0
        shell=W.QWidget();self.setCentralWidget(shell);base=W.QHBoxLayout(shell);base.setContentsMargins(0,0,0,0);base.setSpacing(0)
        rail,nav=panel("rail");rail.setFixedWidth(142);base.addWidget(rail)
        nav.addWidget(label("◩  观市","brand"));nav.addSpacing(28)
        self.pages=W.QStackedWidget();self.nav=[]
        for i,title in enumerate(["行情总览","信号记录","50元以内","模拟账户","每日复盘"]):
            btn=W.QPushButton(title);btn.setCheckable(True);btn.setAutoExclusive(True);btn.clicked.connect(lambda checked,i=i:self.pages.setCurrentIndex(i));nav.addWidget(btn);self.nav.append(btn)
        self.nav[0].setChecked(True);nav.addStretch();nav.addWidget(label("沪深主板 A 股"))
        body,layout=panel("workspace");base.addWidget(body,1);layout.setContentsMargins(18,12,18,8);layout.setSpacing(10)
        self.today=label();self.today.setWordWrap(True);layout.addWidget(self.today)
        self.heading=label("市场总览","headline");layout.addWidget(self.heading)
        self.pages.currentChanged.connect(lambda i:self.heading.setText(["市场总览","策略信号","50元以内","模拟账户","每日复盘"][i]))
        cards=W.QHBoxLayout();layout.addLayout(cards);self.nums=[]
        for title,color in [("有效报价","#183c35"),("上涨家数",UP),("下跌家数",DOWN),("平盘家数","#59726d")]:
            card,col=panel("card");col.addWidget(label(title,"cardTitle"));num=label("—","number");num.setStyleSheet("color:"+color);col.addWidget(num);cards.addWidget(card);self.nums.append(num)
        layout.addWidget(self.pages,1)
        split=W.QSplitter();split.setChildrenCollapsible(False);self.pages.addWidget(split)
        left,col=panel("listPanel");split.addWidget(left)
        filters=W.QHBoxLayout();col.addLayout(filters);self.search=W.QLineEdit();self.search.setPlaceholderText("搜索代码 / 股票简称")
        self.scope=W.QComboBox();self.scope.addItems(["全部主板","沪市主板","深市主板"]);filters.addWidget(self.search,1);filters.addWidget(self.scope)
        inspect=W.QPushButton("查这只股");filters.addWidget(inspect)
        inspect.clicked.connect(self.lookup)
        self.model=Rows(["代码","名称","最新","涨跌幅","报价时间"]);self.list=table(self.model);col.addWidget(self.list,1)
        self.list.clicked.connect(self.select);self.list.activated.connect(self.select)
        self.search.textChanged.connect(self.refresh);self.scope.currentIndexChanged.connect(self.refresh)
        hero,h=panel("hero");split.addWidget(hero);split.setSizes([620,390])
        self.inspector=Inspector(self,market);h.addWidget(self.inspector)
        split.setSizes([425,590])
        self.events=Rows(["交易日","行情时间","代码","简称","信号","价格"]);self.pages.addWidget(table(self.events))
        cheap,cl=panel("listPanel");self.cheap=Rows(["代码","名称","最新","涨跌幅","报价时间","现金可买(股)"]);cv=table(self.cheap);cl.addWidget(label("沪深主板 · 最新报价 ≤ 50.00 元 · 按涨幅排序"));cl.addWidget(cv);self.pages.addWidget(cheap)
        def opencheap(idx):
            self.symbol=self.lowcodes[idx.row()];self.loaded=None;self.nav[0].click();self.display()
        cv.doubleClicked.connect(opencheap)
        account,al=panel("listPanel");self.balance=label("","heroTitle");al.addWidget(self.balance);self.position=label();al.addWidget(self.position)
        controls=W.QHBoxLayout();al.addLayout(controls);controls.addWidget(label("交易账户"))
        self.accounts=W.QComboBox();controls.addWidget(self.accounts,1)
        for number,amount in self.registry.execute("SELECT id,capital FROM profiles ORDER BY id"):
            self.accounts.addItem(f"账户 {number} · 本金 {amount/100:,.2f} 元",number)
        self.accounts.setCurrentIndex(self.accounts.findData(self.active_id))
        self.accounts.currentIndexChanged.connect(self.switch_account)
        create=W.QPushButton("新建账户 · 设置初始资金");controls.addWidget(create);create.clicked.connect(self.new_account)
        rules=label("仅当前选中账户参与新交易，切换后原账户暂停。沪深主板 · 单仓 · 盘中新发现的连续确认 → 后续卖一买入。次日起跌3%或涨6%触发退出，最早下一交易日14:50仍持有则触发卖出，按后续买一模拟成交。双边佣金万五、最低5元，卖出另计万五。")
        rules.setWordWrap(True);al.addWidget(rules)
        self.ledger=Rows(["报价时间","代码","股票简称","方向","成交价","股数","费用","触发原因"]);al.addWidget(table(self.ledger));self.pages.addWidget(account);self.show_account()
        review,rl=panel("listPanel");self.reviewdate=W.QDateEdit(C.QDate.currentDate());self.reviewdate.setCalendarPopup(True)
        rl.addWidget(self.reviewdate);self.cover=label();rl.addWidget(self.cover)
        helptext=label("两组从启用日起各用5000元。候选在首分钟突破时入场；现行组等待连续确认。费用、盘口和退出规则相同。盈亏与回撤为截至所选日的累计值，回撤按实际收到的报价计算。")
        helptext.setWordWrap(True);rl.addWidget(helptext)
        self.summary=Rows(["对照组","信号","及时发现","买入","买入/及时","累计盈亏(元)","最大回撤"])
        rl.addWidget(table(self.summary),1)
        self.category=W.QComboBox();self.category.addItems(["全部","本账户已成交","盘中未成交","盘后或超时"]);rl.addWidget(self.category)
        self.category.currentIndexChanged.connect(self.show_review)
        self.experiment=label();self.experiment.setWordWrap(True);rl.addWidget(self.experiment)
        self.reviewrows=Rows(["行情分钟","代码","规则","首次发现","延迟(秒)","对照账户","主账户"])
        rv=table(self.reviewrows);rv.doubleClicked.connect(self.open_review);rl.addWidget(rv,3);self.pages.addWidget(review)
        self.reviewdate.dateChanged.connect(self.show_review);self.pages.currentChanged.connect(self.show_review)
        self.status=label("连接行情源…","muted");self.progress=label("","muted");layout.addWidget(self.status);layout.addWidget(self.progress)
        self.timer=C.QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(400)
        threading.Thread(target=self.worker,daemon=True).start()
    def account_path(self,number):
        return market.ROOT/("观市模拟账户.sqlite3" if number==1 else f"观市模拟账户_{number}.sqlite3")
    def init_accounts(self):
        self.registry=sqlite3.connect(market.ROOT/"模拟账户设置.sqlite3")
        with self.registry:
            self.registry.execute("CREATE TABLE IF NOT EXISTS profiles(id INTEGER PRIMARY KEY,capital INTEGER NOT NULL)")
            self.registry.execute("CREATE TABLE IF NOT EXISTS active(id INTEGER PRIMARY KEY CHECK(id=1),profile INTEGER NOT NULL)")
            self.registry.execute("INSERT OR IGNORE INTO profiles VALUES(1,500000)")
            self.registry.execute("INSERT OR IGNORE INTO active VALUES(1,1)")
        self.active_id,amount=self.registry.execute("SELECT p.id,p.capital FROM profiles p JOIN active a ON a.profile=p.id").fetchone()
        self.review.profile=self.active_id
        self.account=Account(self.account_path(self.active_id),amount)
        self.activated=None
    def new_account(self):
        amount,ok=W.QInputDialog.getDouble(self,"新建模拟账户","初始资金（元）",5000,0.01,999999999.99,2)
        if not ok:return
        capital=fen(amount)
        with self.registry:
            number=self.registry.execute("INSERT INTO profiles(capital) VALUES(?)",(capital,)).lastrowid
        self.accounts.addItem(f"账户 {number} · 本金 {capital/100:,.2f} 元",number)
        self.accounts.setCurrentIndex(self.accounts.count()-1)
    def switch_account(self,index):
        number=self.accounts.itemData(index)
        if number is None or number==self.active_id:return
        capital=self.registry.execute("SELECT capital FROM profiles WHERE id=?",(number,)).fetchone()[0]
        next_account=Account(self.account_path(number),capital)
        with self.registry:self.registry.execute("UPDATE active SET profile=? WHERE id=1",(number,))
        self.account.db.close()
        self.account=next_account;self.active_id=number;self.activated=dt.datetime.now(market.CN)
        self.review.profile=number;self.show_account();self.refresh_cheap();self.inspector.search();self.show_review()
    def worker(self):
        db=market.ROOT/"全市场盯盘记录.sqlite3"
        if db.exists():
            try:
                with sqlite3.connect(db.as_uri()+"?mode=ro",uri=True) as c:
                    records=c.execute("SELECT day,hm,symbol,kind,price FROM events WHERE substr(symbol,1,5) IN ('sh600','sh601','sh603','sh605') OR symbol BETWEEN 'sz000001' AND 'sz000999' OR symbol BETWEEN 'sz001200' AND 'sz004999' ORDER BY day DESC, hm DESC LIMIT 500").fetchall()
                    self.bus.put(("history",records))
            except sqlite3.Error:pass
        while not self.stop.is_set():
            try:market.run(self.stop,self.bus,book=True)
            except Exception as e:self.bus.put(("status",f"连接失败：{e}，30 秒后重试"));self.stop.wait(30)
    def show_account(self):
        s=self.account.state();pos=s["position"];mv=pos["mark"]*pos["shares"] if pos else 0
        self.balance.setText(f"总资产 {(s['cash']+mv)/100:,.2f} 元　　现金 {s['cash']/100:,.2f}　　盈亏 {(s['cash']+mv-s.get('capital',500000))/100:+,.2f}")
        self.position.setText((f"{self.names.get(pos['symbol'], '简称待同步')}（{pos['symbol']}）　{pos['shares']} 股　买入 {pos['price']/100:.2f}　估值 {pos['mark']/100:.2f}" if pos else "空仓")+ "　·　"+(s['pending']['side']+"待撮合" if s['pending'] else s['note']))
        now=dt.datetime.now(market.CN)
        fills=self.account.db.execute("SELECT side,symbol FROM fills WHERE stamp>=? ORDER BY id",(now.date().isoformat(),)).fetchall()
        trades="、".join(f"{side}{sym[2:]}" for side,sym in fills[-3:]) or "暂无成交"
        qtime=max((q[2] for q in self.data.values()),default=None)
        self.today.setText(f"账户 {self.active_id} · 本金 {s.get('capital',500000)/100:,.2f} 元　今日 {len(fills)} 笔（{trades}）　现金 {s['cash']/100:,.2f} 元　持仓浮盈亏 {((pos['mark']-pos['price'])*pos['shares']/100 if pos else 0):+.2f} 元　最新报价 {qtime.strftime('%m-%d %H:%M:%S') if qtime else '待同步'}")
        self.ledger.replace([(t[5:19].replace('T',' '),sym,self.names.get(sym,'简称待同步'),side,f"{price/100:.2f}",str(qty),f"{fee/100:.2f}",reason) for t,sym,side,price,qty,fee,reason in self.account.db.execute("SELECT stamp,symbol,side,price,shares,fee,reason FROM fills ORDER BY id DESC LIMIT 500")])
    def select(self,idx):
        self.symbol=self.visible[idx.row()];self.loaded=None;self.display()
    def refresh(self,*args):
        text=self.search.text().strip().lower();prefix=["","sh","sz"][self.scope.currentIndex()]
        self.visible=[s for s in self.order if s.startswith(prefix) and (text in s or text in self.names[s])] if self.data else []
        rows=[]
        for s in self.visible:
            price,change,t,_=self.data[s][:4];rows.append((s[2:],self.names[s],f"{price:.2f}",f"{change:+.2f}%" if change else "0.00%",t.strftime("%H:%M:%S")))
        offset=self.list.verticalScrollBar().value()
        self.model.replace(rows);self.list.verticalScrollBar().setValue(offset)
    def lookup(self):
        query=self.search.text().strip()
        if query:
            self.inspector.code.setText(query if not getattr(self,"visible",[]) or len(self.visible)!=1 else self.visible[0])
        self.inspector.search()
    def display(self):
        if self.loaded!=self.symbol:
            self.loaded=self.symbol;self.inspector.select_stock(self.symbol)
    def open_review(self,idx):
        sym=self.reviewrows.rows[idx.row()][1]
        self.symbol=self.loaded=sym
        self.nav[0].click();self.inspector.select_stock(sym,self.reviewdate.date())
    def refresh_cheap(self):
        cash=self.account.state()["cash"]
        rows=[]
        for sym in getattr(self,"lowcodes",[]):
            price,change,t=self.data[sym][:3]
            cents=fen(price)
            qty=(cash//(100*cents))*100 if cents else 0
            while qty and qty*cents+commission(qty*cents)>cash:qty-=100
            rows.append((sym[2:],self.names[sym],f"{price:.2f}",f"{change:+.2f}%",t.strftime("%H:%M:%S"),str(qty)))
        self.cheap.replace(rows)
    def show_review(self,*args):
        if self.pages.currentIndex()!=4:return
        day=self.reviewdate.date().toString("yyyyMMdd")
        coverage,stats,rows=self.review.report(day,self.category.currentText())
        if self.category.currentText()=="本账户已成交":
            rows=[(stamp[11:16],sym,side,"—","—","—",f"{shares} 股 × {price/100:.2f}") for stamp,sym,side,shares,price in self.account.db.execute("SELECT stamp,symbol,side,shares,price FROM fills WHERE replace(substr(stamp,1,10),'-','')=? ORDER BY stamp DESC",(day,))]
        self.experiment.setText(self.review.experiments.summary(day))
        post,online,unknown=self.review.db.execute("""SELECT count(CASE WHEN substr(found,12,5)>='15:00' THEN 1 END),
        count(CASE WHEN substr(found,12,5)<'15:00' AND c.hm IS NOT NULL THEN 1 END),
        count(CASE WHEN substr(found,12,5)<'15:00' AND c.hm IS NULL THEN 1 END)
        FROM signals s LEFT JOIN coverage c ON c.day=s.day AND c.hm=s.hm WHERE s.day=? AND s.outcome='盘后或超时发现'""",(day,)).fetchone()
        progress=self.review.db.execute("SELECT total,done,success FROM progress WHERE day=?",(day,)).fetchone()
        progress_text=f"收盘复核 {progress[1]}/{progress[0]} 只 · 待查 {progress[0]-progress[1]} · 读取失败 {progress[1]-progress[2]} 只" if progress else "本日暂无收盘复核进度"
        self.cover.setWordWrap(True)
        self.cover.setText(f"{progress_text}　账户 {self.active_id} · 收盘后发现 {post} 条 · 其余延迟中信号分钟在线 {online} 条 / 覆盖未知 {unknown} 条 · 交易时段有程序心跳 {coverage[0]} 分钟　·　有新鲜报价 {coverage[1]} 分钟　·　无记录时段覆盖未知")
        self.summary.replace(stats);self.reviewrows.replace(rows)
    def tick(self):
        now=dt.datetime.now(market.CN)
        self.review.heartbeat(now)
        if market.session(now) and self.data:
            latest=max(q[2] for q in self.data.values())
            if (now-latest).total_seconds()>90:self.status.setText(f"行情中断或延迟 · 最后报价 {latest:%m-%d %H:%M:%S}")
        if market.time.monotonic()-self.lastreview>5:
            self.show_review();self.lastreview=market.time.monotonic()
        signals=[]
        for _ in range(750):
            try:k,v=self.bus.get_nowait()
            except queue.Empty:break
            if k=="quotes":
                self.names,self.data,current,fresh,errors,elapsed=v
                self.order=sorted(self.data,key=lambda s:-self.data[s][1])
                changes=[q[1] for q in self.data.values() if q[3] and q[2].date()==dt.datetime.now(market.CN).date()]
                for w,n in zip(self.nums,[len(self.data),sum(x>0 for x in changes),sum(x<0 for x in changes),sum(x==0 for x in changes)]):w.setText(f"{n:,}")
                self.lowcodes=[s for s in self.order if 0<self.data[s][0]<=50]
                self.review.primary_scan(self.account,self.data,dt.datetime.now(market.CN));self.review.scan(self.data,dt.datetime.now(market.CN),fresh);self.show_account()
                self.refresh_cheap();self.refresh();self.display()
                self.status.setText(("行情中断或延迟 · " if market.session(now) and not fresh else "")+f"名录 {len(self.names):,}　·　当日成交 {current:,}　·　盘中90秒内报价 {fresh:,}　·　扫描 {elapsed:.1f} 秒　·　失败批次 {errors}")
            elif k=="review_progress":
                with self.review.db:self.review.db.execute("INSERT OR REPLACE INTO progress VALUES(?,?,?,?)",v)
            elif k=="checked":
                with self.review.db:self.review.db.execute("INSERT OR IGNORE INTO checks VALUES(?,?,?,?)",v)
            elif k=="candidate":self.review.experiments.candidate(*v,self.data.get(v[2]))
            elif k=="review_signal":self.review.signal(*v)
            elif k=="paper_signal":
                if self.activated is None or v[-1]>=self.activated:self.review.primary_signal(self.account,v);self.show_account()
            elif k=="history":signals.extend((d,t,s,self.names.get(s,"—"),kind,f"{p:.2f}") for d,t,s,kind,p in v)
            elif k=="event":signals.append(tuple(map(str,v)))
            elif k=="progress":self.progress.setText(v)
            elif k=="status":self.status.setText(v)
            elif k=="chart":
                self.pending=False;s,date,points,hi=v
                if s!=self.symbol:self.display();continue
                self.loaded=s;self.lastchart=market.time.monotonic();self.chart.points=points;self.chart.message="暂无可用分时行情";self.chart.update()
                self.sessionlabel.setText("分时走势　"+date);self.range.setText("开盘区间上沿　"+(f"{hi:.2f}" if hi is not None else "—"))
        if signals or (self.names and self.events.rows and self.events.rows[0][3]=="—"):
            combined=[(d,t,s,self.names.get(s,n),kind,p) for d,t,s,n,kind,p in signals+self.events.rows]
            self.events.replace(sorted(combined,key=lambda r:(r[0],r[1]),reverse=True)[:500])
    def closeEvent(self,event):
        self.stop.set();event.accept()

if __name__=="__main__":
    W.QApplication.setAttribute(C.Qt.AA_EnableHighDpiScaling)
    app=W.QApplication(sys.argv);app.setStyle("Fusion");app.setStyleSheet(STYLE)
    icon=Path(getattr(sys,"_MEIPASS",Path(__file__).parent))/"guanshi.ico"
    if icon.exists():app.setWindowIcon(G.QIcon(str(icon)))
    win=Terminal();win.show();sys.exit(app.exec_())

