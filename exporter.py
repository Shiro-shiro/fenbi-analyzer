# -*- coding: utf-8 -*-
""" 榜单导出：Excel + 高清PNG（所见即所得，8列）v2.0 """
import os, json, datetime
# openpyxl 在函数内按需导入：它要 ~0.8 秒，放在模块顶层会拖慢程序启动

HEADERS = ['板块', '题型', '答对/答题', '我的正确率%', '目标正确率%',
           '我的用时(秒)', '目标用时(秒)', '优先级得分']


def _summary_sheet(wb, summary):
    from openpyxl.styles import Font, PatternFill
    ws = wb.create_sheet('统计口径与数据质量')
    su = summary or {}
    rows = [
        ['项目', '内容'],
        ['导出时间', datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')],
        ['统计时间口径', '按练习创建时间'],
        ['题量权重口径', su.get('mock_weight_note', '未计算')],
        ['题量权重来源', '固定：2027行政执法类模考4份卷各二级分类平均出现题数/总题数130（设置页可改）'],
        ['完整模考卷份数', su.get('full_mock_papers', 0)],
        ['抓取场次', su.get('total_sessions', 0)],
        ['入榜记录数', su.get('in_ranking', 0)],
        ['未分类记录数（单列未分类组）', su.get('unclassified', 0)],
        ['抓取失败场次', su.get('failed', 0)],
        ['0题空场次', su.get('empty_sessions', 0)],
        ['广西模式跳过（国考独有分类题）', su.get('skipped_guangxi', 0)],
        ['元数据缺失题数', su.get('meta_missing', 0)],
        ['数据是否完整', '否（见失败清单）' if su.get('incomplete') else '是'],
        ['分类树', su.get('exam_tree', '-')],
        ['筛选天数', su.get('days', '全部')],
        ['优先级得分公式', 'max(目标正确率−我的正确率,0)×max(我的用时÷目标用时,0.5)×(每题分值÷0.8)×(1+题量权重)。自定义参考分，非实际提分收益。'],
    ]
    for r in rows: ws.append(r)
    for c in ws[1]:
        c.font = Font(bold=True, color='FFFFFF'); c.fill = PatternFill('solid', fgColor='4472C4')
    ws.column_dimensions['A'].width = 34; ws.column_dimensions['B'].width = 90
    failures = su.get('failures') or []
    if failures:
        ws2 = wb.create_sheet('抓取失败清单')
        ws2.append(['练习ID', '名称', '失败环节', '状态'])
        for f in failures:
            ws2.append([f.get('exerciseId'), f.get('name'),
                        ','.join(k for k in f if k not in ('exerciseId', 'name')),
                        json.dumps(f, ensure_ascii=False)])
    return ws


def export_excel(rows, path, summary=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook(); ws = wb.active; ws.title = '优先级榜单'
    ws.append(HEADERS)
    for r in rows:
        ws.append([r.get(h) for h in HEADERS])
    fill = PatternFill('solid', fgColor='4472C4')
    thin = Side(style='thin', color='D0D0D0')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for c in ws[1]:
        c.font = Font(bold=True, color='FFFFFF'); c.fill = fill
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        c.border = border
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(horizontal='center', vertical='center')
            c.border = border
    widths = [18, 28, 13, 13, 13, 13, 13, 17]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = 'A2'
    _summary_sheet(wb, summary)
    wb.save(path)


def export_png(rows, path, title='粉笔练习题优先级榜单', summary=None):
    from PySide6.QtGui import QImage, QPainter, QColor, QFont, QPen
    from PySide6.QtCore import Qt, QRect
    scale = 2
    colw = [150, 215, 110, 120, 120, 120, 120, 160]
    W = sum(colw) + 40
    rowh = 46
    note_h = 46
    H = 110 + rowh * (len(rows) + 1) + note_h
    img = QImage(W * scale, H * scale, QImage.Format_RGB32)
    img.fill(QColor('#FFFFFF'))
    p = QPainter(img); p.scale(scale, scale)
    p.setRenderHint(QPainter.Antialiasing)
    f = QFont('Microsoft YaHei', 10); p.setFont(f)
    p.setPen(QColor('#1F3864'))
    ft = QFont('Microsoft YaHei', 15, QFont.Bold); p.setFont(ft)
    p.drawText(20, 32, title)
    p.setFont(f)
    x0, y0 = 20, 60
    p.setBrush(QColor('#4F6BED')); p.setPen(Qt.NoPen)
    p.drawRect(x0, y0, sum(colw), rowh)
    p.setPen(QColor('#FFFFFF')); p.setFont(QFont('Microsoft YaHei', 10, QFont.Bold))
    x = x0
    for i, h in enumerate(HEADERS):
        p.drawText(QRect(int(x), int(y0), colw[i], rowh), Qt.AlignCenter, h)
        x += colw[i]
    p.setFont(f)
    for ri, row in enumerate(rows):
        y = y0 + rowh * (ri + 1)
        if ri % 2 == 0:
            p.setBrush(QColor('#F2F6FC')); p.setPen(Qt.NoPen)
            p.drawRect(x0, y, sum(colw), rowh)
        p.setPen(QColor('#333333'))
        x = x0
        for ci, h in enumerate(HEADERS):
            p.drawText(QRect(int(x), int(y), colw[ci], rowh), Qt.AlignCenter, str(row.get(h, '')))
            x += colw[ci]
    p.setPen(QPen(QColor('#D0D0D0'), 1))
    for ci in range(len(colw) + 1):
        x = x0 + sum(colw[:ci])
        p.drawLine(x, y0, x, y0 + rowh * (len(rows) + 1))
    for ri in range(len(rows) + 2):
        y = y0 + rowh * ri
        p.drawLine(x0, y, x0 + sum(colw), y)
    # 底部标注：口径 + 数据质量警告
    su = summary or {}
    su_rows = su.get('in_ranking', 0); su_un = su.get('unclassified', 0); su_fail = su.get('failed', 0)
    note = '优先级得分为自定义参考分，非实际提分收益｜按创建时间统计｜入榜%d条/未分类%d条/失败%d场' % (
        su_rows, su_un, su_fail)
    if su.get('incomplete'):
        note += '　【本次抓取不完整，数据可能缺失】'
    p.setFont(QFont('Microsoft YaHei', 8)); p.setPen(QColor('#888888'))
    p.drawText(QRect(x0, y0 + rowh * (len(rows) + 1) + 6, sum(colw), note_h - 8), Qt.AlignLeft, note)
    p.end()
    img.save(path)


def _rect(x, y, w, h):
    from PySide6.QtCore import QRect
    return QRect(int(x), int(y), int(w), int(h))
