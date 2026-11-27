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

UP, DOWN, MUTED = "#bc2338", "#127653", "#687180"
STYLE = """
QWidget {font-family:'Microsoft YaHei UI';font-size:13px;color:#192331}
QMainWindow,#workspace {background:#f3f5f8}
#masthead {background:#fff;border-bottom:1px solid #e0e5ec}
#brand {color:#b41c34;font-size:24px;font-weight:700}
#masthead QLabel {background:transparent}
#marketScope {color:#626c7b;font-size:12px}
QPushButton#topNav {background:transparent;border:0;border-radius:0;border-bottom:2px solid transparent;
    padding:0 16px;color:#515d6e;font-weight:600}
QPushButton#topNav:hover {background:#f7f8fa;color:#263347}
QPushButton#topNav:checked {background:#fff2f4;border-bottom-color:#bd2138;color:#ac1931}
#headline {font-size:28px;font-weight:700;color:#152034}
#pageMeta,#muted {font-size:12px;color:#687180}
#surface {background:#fff;border:1px solid #e0e5ec;border-radius:10px}
#sectionTitle {font-size:16px;font-weight:700;color:#1c283b}
#accountKey {font-size:12px;color:#627084}
#accountValue {font-family:'Segoe UI','Microsoft YaHei UI';font-size:15px;font-weight:600;color:#203049}
#breadthName {font-size:12px;color:#657287}
#breadthValue {font-family:'Segoe UI';font-size:18px;font-weight:600}
#stockTitle {font-size:22px;font-weight:700;color:#192539}
#stockPrice {font-family:'Segoe UI';font-size:30px;font-weight:600;color:#192539}
#stockMeta {font-size:12px;color:#627084}
#decisionDetail {background:#faf3f5;border:1px solid #efd7dd;border-radius:6px;
    padding:6px 9px;color:#632438}
#reviewOverview {background:#f4f6f9;border-radius:6px;padding:8px 10px;color:#4a5970}
#footer {background:#fff;border-top:1px solid #e0e5ec}
QLineEdit,QComboBox,QDateEdit,QPlainTextEdit {
    background:#fff;border:1px solid #d9e0e9;border-radius:6px;padding:5px 8px;
    selection-background-color:#f4d1d8;selection-color:#451822
}
QLineEdit:focus,QComboBox:focus,QDateEdit:focus,QPlainTextEdit:focus {border-color:#bc2338}
QAbstractItemView {selection-background-color:#fbe9ed;selection-color:#702237;alternate-background-color:#f8fafc}
QPushButton {background:#fff;border:1px solid #d9e0e9;border-radius:6px;padding:5px 11px;color:#364257}
QPushButton:hover {background:#f5f7fb;border-color:#c7d1de}
QPushButton:pressed {background:#e9edf3}
QPushButton#stockQuery {background:#bd2138;color:#fff;border-color:#bd2138;font-weight:700}
QPushButton#stockQuery:hover {background:#a9192f;border-color:#a9192f}
QPushButton#stockQuery:pressed {background:#8c1729;border-color:#8c1729}
QTableView {background:#fff;border:0;outline:0}
QHeaderView {background:#f3f5f8}
QHeaderView::section {background:#f3f5f8;color:#5c687a;border:0;border-bottom:1px solid #e9edf2;padding:5px 8px;font-size:12px}
QScrollBar:vertical {background:#f4f6fa;width:8px;margin:0}
QScrollBar::handle:vertical {background:#c6cfdb;border-radius:3px;min-height:22px}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0}
QToolTip {background:#222a3a;color:#fff;border:1px solid #3c495e;padding:5px}
QSplitter::handle {background:transparent}
QSplitter::handle:hover {background:#e8c9d0}
"""
def label(text="", name=""):
    obj=W.QLabel(text);obj.setObjectName(name);return obj

def panel():
    frame=W.QFrame()
    frame.setObjectName("surface")
    layout=W.QVBoxLayout(frame)
    layout.setContentsMargins(13,12,13,10)
    layout.setSpacing(8)
    return frame,layout


