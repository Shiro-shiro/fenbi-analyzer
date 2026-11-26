# -*- coding: utf-8 -*-
""" 粉笔刷题分析器 GUI 主程序（PySide6 + Fluent 风格）v3.0 界面改版版 """
import os, sys, json, time, threading, traceback, datetime, subprocess
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QGridLayout,
    QFileDialog, QMessageBox, QDialog, QTableWidgetItem, QHeaderView,
    QScrollArea,
    QAbstractItemView, QSizePolicy, QTreeWidgetItem,
)
from PySide6.QtCore import Qt, Signal, QObject, QThread, QSize, QTimer
from PySide6.QtGui import QFont, QPixmap, QDesktopServices, QColor, QPainter
from PySide6.QtCore import QUrl, QRect, QRectF

from qfluentwidgets import (
    MSFluentWindow, FluentIcon, Theme, setTheme, setThemeColor, NavigationItemPosition,
    PrimaryPushButton, PushButton, SimpleCardWidget, HeaderCardWidget,
    TableWidget, TreeWidget, ComboBox, SpinBox, DoubleSpinBox,
    CheckBox, ProgressBar, PlainTextEdit, TextBrowser, SubtitleLabel, BodyLabel,
    CaptionLabel, StrongBodyLabel, VerticalSeparator, NavigationBarPushButton,
)

import core, login, settings, exporter

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
ACCENT = '#4F6BED'


class CenterNavButton(NavigationBarPushButton):
    """ 原版图标固定画在 x=22（按 64px 宽设计），加宽后图标偏左。
        这里把图标与文字都按控件实际宽度居中。 """
    def _drawIcon(self, painter):
        from qfluentwidgets.common.style_sheet import isDarkTheme
        from qfluentwidgets.common.icon import drawIcon, FluentIconBase
        from qfluentwidgets.common.color import autoFallbackThemeColor
        if (self.isPressed or not self.isEnter) and not (self.isSelected or self.isAboutSelected):
            painter.setOpacity(0.6)
        if not self.isEnabled():
            painter.setOpacity(0.4)
        size = 20
        x = (self.width() - size) / 2.0
        y = 13 if self._isSelectedTextVisible else 13 + self.iconAni.offset
        rect = QRectF(x, y, size, size)
        selectedIcon = self._selectedIcon or self._icon
        if isinstance(selectedIcon, FluentIconBase) and (self.isSelected or self.isAboutSelected):
            color = autoFallbackThemeColor(self.lightSelectedColor, self.darkSelectedColor)
            selectedIcon.render(painter, rect, fill=color.name())
        elif self.isSelected or self.isAboutSelected:
            drawIcon(selectedIcon, painter, rect)
        else:
            drawIcon(self._icon, painter, rect)

    def _drawText(self, painter):
        from qfluentwidgets.common.style_sheet import isDarkTheme
        from qfluentwidgets.common.color import autoFallbackThemeColor
        if self.isSelected and not self._isSelectedTextVisible:
            return
        if self.isSelected or self.isAboutSelected:
            painter.setPen(autoFallbackThemeColor(self.lightSelectedColor, self.darkSelectedColor))
        else:
            painter.setPen(Qt.white if isDarkTheme() else Qt.black)
        painter.setFont(self.font())
        rect = QRect(0, 32, self.width(), 26)
        painter.drawText(rect, Qt.AlignCenter, self.text())


def apply_theme():
    """ 浅色主题 + 单一强调色。需在 QApplication 创建后调用。 """
    setTheme(Theme.LIGHT)
    setThemeColor(ACCENT)
    # 导航按钮换成居中版本（原版图标硬编码 x=22，加宽后会偏左）
    import qfluentwidgets.components.navigation.navigation_bar as _nb
    _nb.NavigationBarPushButton = CenterNavButton


class Signals(QObject):
    log = Signal(str)
    progress = Signal(int, int, str)
    done = Signal(object, object, object)   # rows, ranking, summary
    error = Signal(str)
    qr_ready = Signal(str)
    qr_status = Signal(str)
    qr_ok = Signal(str)                     # 扫码登录成功，参数=提示文字
    login_checked = Signal(bool, object)    # 登录态后台校验结果


class LoginCheck(QThread):
    """ 后台校验登录态，避免启动时卡在网络请求上 """
    def __init__(self, parent, cookie):
        super().__init__(parent)
        self.s = parent.sig; self.cookie = cookie

    def run(self):
        try:
            ok, info = login.cookie_ok(self.cookie)
        except Exception:
            ok, info = False, None
        self.s.login_checked.emit(ok, info)


class Worker(QThread):
    def __init__(self, parent, cookie, kinds, days, targets, exam_tree, weights):
        super().__init__(parent)
        self.s = parent.sig; self.cookie = cookie; self.kinds = kinds
        self.days = days; self.targets = targets; self.exam_tree = exam_tree
        self.weights = weights  # {二级分类: 权重小数}，来自固定模考权重设置
        self.cancel = threading.Event()

    def run(self):
        try:
            client = core.FenbiClient(self.cookie, self.cancel, lambda s: self.s.log.emit(s))
            crawler = core.Crawler(client, DATA_DIR,
                                   log=lambda s: self.s.log.emit(s), cancel=self.cancel,
                                   progress=lambda d, t, m: self.s.progress.emit(d, t, m))
            rows = crawler.run(self.kinds, self.days, exam_tree=self.exam_tree)
            rank = core.build_ranking(rows, self.targets, mock_weights=self.weights)
            summary = dict(crawler.summary)
            summary.update(mock_weight_note='固定权重：2027国考行政执法类模考第40/41/42/43季4份卷各二级分类平均出现题数/总题数130；可在设置页修改。',
                           full_mock_papers=4)
            self.s.log.emit('题量权重：' + summary['mock_weight_note'])
            self.s.done.emit(rows, rank, summary)
        except core.Cancelled:
            self.s.log.emit('已取消')
        except core.LoginExpired as e:
            self.s.error.emit('登录已失效：请到「登录」页重新登录。\n%s' % e)
        except Exception as e:
            self.s.error.emit('%s\n%s' % (e, traceback.format_exc()[-800:]))


