# -*- coding: utf-8 -*-
"""
台账导出（Excel）
==================================================
为什么非要做 xlsx，而不是继续用 CSV：

    **20 位发票号码用 Excel 打开 CSV 会变成科学计数法**（`2.6917E+19`），
    末几位直接丢掉。CSV 格式本身没有「单元格格式」这个概念 ——
    从 CSV 那一侧根本解决不了，只能导出真正的 xlsx 并把号码列设成文本。
    （顺带一提：编码用的是 utf-8-sig，中文本来就不会乱码，
     这一点别记错。）

另外多给一个「汇总」工作表：按购买方 + 按月两个视角的小计，
这是代理记账每月最常要看的两个数字。
"""
import os
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# 表头：列名 → 取数键（顺序即列顺序）
COLUMNS = [
    ("序号", "_no", 6),
    ("凭证类型", "kindText", 10),
    ("原文件名", "file", 40),
    ("新文件名", "newName", 40),
    ("购买方", "buyer", 30),
    ("销售方", "seller", 30),
    ("购买方税号", "buyerTax", 22),
    ("销售方税号", "sellerTax", 22),
    ("价税合计", "amount", 12),
    ("不含税金额", "net", 12),
    ("税额", "tax", 11),
    ("发票号码", "no", 23),          # ← 必须按文本写，否则变科学计数法
    ("开票日期", "date", 12),
    ("发票类型", "type", 16),
    ("附件", "attachText", 22),
    ("校验", "checkText", 26),
    ("备注", "remark", 24),
    ("开票人", "drawer", 10),
]

TEXT_COLS = {"发票号码", "购买方税号", "销售方税号", "开票日期"}   # 强制文本
MONEY_COLS = {"价税合计", "不含税金额", "税额"}