class Rows(C.QAbstractTableModel):
    def __init__(self,headers):
        super().__init__()
        self.headers=headers
        self.rows=[]
        self.right_aligned={i for i,h in enumerate(headers) if h in (
            "最新价", "涨跌幅", "价格", "成交价", "股数", "费用", "现金可买(股)", "延迟(秒)"
        )}

    def replace(self,rows):
        if rows==self.rows:
            return
        self.beginResetModel()
        self.rows=rows
        self.endResetModel()

    def rowCount(self,parent=C.QModelIndex()):
        return len(self.rows)

    def columnCount(self,parent=C.QModelIndex()):
        return len(self.headers)

    def headerData(self,i,orientation,role):
        if orientation==C.Qt.Horizontal:
            if role==C.Qt.DisplayRole:
                return self.headers[i]
            if role==C.Qt.TextAlignmentRole:
                return int(self.alignment(i))

    def alignment(self,column):
        if column in self.right_aligned:
            return C.Qt.AlignRight | C.Qt.AlignVCenter
        if self.headers[column] in ("代码", "名称", "简称", "股票简称", "触发原因"):
            return C.Qt.AlignLeft | C.Qt.AlignVCenter
        return C.Qt.AlignCenter

    def data(self,index,role):
        if not index.isValid():
            return None
        col=index.column()
        value=self.rows[index.row()][col]
        if role==C.Qt.DisplayRole:
            return value
        if role==C.Qt.TextAlignmentRole:
            return int(self.alignment(col))
        if role==C.Qt.ForegroundRole:
            if self.headers[col] in ("涨跌幅", "累计盈亏(元)") and isinstance(value,str) and value not in ("+0.00", "-0.00"):
                return G.QColor(UP if value.startswith("+") else DOWN if value.startswith("-") else MUTED)
            if self.headers[col]=="方向":
                return G.QColor(UP if value=="买入" else DOWN)
            if self.headers[col]=="代码":
                return G.QColor(MUTED)


class Table(W.QTableView):
    def __init__(self,empty):
        super().__init__()
        self.empty=empty

    def paintEvent(self,event):
        super().paintEvent(event)
        if self.model().rowCount()==0:
            painter=G.QPainter(self.viewport())
            painter.setPen(G.QColor(MUTED))
            painter.drawText(self.viewport().rect(),C.Qt.AlignCenter,self.empty)


def table(model,empty="暂无记录"):
    view=Table(empty)
    view.setModel(model)
    view.verticalHeader().hide()
    view.setSelectionBehavior(W.QAbstractItemView.SelectRows)
    view.setSelectionMode(W.QAbstractItemView.SingleSelection)
    view.setShowGrid(False)
    view.setAlternatingRowColors(True)
    view.setTextElideMode(C.Qt.ElideRight)
    view.horizontalHeader().setSectionResizeMode(W.QHeaderView.Stretch)
    view.horizontalHeader().setMinimumHeight(31)
    view.verticalHeader().setDefaultSectionSize(30)
    return view