class QRWorker(QThread):
    def __init__(self, parent, obj):
        super().__init__(parent); self.s = parent.sig; self.obj = obj

    def run(self):
        try:
            self.obj.start()
            self.s.qr_ready.emit(self.obj.qr_url)
            self.obj.poll()
            self.s.qr_ok.emit('登录成功')
        except Exception as e:
            self.s.qr_status.emit('登录失败：%s' % e)


class QrDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent); self.setWindowTitle('扫码登录粉笔'); self.resize(380, 500)
        self.sig = parent.sig; self.obj = None; self.worker = None
        lay = QVBoxLayout(self); lay.setContentsMargins(20, 20, 20, 20); lay.setSpacing(12)
        self.lbl = SubtitleLabel('正在获取二维码…'); self.lbl.setAlignment(Qt.AlignCenter)
        self.img = QLabel(); self.img.setAlignment(Qt.AlignCenter)
        self.status = BodyLabel('请用粉笔APP扫码，并在手机上确认'); self.status.setAlignment(Qt.AlignCenter)
        self.btn = PushButton('取消'); self.btn.clicked.connect(self.reject)
        lay.addWidget(self.lbl); lay.addWidget(self.img); lay.addWidget(self.status); lay.addWidget(self.btn)
        self.sig.qr_ready.connect(self.show_qr)
        self.sig.qr_status.connect(self.status.setText)
        self.sig.qr_ok.connect(self.on_success)

    def show_qr(self, url):
        try:
            p = os.path.join(DATA_DIR, 'qr.png')
            login.make_qr_image(url, p)
            self.img.setPixmap(QPixmap(p).scaled(320, 320, Qt.KeepAspectRatio))
            self.lbl.setText('扫码后请在手机点「确认登录」')
        except Exception as e:
            self.status.setText('二维码生成失败: %s' % e)

    def on_success(self, msg):
        """ 扫码成功：显示成功提示，短暂停留后自动关闭对话框 """
        self.lbl.setText('✓ 登录成功')
        self.status.setText('登录态已记住，窗口即将自动关闭…')
        self.btn.setText('关闭')
        self.img.clear()
        self.lbl.setStyleSheet('color:#0F7B0F;')      # 绿色成功色
        self.status.setStyleSheet('color:#0F7B0F;')
        self.btn.setEnabled(False)
        QTimer.singleShot(1200, self._finish)

    def _finish(self):
        try:
            self.sig.qr_ok.disconnect(self.on_success)
        except Exception:
            pass
        self.accept()

    def start(self):
        self.obj = login.QRLogin(on_status=lambda s: self.sig.qr_status.emit(s), cancel=threading.Event())
        self.worker = QRWorker(self.parent(), self.obj)
        self.worker.start()

    def closeEvent(self, e):
        if self.obj: self.obj.cancel.set()
        super().closeEvent(e)


class LogDialog(QDialog):
    """ 运行日志独立窗口：只在点「运行日志」时创建显示，主界面不再占用空间。 """
    def __init__(self, parent, edit):
        super().__init__(parent)
        self.setWindowTitle('运行日志')
        self.resize(880, 620)
        self.edit = edit
        self.edit.setParent(self)
        lay = QVBoxLayout(self); lay.setContentsMargins(16, 14, 16, 14); lay.setSpacing(10)
        tip = CaptionLabel('日志含请求状态、失败原因、字段缺口统计，可直接导出后发给他人排查。')
        lay.addWidget(tip)
        lay.addWidget(self.edit, 1)
        bar = QHBoxLayout(); bar.setSpacing(8)
        self.btn_export = PrimaryPushButton(FluentIcon.SAVE_AS, '导出日志')
        self.btn_export.clicked.connect(parent.export_log)
        self.btn_clear = PushButton(FluentIcon.DELETE, '清空')
        self.btn_clear.clicked.connect(parent.clear_log)
        self.btn_close = PushButton('关闭')
        self.btn_close.clicked.connect(self.close)
        bar.addWidget(self.btn_export); bar.addWidget(self.btn_clear)
        bar.addStretch(); bar.addWidget(self.btn_close)
        lay.addLayout(bar)

    def closeEvent(self, e):
        """ 关闭时把日志控件还给主窗口，避免被对话框一起销毁。 """
        self.edit.setParent(None)
        super().closeEvent(e)


