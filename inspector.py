"""Single-stock intraday review. Prices are yuan and source volume is cumulative lots."""
import datetime as dt
import json
import queue
import threading
from pathlib import Path

from PyQt5 import QtCore as C, QtGui as G, QtWidgets as W

PRICE_LINE = "#ff6978"
VWAP_LINE = "#edbd65"
CHART_BG = "#161a22"
CHART_GRID = "#2a303d"
CHART_TEXT = "#a7aebd"


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
    clicked = C.pyqtSignal(str)
    AXIS_LABELS = ((0, "09:30"), (60, "10:30"), (120, "11:30 / 13:00"),
                   (180, "14:00"), (240, "15:00"))

    def __init__(self):
        super().__init__()
        self._rows = []
        self.marks = {}
        self.hovered = None
        self.selected = None
        self.setMinimumHeight(110)
        self.setMouseTracking(True)
        self.setCursor(C.Qt.CrossCursor)
        self.setAccessibleName("分时走势与成交量")

    @property
    def rows(self):
        return self._rows

    @rows.setter
    def rows(self, rows):
        self._rows = rows
        self.hovered = self.selected = None

    def horizontal_bounds(self):
        return 14, max(15, self.width() - 54)

    def plot_x(self, offset, afternoon=False):
        left, right = self.horizontal_bounds()
        lunch_gap = 8
        return left + (right-left-lunch_gap) * offset/240 + (lunch_gap if afternoon else 0)

    def row_x(self, row):
        return self.plot_x(row[1], row[0]>="1300")

    def nearest_row(self, x):
        return min(self.rows, key=lambda row: abs(self.row_x(row)-x), default=None)

    def mouseMoveEvent(self, event):
        row = self.nearest_row(event.x())
        if row != self.hovered:
            self.hovered = row
            self.update()

    def leaveEvent(self, event):
        self.hovered = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == C.Qt.LeftButton:
            self.selected = self.nearest_row(event.x())
            if self.selected:
                self.update()
                self.clicked.emit(self.selected[0])

    def price_paths(self, column, y_at, floor):
        path = G.QPainterPath()
        area = G.QPainterPath()
        previous = None
        for row in self.rows:
            value = row[column]
            if value is None:
                previous = None
                continue
            x, y = self.row_x(row), y_at(value)
            consecutive = previous and row[1]-previous[0][1] <= 1
            same_session = previous and (row[0] >= "1300") == (previous[0][0] >= "1300")
            if consecutive and same_session:
                _, px, py = previous
                path.lineTo(x, y)
                if column==2:
                    area.moveTo(px, floor)
                    area.lineTo(px, py)
                    area.lineTo(x, y)
                    area.lineTo(x, floor)
                    area.closeSubpath()
            else:
                path.moveTo(x, y)
            previous = row, x, y
        return path, area

    def draw_grid(self, painter, top, floor, base, lo, hi, active_x, price_y):
        left, right = self.horizontal_bounds()
        metrics = painter.fontMetrics()
        for fraction in (0, .5, 1) if floor-top < 64 else (0, 1/3, 2/3, 1):
            y = round(top+(floor-top)*fraction)
            painter.setPen(G.QColor(CHART_GRID))
            painter.drawLine(left, y, right, y)
            if abs(price_y-y) > 13:
                painter.setPen(G.QColor(CHART_TEXT))
                painter.drawText(right+7, y+4, f"{hi-(hi-lo)*fraction:.2f}")
        for offset, label in self.AXIS_LABELS:
            x = round(self.plot_x(offset, offset>120) + (4 if offset==120 else 0))
            painter.setPen(G.QColor(CHART_GRID))
            painter.drawLine(x, top, x, base)
            if offset in (0,120,240) or self.width()>=420:
                text_width = metrics.horizontalAdvance(label)
                text_x = max(left, min(right-text_width, x-text_width//2))
                if active_x is None or not text_x-20 <= active_x <= text_x+text_width+20:
                    painter.setPen(G.QColor(CHART_TEXT))
                    painter.drawText(text_x, self.height()-7, label)
        painter.setPen(G.QColor(CHART_GRID))
        painter.drawLine(left, base, right, base)

    def draw_badge(self, painter, text, x, y, color):
        width = painter.fontMetrics().horizontalAdvance(text)+10
        x = max(3, min(self.width()-width-3, round(x-width/2)))
        box = C.QRectF(x, round(y), width, 18)
        painter.setPen(C.Qt.NoPen)
        painter.setBrush(G.QColor("#303847"))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(G.QColor(color))
        painter.drawText(box, C.Qt.AlignCenter, text)

    def draw_readout(self, painter, row):
        hm, _, price, average, volume = row
        values = (
            (f"{hm[:2]}:{hm[2:]}", CHART_TEXT),
            (f"分时 {price:.2f}", PRICE_LINE),
            (f"均价 {average:.2f}" if average is not None else "均价 —", VWAP_LINE),
            (f"量 {volume:,.0f} 手" if volume is not None else "量 —", CHART_TEXT),
        )
        x = self.horizontal_bounds()[0]
        for text, color in values:
            width = painter.fontMetrics().horizontalAdvance(text)
            if x+width <= self.width()-10:
                painter.setPen(G.QColor(color))
                painter.drawText(x, 17, text)
                x += width+12

    def paintEvent(self, event):
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.Antialiasing)
        painter.setPen(C.Qt.NoPen)
        painter.setBrush(G.QColor(CHART_BG))
        painter.drawRoundedRect(C.QRectF(self.rect()), 8, 8)
        painter.setPen(G.QColor(CHART_TEXT))
        if not self.rows:
            painter.drawText(self.rect(), C.Qt.AlignCenter, "暂无该日分时数据")
            return

        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        left, right = self.horizontal_bounds()
        top, base = 30, self.height()-24
        floor = top+max(10, round((base-top)*.73))
        prices = [value for row in self.rows for value in row[2:4] if value is not None]
        pad = max(.015, (max(prices)-min(prices))*.09)
        lo, hi = min(prices)-pad, max(prices)+pad
        def y_at(price):
            return floor-(price-lo)/(hi-lo)*(floor-top)
        active = self.hovered or self.selected
        readout = active or self.rows[-1]
        active_x = round(self.row_x(readout))
        active_y = round(y_at(readout[2]))
        self.draw_readout(painter, readout)
        self.draw_grid(painter, top, floor, base, lo, hi, active_x if active else None, active_y)

        peak = max([row[4] or 0 for row in self.rows]+[1])
        bar_width = max(1.0, (right-left)/350)
        previous = None
        for row in self.rows:
            price, volume = row[2], row[4]
            if volume:
                x = self.row_x(row)
                height = max(1, (base-floor-7)*volume/peak)
                color = "#39866a" if previous is not None and price<previous else "#a54b58"
                painter.fillRect(C.QRectF(x-bar_width/2, base-height, bar_width, height), G.QColor(color))
            previous = price

        price_path, area = self.price_paths(2, y_at, floor)
        gradient = G.QLinearGradient(0, top, 0, floor)
        gradient.setColorAt(0, G.QColor(255, 105, 120, 47))
        gradient.setColorAt(1, G.QColor(255, 105, 120, 2))
        painter.fillPath(area, gradient)
        average_path, _ = self.price_paths(3, y_at, floor)
        for path, color, stroke in ((average_path, VWAP_LINE, 1.15), (price_path, PRICE_LINE, 1.5)):
            painter.setPen(G.QPen(G.QColor(color), stroke))
            painter.drawPath(path)

        previous_marker = -50
        for row in self.rows:
            marker = self.marks.get(row[0])
            if marker:
                x, y = self.row_x(row), y_at(row[2])
                painter.setPen(C.Qt.NoPen)
                painter.setBrush(G.QColor(VWAP_LINE))
                painter.drawEllipse(C.QPointF(x, y), 2.5, 2.5)
                if x-previous_marker >= 25 and floor-top >= 45:
                    self.draw_badge(painter, marker, x, max(20, y-22), "#ffffff")
                    previous_marker = x

        hm, _, price, _, _ = readout
        if active:
            painter.setPen(G.QPen(G.QColor("#667284"), 1, C.Qt.DashLine))
            painter.drawLine(active_x, top, active_x, base)
            painter.drawLine(left, active_y, right, active_y)
            self.draw_badge(painter, f"{hm[:2]}:{hm[2:]}", active_x, self.height()-21, "#ffffff")
        painter.setPen(G.QPen(G.QColor(CHART_BG), 1))
        painter.setBrush(G.QColor(PRICE_LINE))
        painter.drawEllipse(C.QPointF(active_x, active_y), 3.4, 3.4)
        self.draw_badge(painter, f"{price:.2f}", (right+self.width())/2, active_y-9, "#ffffff")


class Inspector(W.QWidget):
    def __init__(self,terminal,market):
        super().__init__(terminal)
        self.setWindowTitle("查这只股 · 分时与决策")
        self.setMinimumWidth(440)
        self.terminal=terminal
        self.market=market
        self.symbol=None
        self.series_status=""
        layout=W.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(8)

        header=W.QHBoxLayout()
        identity=W.QVBoxLayout()
        identity.setSpacing(1)
        self.stock_name=W.QLabel("查这只股",objectName="stockTitle")
        self.stock_meta=W.QLabel("",objectName="stockMeta")
        identity.addWidget(self.stock_name)
        identity.addWidget(self.stock_meta)
        header.addLayout(identity,1)
        self.price=W.QLabel("—",objectName="stockPrice")
        self.price.setAlignment(C.Qt.AlignRight | C.Qt.AlignVCenter)
        self.change=W.QLabel("",objectName="stockMeta")
        header.addWidget(self.price)
        header.addWidget(self.change)
        layout.addLayout(header)

        controls=W.QHBoxLayout()
        controls.setSpacing(6)
        controls.addWidget(W.QLabel("分时走势",objectName="sectionTitle"))
        controls.addStretch()
        code=W.QLineEdit(terminal.symbol)
        code.setPlaceholderText("代码或股票简称")
        code.setAccessibleName("股票代码或简称")
        code.setMaximumWidth(160)
        date=W.QDateEdit(C.QDate.currentDate())
        date.setCalendarPopup(True)
        date.setDisplayFormat("yyyy-MM-dd")
        date.setAccessibleName("选择分时日期")
        button=W.QPushButton("查询")
        button.setObjectName("stockQuery")
        for widget in (code,date,button):
            controls.addWidget(widget)
        layout.addLayout(controls)
        plot=Plot()
        self.plot=plot
        layout.addWidget(plot,1)

        decision_header=W.QHBoxLayout()
        decision_header.addWidget(W.QLabel("决策与成交",objectName="sectionTitle"))
        decision_header.addStretch()
        decision_header.addWidget(W.QLabel("点击分时图复核该分钟",objectName="muted"))
        layout.addLayout(decision_header)
        log=W.QPlainTextEdit()
        log.setReadOnly(True)
        log.setMinimumHeight(50)
        log.setMaximumHeight(90)
        layout.addWidget(log)
        detail=W.QLabel("",objectName="decisionDetail")
        detail.setWordWrap(True)
        detail.hide()
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
            detail.show()
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
            plot.rows=[];plot.marks={};plot.update();log.clear();detail.clear();detail.hide();evidence.clear();previous_volumes.clear();today_volume.clear()
            if len(sym)!=8 or not market.main_board(sym):
                self.symbol=None
                self.series_status="请输入沪深主板股票代码或简称"
                self.update_quote()
                return
            self.symbol=sym
            self.series_status="正在读取分时"
            self.update_quote()
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
                inbox.put((token,status,rows,[volumes(d) for d in past],volumes(data or {})))
            threading.Thread(target=fetch,daemon=True).start()

        def receive():
            while not inbox.empty():
                token,status,rows,prior,volumes=inbox.get_nowait()
                if token==generation:
                    self.series_status=status
                    plot.rows=rows;previous_volumes[:]=prior;today_volume.update(volumes);plot.update()
                    self.update_quote()
        timer=C.QTimer(self);timer.timeout.connect(receive);timer.start(100)
        button.clicked.connect(search);code.returnPressed.connect(search)
        self.code,self.date,self.search=code,date,search
        refresh=C.QTimer(self);refresh.setInterval(60000);refresh.timeout.connect(lambda: search() if self.isVisible() and date.date()==C.QDate.currentDate() else None);refresh.start()
        date.dateChanged.connect(search)
        search()

    def update_quote(self):
        symbol=self.symbol
        if not symbol:
            self.stock_name.setText("查这只股")
            self.stock_meta.setText(self.series_status)
            self.price.setText("—")
            self.change.clear()
            return
        selected=self.date.date().toPyDate()
        today=dt.datetime.now(self.market.CN).date()
        quote=self.terminal.data.get(symbol)
        exchange="沪市" if symbol.startswith("sh") else "深市"
        self.stock_name.setText(self.terminal.names.get(symbol,symbol[2:]))
        self.stock_meta.setText(f"{symbol[2:]}　·　{exchange}主板　·　{self.series_status}")
        color="#627084"
        if selected==today and quote and quote[2].date()==today:
            self.price.setText(f"{quote[0]:.2f}")
            change=quote[1]
            self.change.setText(f"{change:+.2f}%" if change else "0.00%")
            color="#bc2338" if change>0 else "#127653" if change<0 else color
        elif self.plot.rows:
            self.price.setText(f"{self.plot.rows[-1][2]:.2f}")
            self.change.setText("分时末价")
        else:
            self.price.setText("—")
            self.change.clear()
        self.change.setStyleSheet("color:"+color)

    def select_stock(self,symbol,date=None):
        self.code.setText(symbol)
        if date is not None:
            self.date.blockSignals(True);self.date.setDate(date);self.date.blockSignals(False)
        self.search()