class Terminal(W.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("观市 · 沪深主板模拟终端")
        self.resize(1360,820)
        self.setMinimumSize(1000,660)
        self.review=Review(market.ROOT)
        self.lastreview=0
        self.init_accounts()
        self.bus=queue.Queue()
        self.stop=threading.Event()
        self.data={}
        self.names={}
        self.symbol="sz000711"
        self.loaded=self.symbol

        shell=W.QWidget()
        self.setCentralWidget(shell)
        base=W.QVBoxLayout(shell)
        base.setContentsMargins(0,0,0,0)
        base.setSpacing(0)
        masthead=W.QFrame()
        masthead.setObjectName("masthead")
        top=W.QHBoxLayout(masthead)
        top.setContentsMargins(19,0,19,0)
        top.setSpacing(0)
        brand=label("观市","brand")
        top.addWidget(brand)
        top.addSpacing(25)
        self.pages=W.QStackedWidget()
        self.nav=[]
        sections=("行情总览","信号记录","50 元以内","模拟账户","每日复盘")
        for i,title in enumerate(sections):
            button=W.QPushButton(title)
            button.setObjectName("topNav")
            button.setFixedHeight(53)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.clicked.connect(lambda checked,i=i:self.pages.setCurrentIndex(i))
            top.addWidget(button)
            self.nav.append(button)
        self.nav[0].setChecked(True)
        top.addStretch()
        top.addWidget(label("沪深主板  /  模拟交易","marketScope"))
        base.addWidget(masthead)

        workspace=W.QWidget()
        workspace.setObjectName("workspace")
        layout=W.QVBoxLayout(workspace)
        layout.setContentsMargins(18,13,18,13)
        layout.setSpacing(10)
        base.addWidget(workspace,1)

        header=W.QHBoxLayout()
        header.setSpacing(20)
        titlebox=W.QVBoxLayout()
        titlebox.setSpacing(2)
        self.heading=label(sections[0],"headline")
        titlebox.addWidget(self.heading)
        self.today=label("","pageMeta")
        titlebox.addWidget(self.today)
        header.addLayout(titlebox,1)
        self.pages.currentChanged.connect(lambda i:self.heading.setText(sections[i]))
        self.account_stats=[]
        for title in ("可用资金（元）","持仓浮盈亏（元）","今日成交"):
            stat=W.QVBoxLayout()
            stat.setSpacing(2)
            stat.addWidget(label(title,"accountKey"))
            value=label("—","accountValue")
            stat.addWidget(value)
            self.account_stats.append(value)
            header.addLayout(stat)
        layout.addLayout(header)
        layout.addWidget(self.pages,1)

        split=W.QSplitter()
        split.setChildrenCollapsible(False)
        split.setHandleWidth(10)
        self.pages.addWidget(split)
        left,quotes=panel()
        left.setMinimumWidth(383)
        split.addWidget(left)
        list_heading=W.QHBoxLayout()
        list_heading.addWidget(label("沪深主板","sectionTitle"))
        list_heading.addStretch()
        self.quote_count=label("等待行情","muted")
        list_heading.addWidget(self.quote_count)
        quotes.addLayout(list_heading)
        breadth=W.QHBoxLayout()
        breadth.setSpacing(8)
        self.nums=[]
        for title,color in (("报价","#223049"),("上涨",UP),("下跌",DOWN),("平盘",MUTED)):
            pair=W.QVBoxLayout()
            pair.setSpacing(1)
            pair.addWidget(label(title,"breadthName"))
            number=label("—","breadthValue")
            number.setStyleSheet("color:"+color)
            pair.addWidget(number)
            breadth.addLayout(pair,1)
            self.nums.append(number)
        quotes.addLayout(breadth)
        filters=W.QHBoxLayout()
        filters.setSpacing(6)
        self.search=W.QLineEdit()
        self.search.setPlaceholderText("代码 / 股票简称")
        self.search.setClearButtonEnabled(True)
        self.scope=W.QComboBox()
        self.scope.addItems(["全部主板","沪市主板","深市主板"])
        filters.addWidget(self.search,1)
        filters.addWidget(self.scope)
        inspect=W.QPushButton("定位")
        inspect.clicked.connect(self.lookup)
        self.search.returnPressed.connect(self.lookup)
        filters.addWidget(inspect)
        quotes.addLayout(filters)
        self.model=Rows(["代码","名称","最新价","涨跌幅","报价时间"])
        self.list=table(self.model,"暂无符合条件的行情")
        self.list.horizontalHeader().setSectionResizeMode(1,W.QHeaderView.Stretch)
        for col,width in ((0,65),(2,63),(3,68),(4,77)):
            self.list.horizontalHeader().setSectionResizeMode(col,W.QHeaderView.Fixed)
            self.list.setColumnWidth(col,width)
        quotes.addWidget(self.list,1)
        self.list.clicked.connect(self.select)
        self.list.activated.connect(self.select)
        self.search.textChanged.connect(self.refresh)
        self.scope.currentIndexChanged.connect(self.refresh)

        hero,h=panel()
        split.addWidget(hero)
        h.setContentsMargins(11,11,11,10)
        self.inspector=Inspector(self,market)
        h.addWidget(self.inspector)
        split.setStretchFactor(0,1)
        split.setStretchFactor(1,2)
        split.setSizes([442,846])

        signals,sl=panel()
        sl.addWidget(label("策略触发记录","sectionTitle"))
        self.events=Rows(["交易日","行情时间","代码","简称","信号","价格"])
        sl.addWidget(table(self.events),1)
        self.pages.addWidget(signals)

        cheap,cl=panel()
        cl.addWidget(label("最新报价 ≤ 50.00 元","sectionTitle"))
        cl.addWidget(label("按涨跌幅排序，现金可买股数已扣除预计佣金。","muted"))
        self.cheap=Rows(["代码","名称","最新价","涨跌幅","报价时间","现金可买(股)"])
        affordable=table(self.cheap)
        cl.addWidget(affordable,1)
        self.pages.addWidget(cheap)
        def opencheap(idx):
            self.symbol=self.lowcodes[idx.row()]
            self.loaded=None
            self.nav[0].click()
            self.display()
        affordable.doubleClicked.connect(opencheap)

        account,al=panel()
        controls=W.QHBoxLayout()
        controls.addWidget(label("交易账户","sectionTitle"))
        self.accounts=W.QComboBox()
        controls.addWidget(self.accounts)
        for number,amount in self.registry.execute("SELECT id,capital FROM profiles ORDER BY id"):
            self.accounts.addItem(f"账户 {number} · 本金 {amount/100:,.2f} 元",number)
        self.accounts.setCurrentIndex(self.accounts.findData(self.active_id))
        self.accounts.currentIndexChanged.connect(self.switch_account)
        controls.addStretch()
        create=W.QPushButton("新建模拟账户")
        create.setObjectName("stockQuery")
        controls.addWidget(create)
        create.clicked.connect(self.new_account)
        al.addLayout(controls)
        self.balance=label("","sectionTitle")
        self.position=label("","pageMeta")
        self.position.setWordWrap(True)
        al.addWidget(self.balance)
        al.addWidget(self.position)
        rules=label("仅所选账户接收新交易。沪深主板单仓，连续确认后按后续卖一买入。次日起跌 3% 或涨 6% 触发退出，仍持仓则最早次日 14:50 按后续买一退出。佣金万五，最低 5 元，卖出另计万五。","muted")
        rules.setWordWrap(True)
        al.addWidget(rules)
        al.addWidget(label("成交明细","sectionTitle"))
        self.ledger=Rows(["报价时间","代码","股票简称","方向","成交价","股数","费用","触发原因"])
        al.addWidget(table(self.ledger),1)
        self.pages.addWidget(account)
        self.show_account()

        review,rl=panel()
        datebar=W.QHBoxLayout()
        datebar.addWidget(label("复盘记录","sectionTitle"))
        datebar.addStretch()
        self.reviewdate=W.QDateEdit(C.QDate.currentDate())
        self.reviewdate.setCalendarPopup(True)
        self.reviewdate.setDisplayFormat("yyyy-MM-dd")
        self.category=W.QComboBox()
        self.category.addItems(["全部","本账户已成交","盘中未成交","盘后或超时"])
        datebar.addWidget(self.reviewdate)
        datebar.addWidget(self.category)
        rl.addLayout(datebar)
        self.cover=label("","reviewOverview")
        self.cover.setWordWrap(True)
        rl.addWidget(self.cover)
        self.summary=Rows(["对照组","信号","及时发现","买入","买入/及时","累计盈亏(元)","最大回撤"])
        summary=table(self.summary)
        summary.setMaximumHeight(100)
        rl.addWidget(summary)
        self.experiment=label("","muted")
        self.experiment.setWordWrap(True)
        self.experiment.setToolTip("首分钟突破与连续确认对照组各用 5000 元。费用、盘口和退出规则相同。按实际收到的报价计算截至所选日的累计值。")
        rl.addWidget(self.experiment)
        review_heading=W.QHBoxLayout()
        review_heading.addWidget(label("信号明细","sectionTitle"))
        review_heading.addStretch()
        review_heading.addWidget(label("双击记录查看当日分时","muted"))
        rl.addLayout(review_heading)
        self.reviewrows=Rows(["行情分钟","代码","规则","首次发现","延迟(秒)","对照账户","主账户"])
        records=table(self.reviewrows)
        records.doubleClicked.connect(self.open_review)
        rl.addWidget(records,1)
        self.pages.addWidget(review)
        self.reviewdate.dateChanged.connect(self.show_review)
        self.category.currentIndexChanged.connect(self.show_review)
        self.pages.currentChanged.connect(self.show_review)

        footer=W.QFrame()
        footer.setObjectName("footer")
        statusbar=W.QHBoxLayout(footer)
        statusbar.setContentsMargins(18,5,18,5)
        self.status=label("连接行情源…","muted")
        self.progress=label("","muted")
        statusbar.addWidget(self.status,1)
        statusbar.addWidget(self.progress)
        base.addWidget(footer)

        self.timer=C.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(400)
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
        unrealized=(pos["mark"]-pos["price"])*pos["shares"]/100 if pos else 0
        self.today.setText(f"模拟账户 {self.active_id}　·　本金 {s.get('capital',500000)/100:,.2f} 元")
        cash, pnl, count=self.account_stats
        cash.setText(f"{s['cash']/100:,.2f}")
        pnl.setText(f"{unrealized:+,.2f}")
        pnl.setStyleSheet("color:"+(UP if unrealized>0 else DOWN if unrealized<0 else MUTED))
        count.setText(f"{len(fills)} 笔")
        count.setToolTip(trades)
        self.ledger.replace([(t[5:19].replace('T',' '),sym,self.names.get(sym,'简称待同步'),side,f"{price/100:.2f}",str(qty),f"{fee/100:.2f}",reason) for t,sym,side,price,qty,fee,reason in self.account.db.execute("SELECT stamp,symbol,side,price,shares,fee,reason FROM fills ORDER BY id DESC LIMIT 500")])
    def select(self,idx):
        self.symbol=self.visible[idx.row()];self.loaded=None;self.display()
    def refresh(self,*args):
        text=self.search.text().strip().lower();prefix=["","sh","sz"][self.scope.currentIndex()]
        self.visible=[s for s in self.order if s.startswith(prefix) and (text in s or text in self.names[s])] if self.data else []
        rows=[]
        for s in self.visible:
            price,change,t,_=self.data[s][:4];rows.append((s[2:],self.names[s],f"{price:.2f}",f"{change:+.2f}%" if change else "0.00%",t.strftime("%H:%M:%S")))
        self.quote_count.setText(f"{len(rows):,} 只 · 涨跌幅排序" if self.data else "等待行情")
        offset=self.list.verticalScrollBar().value()
        self.model.replace(rows)
        if self.symbol in self.visible:
            self.list.selectRow(self.visible.index(self.symbol))
        self.list.verticalScrollBar().setValue(offset)
    def lookup(self):
        query=self.search.text().strip()
        if query and len(getattr(self,"visible",[]))==1:
            self.symbol=self.visible[0]
            self.loaded=None
            self.display()
            return
        if query:
            self.inspector.code.setText(query)
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
        self.experiment.setText(self.review.experiments.summary(day).replace("　|　","\n"))
        post,online,unknown=self.review.db.execute("""SELECT count(CASE WHEN substr(found,12,5)>='15:00' THEN 1 END),
        count(CASE WHEN substr(found,12,5)<'15:00' AND c.hm IS NOT NULL THEN 1 END),
        count(CASE WHEN substr(found,12,5)<'15:00' AND c.hm IS NULL THEN 1 END)
        FROM signals s LEFT JOIN coverage c ON c.day=s.day AND c.hm=s.hm WHERE s.day=? AND s.outcome='盘后或超时发现'""",(day,)).fetchone()
        progress=self.review.db.execute("SELECT total,done,success FROM progress WHERE day=?",(day,)).fetchone()
        progress_text=f"收盘复核 {progress[1]}/{progress[0]} 只 · 待查 {progress[0]-progress[1]} · 读取失败 {progress[1]-progress[2]} 只" if progress else "本日暂无收盘复核进度"
        self.cover.setWordWrap(True)
        self.cover.setText(
            f"{progress_text}　·　在线记录 {coverage[0]} 分钟　·　新鲜报价 {coverage[1]} 分钟\n"
            f"收盘后发现 {post} 条　·　其余延迟中，信号分钟在线 {online} 条、覆盖未知 {unknown} 条"
        )
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
                self.refresh_cheap();self.refresh();self.display();self.inspector.update_quote()
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