class MainWindow(MSFluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('粉笔刷题分析器')
        self.resize(1120, 720)
        self.setMinimumSize(1000, 680)
        self.sig = Signals()
        self.sig.log.connect(self.on_log)
        self.sig.progress.connect(self.on_progress)
        self.sig.done.connect(self.on_done)
        self.sig.error.connect(self.on_error)
        self.worker = None
        self.rows = []; self.ranking = []; self.summary = {}
        self.targets = settings.load_settings()
        self.weights = settings.load_weights()
        self.sig.login_checked.connect(self.on_login_checked)
        self._login_worker = None
        self.log_dialog = None
        self.build_ui()
        # 登录态先显示本地信息，网络校验放后台（不阻塞窗口出现）
        self._show_login_state_local()
        self._check_login_async()

    # ---------- 界面骨架：左侧导航 + 顶部标题栏 ----------
    def build_ui(self):
        self.nav = self.navigationInterface

        self.page_main = self.main_tab(); self.page_main.setObjectName('page_main')
        # 抓取设置 + 目标与权重设置 合并为一页：顶部抓取设置，下方目标与权重
        self.page_settings = self.settings_page(); self.page_settings.setObjectName('page_settings')
        self._tree_built = False
        self.page_login = self.login_tab(); self.page_login.setObjectName('page_login')
        self.page_help = self.help_tab(); self.page_help.setObjectName('page_help')

        self.addSubInterface(self.page_main, FluentIcon.HOME, '榜单',
                             position=NavigationItemPosition.TOP)
        self.addSubInterface(self.page_settings, FluentIcon.FILTER, '抓取与目标',
                             position=NavigationItemPosition.TOP)
        self.addSubInterface(self.page_login, FluentIcon.PEOPLE, '登录',
                             position=NavigationItemPosition.TOP)
        self.addSubInterface(self.page_help, FluentIcon.HELP, '使用说明',
                             position=NavigationItemPosition.BOTTOM)
        # 导航项默认 64px 宽，长文字会被裁掉，按最长文字放宽
        for item in self.nav.items.values():
            item.setFixedSize(104, 58)
        self.nav.setFixedWidth(112)

        self.nav.setCurrentItem(self.page_main)
        self.stackedWidget.setCurrentWidget(self.page_main)
        # 权重表控件多（29 个二级分类 × 5 个输入框），首次进入会卡顿。
        # 改为：窗口出来后先让用户看到榜单页，再在后台分帧预建（每帧一个分组/一行，
        # 单帧最坏约 70ms，不阻塞交互），等用户真正点进来时已经建好。
        self.stackedWidget.currentChanged.connect(self._ensure_tree_built)
        QTimer.singleShot(400, self._ensure_tree_built)

        # 运行日志：不占主界面，改为独立弹窗（内容常驻内存，随时可看/导出）
        self.log = PlainTextEdit(); self.log.setReadOnly(True)
        self.log.setMinimumHeight(360)
        self.log.setStyleSheet('font-family:Consolas,"Microsoft YaHei";font-size:12px;')

    def show_log_dialog(self):
        if self.log_dialog is None or not self.log_dialog.isVisible():
            self.log_dialog = LogDialog(self, self.log)
        self.log_dialog.show(); self.log_dialog.raise_(); self.log_dialog.activateWindow()

    def export_log(self):
        text = self.log.toPlainText()
        if not text.strip():
            QMessageBox.information(self, '提示', '当前没有日志内容'); return
        default = '运行日志_%s.txt' % datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        p, _ = QFileDialog.getSaveFileName(self, '导出日志', default, '文本 (*.txt)')
        if not p: return
        try:
            head = ['粉笔刷题分析器 运行日志',
                    '导出时间：%s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    '程序目录：%s' % BASE,
                    '=' * 60, '']
            with open(p, 'w', encoding='utf-8') as f:
                f.write('\n'.join(head) + text + '\n')
            QMessageBox.information(self, '完成', '已导出：' + p)
        except Exception as e:
            QMessageBox.critical(self, '失败', '导出失败：%s' % e)

    def clear_log(self):
        if self.log.toPlainText().strip():
            if QMessageBox.question(self, '确认', '确定清空当前日志？') != QMessageBox.Yes:
                return
        self.log.clear()

    def _ensure_tree_built(self, *_):
        """ 建目标与权重表：窗口出来后由定时器在后台预建，用户点进该页时通常已建好。
            若用户来得比预建快，这里立刻把表格显示出来（行会继续逐行出现）。 """
        if self._tree_built:
            return
        self._tree_built = True
        self.tree_holder.addWidget(self.targets_panel(), 1)
        # 用户已经在这一页了：立刻显示表格（未建完时先显示"正在生成设置项…"与已建好的行）
        self._scroll.setVisible(True)

    # ---------- 榜单页 ----------
    def main_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 20, 24, 20); lay.setSpacing(14)

        # 操作区
        op = SimpleCardWidget(); opv = QVBoxLayout(op)
        opv.setContentsMargins(18, 14, 18, 14); opv.setSpacing(10)
        bar = QHBoxLayout(); bar.setSpacing(10)
        self.btn_fetch = PrimaryPushButton(FluentIcon.PLAY, '抓取并计算')
        self.btn_fetch.clicked.connect(self.start_fetch)
        self.btn_cancel = PushButton(FluentIcon.CANCEL, '取消抓取')
        self.btn_cancel.clicked.connect(self.cancel_fetch)
        self.btn_cancel.setEnabled(False)
        self.btn_xlsx = PushButton(FluentIcon.DOCUMENT, '导出Excel')
        self.btn_xlsx.clicked.connect(self.export_xlsx)
        self.btn_png = PushButton(FluentIcon.SAVE, '导出高清PNG')
        self.btn_png.clicked.connect(self.export_png)
        bar.addWidget(self.btn_fetch); bar.addWidget(self.btn_cancel)
        bar.addWidget(VerticalSeparator())
        bar.addWidget(self.btn_xlsx); bar.addWidget(self.btn_png)
        bar.addWidget(VerticalSeparator())
        self.btn_log = PushButton(FluentIcon.HISTORY, '运行日志')
        self.btn_log.clicked.connect(self.show_log_dialog)
        bar.addWidget(self.btn_log)
        bar.addStretch()
        opv.addLayout(bar)
        self.pbar = ProgressBar(); self.pbar.setValue(0)
        opv.addWidget(self.pbar)
        lay.addWidget(op)

        # 榜单表格
        card = SimpleCardWidget(); cv = QVBoxLayout(card)
        cv.setContentsMargins(18, 14, 18, 14); cv.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(SubtitleLabel('优先级榜单'))
        head.addStretch()
        head.addWidget(CaptionLabel('按优先级得分从高到低；只读'))
        cv.addLayout(head)

        self.table = TableWidget()
        self.table.setColumnCount(len(exporter.HEADERS))
        self.table.setHorizontalHeaderLabels(exporter.HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        # 固定列合计 693，题型列用 Stretch 吃掉剩余宽度（1120 窗口下约 230px，最小窗口 1000 时 110px）
        fixed = [140, 0, 85, 90, 90, 100, 100, 88]   # 0 = 题型列自动拉伸
        for ci in range(len(exporter.HEADERS)):
            if ci == 1:
                hh.setSectionResizeMode(ci, QHeaderView.Stretch)
            else:
                hh.setSectionResizeMode(ci, QHeaderView.Fixed)
                self.table.setColumnWidth(ci, fixed[ci])
        hh.setStretchLastSection(False)
        hh.setMinimumSectionSize(60)   # 窗口缩到最小也不让题型列塌成一条缝
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        cv.addWidget(self.table, 1)
        lay.addWidget(card, 1)
        return w

    # ---------- 抓取与目标（合并页：上=抓取设置，下=目标与权重） ----------
    def settings_page(self):
        """ 顶部抓取设置（紧凑单行），下方占满剩余空间的是目标与权重表。 """
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 16, 20, 16); lay.setSpacing(10)

        # —— 顶部：抓取设置（一行放完，不写说明文字）——
        card = HeaderCardWidget(); card.setTitle('抓取设置')
        row = QHBoxLayout(); row.setSpacing(8); row.setContentsMargins(0, 4, 0, 4)

        self.chk_special = CheckBox('专项'); self.chk_special.setChecked(True)
        self.chk_exam = CheckBox('模考'); self.chk_exam.setChecked(True)
        self.chk_recite = CheckBox('背题')
        for c in (self.chk_special, self.chk_exam, self.chk_recite):
            row.addWidget(c)

        row.addWidget(VerticalSeparator())
        self.spin_days = SpinBox(); self.spin_days.setRange(1, 3650); self.spin_days.setValue(30)
        self.spin_days.setFixedWidth(130); self.spin_days.setToolTip('最近多少天（含今天，从今天往回数）')
        self.chk_all = CheckBox('全部')
        self.chk_all.setToolTip('勾选则不限时间')
        self.chk_all.toggled.connect(lambda v: self.spin_days.setDisabled(v))
        row.addWidget(BodyLabel('最近')); row.addWidget(self.spin_days)
        row.addWidget(BodyLabel('天')); row.addWidget(self.chk_all)

        row.addWidget(VerticalSeparator())
        self.cmb_tree = ComboBox()
        # 注意：addItem 第二个参数是图标，数据必须用关键字 userData 传
        self.cmb_tree.addItem('国考·行政执法类', userData='guokao')
        self.cmb_tree.addItem('广西·B类', userData='guangxi')
        self.cmb_tree.setFixedWidth(180)
        self.cmb_tree.setToolTip('按所选考试的分类树归类')
        row.addWidget(BodyLabel('分类')); row.addWidget(self.cmb_tree)
        row.addStretch()
        card.viewLayout.addLayout(row)
        lay.addWidget(card)

        # —— 下方：目标与权重（占大部分界面）——
        head = QHBoxLayout(); head.setSpacing(8)
        head.addWidget(SubtitleLabel('目标与权重'))
        head.addWidget(CaptionLabel('分类 · 目标正确率 · 目标用时 · 每题分值 · 题量权重'))
        head.addStretch()
        self.btn_save_set = PrimaryPushButton(FluentIcon.SAVE, '保存')
        self.btn_save_set.clicked.connect(self.save_targets)
        self.btn_reset_set = PushButton(FluentIcon.SYNC, '恢复默认')
        self.btn_reset_set.clicked.connect(self.reset_targets)
        head.addWidget(self.btn_save_set); head.addWidget(self.btn_reset_set)
        lay.addLayout(head)

        self.tree_holder = QVBoxLayout(); self.tree_holder.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self.tree_holder, 1)
        return w

    # 设置项列宽（表头与数据行共用，保证纵向对齐；已按实际渲染调够，避免数字被截断，
    # 同时保证总宽不超过 1120 窗口下的可用宽度，右侧“权重比值”不被挤出）
    COL_W = dict(name=112, acc=144, tm=132, sc=144, cnt=142, tot=134, ratio=68)

    def targets_panel(self):
        """ 目标与权重表：按 一级 → 二级 分组，每个二级分类一行。
            只含表格本体，不含标题/按钮（那些在 settings_page 里）。
            用 QGridLayout 固定列宽，保证表头与数据严格对齐。
            控件数量多（29 个二级分类 × 5 个输入框），逐行分帧构建，
            否则首次进入该页会卡住 1.5 秒以上。 """
        w0 = self.COL_W
        holder = QWidget(); g = QGridLayout(holder)
        g.setContentsMargins(4, 4, 8, 4)
        g.setHorizontalSpacing(5); g.setVerticalSpacing(2)
        cols = ['name', 'acc', 'tm', 'sc', 'cnt', 'slash', 'tot', 'ratio']
        widths = dict(w0); widths['slash'] = 10
        # 固定总宽：否则 QScrollArea(widgetResizable) 会把多余宽度均摊到各列，导致列错位
        total_w = sum(widths[k] for k in cols) + 5 * (len(cols) - 1) + 12
        holder.setFixedWidth(total_w)
        for ci, key in enumerate(cols):
            g.setColumnMinimumWidth(ci, widths[key])
            g.setColumnStretch(ci, 0)

        # —— 表头 ——
        for ci, (text, key) in enumerate((('分类', 'name'), ('目标正确率', 'acc'), ('目标用时', 'tm'),
                                          ('每题分值', 'sc'), ('平均出现题数', 'cnt'),
                                          ('', 'slash'), ('总题数', 'tot'), ('权重比值', 'ratio'))):
            lab = CaptionLabel(text) if text else QLabel('')
            lab.setFixedWidth(widths[key])
            if key in ('acc', 'tm', 'sc', 'cnt', 'tot', 'ratio'):
                lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            g.addWidget(lab, 0, ci)
        sep = QWidget(); sep.setFixedHeight(1); sep.setStyleSheet('background:#E5E5E5;')
        g.addWidget(sep, 1, 0, 1, len(cols) + 1)

        self.edits = {}; self.weight_edits = {}
        self._g = g; self._cols = cols; self._row = 2
        self._holder = holder
        self._loading = CaptionLabel('正在生成设置项…')
        self._loading.setStyleSheet('color:#888;')
        g.addWidget(self._loading, 2, 0, 1, len(cols))
        self._pairs = self._category_pairs()
        self._built = False
        # 构建队列：分组标题与数据行按顺序入队，之后分帧构建（每帧约 12ms）
        self._queue = []
        for l1 in dict.fromkeys(a for a, _ in self._pairs):
            self._queue.append(('group', l1, None))
            for a, b in self._pairs:
                if a == l1:
                    self._queue.append(('row', l1, b))
        self._first_group = True
        # 构建期间关闭重绘，避免每加一行就整表重排；完成后统一刷新一次
        holder.setUpdatesEnabled(False)
        QTimer.singleShot(0, self._build_chunk)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(holder)
        self._scroll = scroll
        # 预建期间先不显示表格本体：否则表格一旦进入可见布局，
        # 会把 29 行一次性排版（实测约 0.4s），这笔开销会落在用户点进该页的瞬间。
        # 建完后由 _build_chunk 打开；若用户在预建完成前就点进来，
        # _ensure_tree_built 会立刻打开，让他看着行一行行长出来。
        scroll.setVisible(False)
        return scroll

    def _build_chunk(self):
        """ 分帧构建：每帧只处理一个队列项（一个分组标题或一个二级分类行），
            界面保持响应。构建期间 holder 关闭重绘，完成后统一 updateGeometry + update。 """
        if getattr(self, '_built', False):
            return
        g = self._g; cols = self._cols; holder = self._holder
        r = self._row
        if self._queue:
            kind, l1, b = self._queue.pop(0)
            if kind == 'group':
                if not self._first_group:
                    sp = QWidget(); sp.setFixedHeight(6)
                    g.addWidget(sp, r, 0, 1, len(cols)); r += 1
                self._first_group = False
                g.addWidget(StrongBodyLabel(l1), r, 0, 1, len(cols)); r += 1
            else:
                self._build_row(g, r, b)
                r += 1
            self._row = r
        if self._queue:
            QTimer.singleShot(0, self._build_chunk)
            return
        if getattr(self, '_loading', None) is not None:
            g.removeWidget(self._loading); self._loading.deleteLater(); self._loading = None
        g.setRowStretch(r, 1)
        self._built = True
        holder.setUpdatesEnabled(True)
        holder.updateGeometry(); holder.update()
        # 建完立刻打开：此时用户通常还在榜单页，这次首屏排版的开销落在后台，
        # 等他点进该页时表格已经有尺寸，切换耗时与其它页相当。
        self._scroll.setVisible(True)

    def _build_row(self, g, r, b):
        """ 构建一个二级分类的设置行。 """
        w0 = self.COL_W
        tg = self.targets.get(b, {})
        name = BodyLabel(b); name.setFixedWidth(w0['name'])
        acc = DoubleSpinBox(); acc.setRange(0, 100); acc.setDecimals(1)
        acc.setValue(float(tg.get('target_acc', 80))); acc.setSuffix('%')
        acc.setFixedWidth(w0['acc']); acc.setToolTip('目标正确率（%）')
        tm = SpinBox(); tm.setRange(1, 999); tm.setValue(int(tg.get('target_time', 60)))
        tm.setSuffix('秒'); tm.setFixedWidth(w0['tm']); tm.setToolTip('目标用时（秒/题）')
        sc = DoubleSpinBox(); sc.setRange(0.1, 5); sc.setDecimals(2)
        sc.setValue(float(tg.get('score', 0.8))); sc.setSuffix('分')
        sc.setFixedWidth(w0['sc']); sc.setToolTip('每题分值（分）')
        wv = self.weights.get(b, {})
        cnt = DoubleSpinBox(); cnt.setRange(0, 999999); cnt.setDecimals(2)
        cnt.setValue(float(wv.get('count', 0))); cnt.setFixedWidth(w0['cnt'])
        cnt.setToolTip('该题型在每份模考卷中的平均出现题数')
        slash = BodyLabel('/'); slash.setFixedWidth(10)
        tot = SpinBox(); tot.setRange(1, 999999); tot.setValue(int(wv.get('total', 130)))
        tot.setFixedWidth(w0['tot']); tot.setToolTip('每份模考卷的总题数')
        ratio = CaptionLabel(''); ratio.setFixedWidth(w0['ratio'])
        ratio.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for ci, wd in enumerate((name, acc, tm, sc, cnt, slash, tot, ratio)):
            g.addWidget(wd, r, ci)
        self.edits[b] = (acc, tm, sc)
        self.weight_edits[b] = (cnt, tot, ratio)
        cnt.valueChanged.connect(lambda *_: self._refresh_one(b))
        tot.valueChanged.connect(lambda *_: self._refresh_one(b))
        self._refresh_one(b)

    def _category_pairs(self):
        """ 一级→二级清单，来源 categories_guokao.json；附带 targets/weights 中有但树里没有的二级分类。 """
        path = os.path.join(DATA_DIR, 'categories_guokao.json')
        d = core.load_json(path, {}) or {}
        d = d.get('data') or d
        roots = d.get('baseKeypointVOS') or d.get('baseKeypoints') or []
        pairs = []
        for r in roots:
            l1 = r.get('name')
            for c in (r.get('children') or []):
                l2 = c.get('name')
                if l1 and l2: pairs.append((l1, l2))
        known = {b for _, b in pairs}
        for b in list(self.targets) + [k for k in self.weights if not k.startswith('_')]:
            if b not in known and b not in ('其他',):
                pairs.append(('(待补充：一级分类)', b))
        return pairs

    def _refresh_ratios(self):
        for cat in list(self.weight_edits):
            self._refresh_one(cat)

    def _refresh_one(self, cat):
        pair = self.weight_edits.get(cat)
        if not pair: return
        cnt, tot, ratio = pair
        t = tot.value()
        ratio.setText('= %.4f' % (cnt.value() / t) if t else '= 0')

    def save_targets(self):
        for b, (a, t, s) in self.edits.items():
            src = self.targets.get(b, {}).get('source', '')
            self.targets[b] = {'target_acc': a.value(), 'target_time': t.value(),
                               'score': s.value(), 'source': src}
        settings.save_settings(self.targets)
        for cat, (cnt, tot, ratio) in self.weight_edits.items():
            self.weights[cat] = {'count': cnt.value(), 'total': tot.value()}
        settings.save_weights(self.weights)
        self._refresh_ratios()
        QMessageBox.information(self, '完成', '设置已保存')

    def reset_targets(self):
        self.targets = settings.reset_settings()
        for b, (a, t, s) in self.edits.items():
            tg = self.targets.get(b, {})
            a.setValue(float(tg.get('target_acc', 80))); t.setValue(int(tg.get('target_time', 60)))
            s.setValue(float(tg.get('score', 0.8)))
        self.weights = settings.reset_weights()
        for cat, (cnt, tot, ratio) in self.weight_edits.items():
            v = self.weights.get(cat, {'count': 0, 'total': 130})
            cnt.setValue(float(v.get('count', 0))); tot.setValue(int(v.get('total', 130)))
        self._refresh_ratios()
        QMessageBox.information(self, '完成', '已恢复默认设置')

    # ---------- 登录页 ----------
    def login_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 20, 24, 20); lay.setSpacing(14)

        g = HeaderCardWidget(); g.setTitle('登录方式一：扫码登录（推荐）')
        v = QVBoxLayout(); v.setSpacing(10)
        v.addWidget(BodyLabel('点下面按钮，用粉笔APP扫码并在手机上确认。登录态会保存在本机。'))
        b = PrimaryPushButton(FluentIcon.QRCODE, '扫码登录'); b.clicked.connect(self.qr_login)
        v.addWidget(b, 0, Qt.AlignLeft)
        self.logout = PushButton(FluentIcon.DELETE, '清除已保存的登录态')
        self.logout.clicked.connect(self.clear_login)
        v.addWidget(self.logout, 0, Qt.AlignLeft)
        g.viewLayout.addLayout(v)
        lay.addWidget(g)

        g2 = HeaderCardWidget(); g2.setTitle('登录方式二：手动粘贴 Cookie（备用）')
        v2 = QVBoxLayout(); v2.setSpacing(10)
        v2.addWidget(BodyLabel('若扫码失败，可在电脑浏览器登录粉笔题库，按F12→网络/Network→随便点一个请求→'
                               '请求头Request Headers→复制Cookie整行，粘贴到下面。'))
        self.cookie_edit = PlainTextEdit(); self.cookie_edit.setMaximumHeight(110)
        v2.addWidget(self.cookie_edit)
        b2 = PushButton(FluentIcon.SAVE, '保存Cookie（先验证）')
        b2.clicked.connect(self.save_cookie_manual)
        v2.addWidget(b2, 0, Qt.AlignLeft)
        g2.viewLayout.addLayout(v2)
        lay.addWidget(g2)

        self.login_state = BodyLabel(''); self.login_state.setWordWrap(True)
        lay.addWidget(self.login_state)
        lay.addStretch(); return w

    # ---------- 使用说明页 ----------
    def help_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 20, 24, 20); lay.setSpacing(10)
        lay.addWidget(SubtitleLabel('粉笔刷题分析器 · 使用说明'))
        t = TextBrowser(); t.setReadOnly(True)
        t.setHtml('''
        <ol>
        <li>先在左侧「登录」页扫码（或粘贴Cookie）。扫码需手机粉笔APP。</li>
        <li>在「抓取与目标」页顶部选练习类型、时间范围和分类标准（默认最近30天）。</li>
        <li>同一页下方可调整各二级分类的目标正确率、目标用时、每题分值，以及题量权重。</li>
        <li>回到「榜单」页点「抓取并计算」，完成后表格直接显示优先级榜单。</li>
        <li>点「导出Excel」或「导出高清PNG」保存结果。</li>
        <li>抓取过程中的详细过程记录在「运行日志」里：点榜单页的「运行日志」按钮打开独立窗口查看，可一键导出 txt。</li>
        </ol>
        <h3>运行日志</h3>
        <ul>
        <li>日志不占用主界面，点「运行日志」按钮弹出独立窗口，关闭窗口后内容仍保留，可随时再看。</li>
        <li>日志包含：运行环境、抓取参数、每一页历史的进度、每一场的失败原因、字段缺口统计、题目状态分布、最慢请求、请求统计（成功/空响应/网络异常/401/429/5xx/放弃/下载量）、失败场次明细。</li>
        <li>点「导出日志」保存为 txt，文件开头会附上导出时间与程序目录，便于他人据此排查。</li>
        <li>若日志里出现 HTTP 453、登录页HTML或大量网络异常，通常是网络被拦截或登录态失效，可先换手机热点、再重新登录。</li>
        </ul>
        <h3>抓取设置</h3>
        <ul>
        <li><b>练习类型</b>：专项练习、模考、背题，可多选；未作答的题不计入。</li>
        <li><b>时间范围</b>：默认最近30天，含今天，从今天往回数；勾「全部」则不限时间。</li>
        <li><b>统计口径</b>：按练习创建时间统计，与榜单一致。</li>
        <li><b>分类标准</b>：按所选官方分类树归类。选广西区考时，国考独有分类（如“组合排列-材料”）的题不统计。</li>
        <li>未匹配到分类的题会以“未分类”单列展示，不会静默删除。</li>
        </ul>
        <h3>目标与权重</h3>
        <p>按「一级分类 → 二级分类」展开。每行从左到右依次为：分类名、目标正确率(%)、目标用时(秒)、每题分值、题量权重(平均出现题数 ÷ 总题数130)、换算后的权重比值。改完点「保存」。</p>
        <h3>优先级得分公式</h3>
        <p>优先级得分 = max(目标正确率−我的正确率, 0) × max(我的用时÷目标用时, 0.5) × (每题分值÷0.8) × (1 + 题量权重)。</p>
        <p>题量权重 = 该题型的「平均出现题数 ÷ 总题数130」，固定来自2027国考行政执法类模考第40/41/42/43季4份卷（去重）各二级分类出现题数的平均值，在「抓取与目标」页以分数形式展示，可自行修改。</p>
        <p>这是自定义优先级参考分，<b>不是实际提分收益或预测分数</b>。分数越高越值得优先练。</p>
        <h3>口径与数据质量</h3>
        <ul>
        <li>用时按接口逐题用时，缺失用时不当0、不参与平均。</li>
        <li>榜单只读不可改；未分类题单列“未分类”组明示，不会静默删除。</li>
        <li>抓取不完整（请求失败/分页上限/登录失效）会明确提示，不会假装“完整完成”。</li>
        <li>公司网络可能屏蔽粉笔，若请求失败请换手机热点。</li>
        <li>登录凭证只保存在本机 data/session.json，不会外传。</li>
        </ul>
        ''')
        lay.addWidget(t, 1); return w

    def _show_login_state_local(self):
        """ 只用本地 session.json 显示登录信息，不发网络请求。 """
        sess = login.load_session()
        if not (sess or {}).get('cookie'):
            self.login_state.setText('当前登录态：未登录')
            return
        uid = (sess or {}).get('userId'); uname = (sess or {}).get('userName')
        who = ('账号：%s%s' % (uname or '', ('（ID %s）' % uid) if uid else '')) if (uid or uname) else '账号：未识别'
        when = datetime.datetime.fromtimestamp((sess or {}).get('saved_at', 0)).strftime('%Y-%m-%d %H:%M')
        self.login_state.setText('当前登录态：正在校验… ｜ %s ｜ 保存于 %s' % (who, when))

    def _check_login_async(self):
        """ 后台校验登录态，结果回来后更新文字。 """
        sess = login.load_session() or {}
        c = sess.get('cookie')
        if not c:
            return
        if self._login_worker and self._login_worker.isRunning():
            return
        self._login_worker = LoginCheck(self, c)
        self._login_worker.start()

    def closeEvent(self, e):
        """ 关窗时先停掉后台线程，避免 Qt 报线程仍在运行。 """
        for th in (getattr(self, '_login_worker', None), getattr(self, 'worker', None)):
            try:
                if th is not None and th.isRunning():
                    if hasattr(th, 'cancel'): th.cancel.set()
                    th.quit(); th.wait(3000)
            except Exception:
                pass
        super().closeEvent(e)

    def on_login_checked(self, ok, info):
        sess = login.load_session() or {}
        uid = (sess or {}).get('userId'); uname = (sess or {}).get('userName')
        if info:
            uid = info.get('userId') or uid; uname = info.get('userName') or uname
        who = ('账号：%s%s' % (uname or '', ('（ID %s）' % uid) if uid else '')) if (uid or uname) else '账号：未识别'
        when = datetime.datetime.fromtimestamp((sess or {}).get('saved_at', 0)).strftime('%Y-%m-%d %H:%M')
        self.login_state.setText('当前登录态：%s ｜ %s ｜ 保存于 %s' %
                                 ('有效' if ok else '已失效（请重新登录）', who, when))

    def refresh_login_state(self):
        """ 同步刷新（登录动作后调用）：先显示本地信息，再后台校验。 """
        self._show_login_state_local()
        self._check_login_async()

    def qr_login(self):
        d = QrDialog(self); d.start(); d.exec()
        self.refresh_login_state()

    def clear_login(self):
        login.clear_cookie(); self.refresh_login_state()
        QMessageBox.information(self, '完成', '已清除登录态')

    def save_cookie_manual(self):
        cookie = self.cookie_edit.toPlainText().strip()
        if not cookie:
            QMessageBox.warning(self, '提示', '请先粘贴Cookie'); return
        ok, msg, _ = login.verify_and_save(cookie, on_warning=lambda w: self.on_log(w))
        self.refresh_login_state()
        (QMessageBox.information if ok else QMessageBox.warning)(self, '完成' if ok else '失败', msg)

    def start_fetch(self):
        cookie = login.load_cookie()
        if not cookie:
            QMessageBox.warning(self, '未登录', '请先在「登录」页扫码或粘贴Cookie'); return
        kinds = set()
        if self.chk_special.isChecked(): kinds.add('专项')
        if self.chk_exam.isChecked(): kinds.add('模考')
        if self.chk_recite.isChecked(): kinds.add('背题')
        if not kinds:
            QMessageBox.warning(self, '提示', '至少选择一种练习类型'); return
        days = None if self.chk_all.isChecked() else self.spin_days.value()
        weight_ratios = settings.weights_to_ratios(self.weights)
        self.worker = Worker(self, cookie, kinds, days, self.targets, self.cmb_tree.currentData(), weight_ratios)
        self.worker.start()
        self.btn_fetch.setDisabled(True); self.btn_cancel.setEnabled(True)
        self.log.clear(); self.on_log('开始抓取…')

    def cancel_fetch(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel.set()
            self.on_log('正在取消…')

    def on_log(self, s): self.log.appendPlainText(s)

    def on_progress(self, d, t, m):
        self.pbar.setMaximum(max(t, 1)); self.pbar.setValue(d); self.log.appendPlainText(m)

    def on_error(self, s):
        self.btn_fetch.setDisabled(False); self.btn_cancel.setEnabled(False)
        self.log.appendPlainText('出错：' + s)
        QMessageBox.critical(self, '出错', s[:600])

    def on_done(self, rows, rank, summary):
        self.btn_fetch.setDisabled(False); self.btn_cancel.setEnabled(False)
        if rows is None: return
        self.rows = rows or []; self.ranking = rank or []; self.summary = summary or {}
        self.table.setRowCount(len(self.ranking))
        for ri, r in enumerate(self.ranking):
            for ci, h in enumerate(exporter.HEADERS):
                it = QTableWidgetItem(str(r.get(h, '')))
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter if ci >= 2
                                    else Qt.AlignLeft | Qt.AlignVCenter)
                self.table.setItem(ri, ci, it)
        su = self.summary
        self.log.appendPlainText('完成：抓取%d条，入榜%d条，未分类%d条，失败%d场，空场次%d场' %
                                 (len(self.rows), su.get('in_ranking', 0), su.get('unclassified', 0),
                                  su.get('failed', 0), su.get('empty_sessions', 0)))
        if su.get('incomplete'):
            self.log.appendPlainText('【注意】本次抓取不完整，榜单数据可能缺失，请检查失败清单。')
        self.pbar.setValue(self.pbar.maximum())

    def export_xlsx(self):
        if not self.ranking:
            QMessageBox.warning(self, '提示', '请先抓取并计算'); return
        p, _ = QFileDialog.getSaveFileName(self, '导出Excel', '粉笔练习题优先级榜单.xlsx', 'Excel (*.xlsx)')
        if p:
            try:
                exporter.export_excel(self.ranking, p, self.summary)
                QMessageBox.information(self, '完成', '已导出：' + p)
            except Exception as e:
                QMessageBox.critical(self, '失败', '导出失败：%s' % e)

    def export_png(self):
        if not self.ranking:
            QMessageBox.warning(self, '提示', '请先抓取并计算'); return
        p, _ = QFileDialog.getSaveFileName(self, '导出PNG', '粉笔练习题优先级榜单.png', 'PNG (*.png)')
        if p:
            try:
                exporter.export_png(self.ranking, p, summary=self.summary)
                QMessageBox.information(self, '完成', '已导出：' + p)
            except Exception as e:
                QMessageBox.critical(self, '失败', '导出失败：%s' % e)


def main():
    app = QApplication(sys.argv)
    apply_theme()
    w = MainWindow(); w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