HEAD_FILL = PatternFill("solid", fgColor="EEF1F6")
HEAD_FONT = Font(bold=True, size=10.5)
CELL_FONT = Font(size=10.5)
THIN = Side(style="thin", color="D8DEE9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _style_header(ws, ncol):
    for c in range(1, ncol + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER
    ws.freeze_panes = "A2"           # 冻结表头，滚长表时列名不跑


def write_ledger(path, rows, meta=None):
    """
    写台账。

    `rows` 每一项是 dict，键见 COLUMNS；`meta` 可带 folder / exported / note。

    返回 {"ok", "path", "rows", "sheets"}
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "发票明细"

    headers = [c[0] for c in COLUMNS]
    ws.append(headers)
    _style_header(ws, len(headers))

    for i, r in enumerate(rows, 1):
        r = dict(r)
        r["_no"] = i
        ws.append([r.get(k, "") if r.get(k) is not None else ""
                   for _, k, _w in COLUMNS])

    # 列格式：号码/税号/日期一律文本，金额按两位小数
    for ci, (title, _k, w) in enumerate(COLUMNS, 1):
        ws.column_dimensions[get_column_letter(ci)].width = w
        for ri in range(2, ws.max_row + 1):
            cell = ws.cell(row=ri, column=ci)
            cell.font = CELL_FONT
            cell.border = BORDER
            if title in TEXT_COLS:
                cell.number_format = "@"          # ← 防科学计数法的关键
                cell.alignment = Alignment(horizontal="left")
            elif title in MONEY_COLS:
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right")

    _write_summary(wb, rows, meta or {})
    wb.save(path)
    return {"ok": True, "path": path, "rows": len(rows),
            "sheets": [s.title for s in wb.worksheets]}


def _write_summary(wb, rows, meta):
    """汇总表：总数总金额 + 按购买方 + 按月"""
    ws = wb.create_sheet("汇总")

    def title(text, row):
        c = ws.cell(row=row, column=1, value=text)
        c.font = Font(bold=True, size=12)
        return row + 1

    def table(headers, data, row, widths=(30, 14, 10)):
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row=row, column=ci, value=h)
            c.fill = HEAD_FILL
            c.font = HEAD_FONT
            c.border = BORDER
            ws.column_dimensions[get_column_letter(ci)].width = widths[ci - 1]
        row += 1
        for d in data:
            for ci, v in enumerate(d, 1):
                c = ws.cell(row=row, column=ci, value=v)
                c.font = CELL_FONT
                c.border = BORDER
                if ci == 2 and isinstance(v, (int, float)):
                    c.number_format = "#,##0.00"
            row += 1
        return row + 1

    inv = [r for r in rows if r.get("kind") != "trip"]
    total = sum(r.get("amount") or 0 for r in rows if r.get("kind") != "trip")

    row = title("发票台账汇总", 1)
    row = table(["项目", "数值"],
                [["导出时间", (meta.get("exported")
                             or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))],
                 ["发票张数", len(inv)],
                 ["行程单/附件", len(rows) - len(inv)],
                 ["价税合计", round(total, 2)]],
                row, widths=(30, 26, 10))

    # 按购买方
    by_buyer = {}
    for r in inv:
        k = r.get("buyer") or "（未识别）"
        a = r.get("amount") or 0
        b = by_buyer.setdefault(k, [0, 0.0])
        b[0] += 1
        b[1] += a
    data = sorted(((k, round(v[1], 2), v[0]) for k, v in by_buyer.items()),
                  key=lambda x: -x[1])
    row = title("按购买方", row)
    row = table(["购买方", "价税合计", "张数"], data, row)

    # 按月
    by_mon = {}
    for r in inv:
        d = (r.get("date") or "").strip()
        k = ("%s-%s" % (d[:4], d[4:6])) if len(d) >= 6 else "（无日期）"
        a = r.get("amount") or 0
        b = by_mon.setdefault(k, [0, 0.0])
        b[0] += 1
        b[1] += a
    data = sorted(((k, round(v[1], 2), v[0]) for k, v in by_mon.items()))
    row = title("按开票月份", row)
    table(["月份", "价税合计", "张数"], data, row)


# ---------------------------------------------------------------- 改名对照表（v2.6）
# 为什么单独做一个 CSV：
#   台账是**结果**（改成什么名字、多少钱），对照表是**过程留痕**
#   （这个文件原来叫什么、现在叫什么）。审计或者客户对账时问
#   「这张票你动过吗」，拿这张表一查就知道。
MAPPING_COLUMNS = ["原文件名", "新文件名", "发票号码", "价税合计",
                   "购买方", "开票日期", "格式", "操作时间"]


def _excel_text(v):
    """
    让 Excel 老老实实按**文本**显示。

    20 位发票号码直接写进 CSV，Excel 会当成数字 → 显示成 2.6917E+19，
    末几位直接丢。这是 CSV 格式的天生缺陷（没有单元格格式这一说），
    只能用 `="值"` 这种写法骗过 Excel。
    ⚠️ 代价是用记事本打开会看到 ="…" 这层包装，这是有意的取舍。
    """
    s = "" if v is None else str(v)
    if not s:
        return ""
    return '="%s"' % s.replace('"', '""')


def write_mapping(path, rows, meta=None):
    """
    导出「原文件名 → 新文件名」对照表（CSV，utf-8-sig 保证 Excel 不乱码）。

    返回 {"ok", "path", "rows"}
    """
    import csv
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(MAPPING_COLUMNS)
        for r in rows:
            w.writerow([
                r.get("file", ""),
                r.get("newName", ""),
                _excel_text(r.get("no", "")),
                r.get("amountText", ""),
                r.get("buyer", ""),
                r.get("date", ""),
                r.get("fmt", ""),
                r.get("time", ""),
            ])
    return {"ok": True, "path": path, "rows": len(rows)}


def write_pack_list(path, rows, meta=None):
    """
    打包时放进文件夹里的「报销清单」（v2.7）。

    和台账的区别：台账是给自己留档的（字段多、按客户/按月汇总），
    这张清单是**交出去**的 —— 对方只想知道「谁报的、几张、多少钱」，
    所以只留 序号 / 文件名 / 购买方 / 价税合计 / 发票号码，末尾一行合计。

    rows: [{"name":打包后的文件名, "buyer":..., "amount":float, "no":...}]
    meta: {"title": "报销单_20261018", "folder": ..., "trips": n}

    返回 {"ok", "path", "rows", "total"}
    """
    meta = meta or {}
    wb = Workbook()
    ws = wb.active
    ws.title = "报销清单"

    # 标题行（合并单元格）—— 报销单交上去，抬头要写清楚是哪一单
    title = meta.get("title") or "报销清单"
    ws.merge_cells("A1:E1")
    c = ws["A1"]
    c.value = title
    c.font = Font(bold=True, size=13)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 26

    heads = [("序号", 6), ("文件名", 46), ("购买方", 30),
             ("价税合计", 13), ("发票号码", 23)]
    for i, (name, width) in enumerate(heads, 1):
        cell = ws.cell(row=2, column=i, value=name)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A3"

    total = 0.0
    r0 = 3
    for n, r in enumerate(rows or [], 1):
        amt = r.get("amount")
        try:
            total += float(amt)
        except (TypeError, ValueError):
            amt = None
        ws.cell(row=r0 + n - 1, column=1, value=n).font = CELL_FONT
        ws.cell(row=r0 + n - 1, column=2, value=r.get("name", "")).font = CELL_FONT
        ws.cell(row=r0 + n - 1, column=3, value=r.get("buyer", "")).font = CELL_FONT
        a = ws.cell(row=r0 + n - 1, column=4)
        a.value = float(amt) if amt is not None else None
        a.number_format = "#,##0.00"        # 金额右对齐千分位，一眼看总数
        a.font = CELL_FONT
        no = ws.cell(row=r0 + n - 1, column=5)
        # 号码列必须是**文本**，否则 Excel 会把 20 位号码变成科学计数法
        no.value = str(r.get("no") or "")
        no.number_format = "@"
        no.font = CELL_FONT
        for cidx in range(1, 6):
            ws.cell(row=r0 + n - 1, column=cidx).border = BORDER
        ws.row_dimensions[r0 + n - 1].height = 18

    # 合计行
    last = r0 + len(rows or [])
    ws.cell(row=last, column=1, value="").border = BORDER
    s = ws.cell(row=last, column=2, value="合计 %d 项" % len(rows or []))
    s.font = Font(bold=True, size=10.5)
    s.border = BORDER
    ws.cell(row=last, column=3, value="").border = BORDER
    t = ws.cell(row=last, column=4, value=round(total, 2))
    t.font = Font(bold=True, size=10.5)
    t.number_format = "#,##0.00"
    t.border = BORDER
    ws.cell(row=last, column=5, value="").border = BORDER

    note = meta.get("note") or ""
    if note:
        nrow = last + 2
        ws.cell(row=nrow, column=1, value=note).font = Font(size=9, color="6B7280")

    wb.save(path)
    return {"ok": True, "path": path, "rows": len(rows or []),
            "total": round(total, 2)}

