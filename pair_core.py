# -*- coding: utf-8 -*-
"""
行程单解析 + 与发票配对
==================================================
背景（2026-10 用户提的需求）：
    网约车报销的标配是「一张发票 + 一份行程单」。行程单不是坏文件，
    它是发票的**配套凭证**，但老版本把它当成「未识别」标红 —— 这是错的。

设计要点：
    1. 发票是**主**、行程单是**附件** —— 界面上行程单折叠到发票下面，不并列
    2. 配对判据按信号强度：金额相等 > 平台/销售方吻合 > 开票日落在行程区间内
       （这个判据参考了 InvoiceFlowAI / ExpenseReimbursement / Invoice-Downloader
        三个开源工具，它们独立发展出了高度一致的做法）
    3. 配不上不算错误 —— 用黄色「待配对」，那其实是有用信息：
       「这张行程单对应的发票可能还没开，你该去催了」

⚠️ 不要把它写成「打车专用」：酒店发票+水单、机票+行程单是同一个模式，
   所以对外叫「凭证配对」，以后扩场景不用重写。
"""
import os
import re

# PyMuPDF 按需加载（v2.7，理由见 invoice_parser._fitz 的注释）：
# 原来在这里顶层 import，导致「打开软件」就白白等了 0.58 秒。
from invoice_parser import _fitz


# ----------------------------------------------------------------- 平台识别
# 常见出行平台。行程单标题通常是「哈啰出行- 行程单」这种写法
PLATFORMS = [
    ("哈啰", "哈啰出行"), ("hello", "哈啰出行"),
    ("滴滴", "滴滴出行"), ("didi", "滴滴出行"),
    ("高德", "高德打车"),
    ("t3", "T3出行"), ("t3出行", "T3出行"),
    ("享道", "享道出行"),
    ("曹操", "曹操出行"),
    ("首汽", "首汽约车"),
    ("花小猪", "花小猪"),
    ("美团", "美团打车"),
    ("阳光出行", "阳光出行"),
    ("如祺", "如祺出行"),
    ("嘀嗒", "嘀嗒出行"),
    ("万顺", "万顺叫车"),
    ("12306", "铁路12306"),
]


def detect_platform(text):
    """从行程单文本里认出是哪个平台（认不出返回空串，不影响配对）"""
    low = (text or "").lower()
    for key, name in PLATFORMS:
        if key in low:
            return name
    return ""


# ----------------------------------------------------------------- 抬头字段
RX_TRIPS = re.compile(r"共\s*(\d+)\s*[个趟笔]?\s*行程")
RX_TOTAL = re.compile(r"合计\s*[¥￥]?\s*([\d,]+(?:\.\d+)?)\s*元")
RX_RANGE = re.compile(r"(\d{4}-\d{2}-\d{2})[^\d]{1,8}(\d{4}-\d{2}-\d{2})")
RX_PHONE = re.compile(r"1[3-9]\d{9}")
# 只认「行程单 / 行程报销单 / 行程明细」这几个词做标题判据
TRIP_HINTS = ("行程单", "行程报销单", "行程明细", "出行单")


def is_trip_text(text):
    """
    判断一份 PDF 的文本是不是行程单。

    ⚠️ 光看「行程单」三个字不够：发票的备注栏里也可能出现这个词。
       所以再加一道反向判据 —— 只要出现了发票才有的字段（发票号码 / 价税合计），
       就按发票处理（发票是主凭证，宁可漏判行程单，不可误判发票）。
    """
    if not text:
        return False
    if not any(h in text for h in TRIP_HINTS):
        return False
    if ("发票号码" in text) or ("价税合计" in text):
        return False
    return True


def _rows_from_tables(doc):
    """
    用 PyMuPDF 的表格识别把行程明细抠出来。

    实测（哈啰行程单）：`find_tables()` 直接给出 27 行 × 9 列
    （表头 + 26 趟行程），比按坐标自己拼靠谱得多。
    """
    out = []
    for page in doc:
        try:
            tabs = page.find_tables()
        except Exception:
            continue
        for t in getattr(tabs, "tables", []):
            try:
                rows = t.extract()
            except Exception:
                continue
            if not rows or len(rows) < 2:
                continue
            header = [(str(c) if c else "").strip() for c in rows[0]]
            # 认一下表头：必须同时有「金额」和（起点/终点/时间）之类的列
            joined = "".join(header)
            if "金额" not in joined:
                continue
            if not any(k in joined for k in ("起点", "终点", "出发", "车型", "城市")):
                continue

            def col(*keys):
                for i, h in enumerate(header):
                    if any(k in h for k in keys):
                        return i
                return -1

            ci = {"no": col("序号"), "car": col("车型"), "city": col("城市"),
                  "from_time": col("出发时间", "上车时间"),
                  "to_time": col("到达时间", "下车时间"),
                  "start": col("起点", "上车地"), "end": col("终点", "下车地"),
                  "kind": col("费用类型", "类型"), "amount": col("金额")}
            for r in rows[1:]:
                cells = [(str(c).replace("\n", " ").strip() if c else "")
                         for c in r]
                if len(cells) < 2:
                    continue

                def take(key):
                    i = ci.get(key, -1)
                    return cells[i] if 0 <= i < len(cells) else ""

                amt = take("amount").replace(",", "").replace("¥", "").strip()
                if not re.match(r"^-?\d+(?:\.\d+)?$", amt):
                    continue                     # 不是金额行（可能是合计行）
                out.append({
                    "no": take("no"), "car": take("car"), "city": take("city"),
                    "from_time": take("from_time"), "to_time": take("to_time"),
                    "start": take("start"), "end": take("end"),
                    "kind": take("kind"), "amount": float(amt),
                })
    return out


def parse_trip(path):
    """
    解析一份行程单，返回结构化信息。认不出就返回 None（交回给调用方当未知文件）。

    返回：
        {"platform", "trips", "total", "date_from", "date_to",
         "phone", "rows", "pages"}
    """
    try:
        doc = _fitz().open(path)
    except Exception:
        return None
    try:
        if doc.needs_pass:
            return None
        texts = []
        for page in doc:
            try:
                texts.append(page.get_text())
            except Exception:
                pass
        full = "\n".join(texts)
        if not is_trip_text(full):
            return None

        info = {
            "platform": detect_platform(full),
            "trips": 0,
            "total": None,
            "date_from": "",
            "date_to": "",
            "phone": "",
            "rows": [],
            "pages": doc.page_count,
        }

        m = RX_TRIPS.search(full)
        if m:
            info["trips"] = int(m.group(1))

        m = RX_TOTAL.search(full)
        if m:
            try:
                info["total"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass

        m = RX_RANGE.search(full)
        if m:
            info["date_from"], info["date_to"] = m.group(1), m.group(2)

        m = RX_PHONE.search(full)
        if m:
            info["phone"] = m.group(0)

        info["rows"] = _rows_from_tables(doc)

        # 明细比抬头更可信：抬头没写全时用明细兜底
        if info["rows"]:
            if not info["trips"]:
                info["trips"] = len(info["rows"])
            if info["total"] is None:
                info["total"] = round(sum(r["amount"] for r in info["rows"]), 2)
            if not info["date_from"]:
                days = sorted(d for r in info["rows"]
                              for d in [r["from_time"][:10]] if d)
                if days:
                    info["date_from"], info["date_to"] = days[0], days[-1]
        return info
    finally:
        try:
            doc.close()
        except Exception:
            pass


# ----------------------------------------------------------------- 配对
# 分数设计：金额一致是最强信号 —— 发票本来就是按行程单开出来的，通常一分不差
SCORE_EXACT = 100        # 金额完全相等（±0.015）
SCORE_NEAR = 40          # 金额接近（±1%）
SCORE_PLATFORM = 50      # 行程单平台和发票销售方对得上
SCORE_DATERANGE = 20     # 开票日落在行程单的起止区间内
THRESHOLD = 60           # 低于这个分数不配对


def match_score(inv, trip):
    """
    算一张发票和一份行程单的匹配度 → (分数, 理由文字)

    判据参考三个同类开源工具（InvoiceFlowAI / ExpenseReimbursement /
    Invoice-Downloader），它们的做法高度一致：金额 + 平台/销售方 + 日期。
    """
    score, why = 0, []

    a, b = inv.get("amount"), trip.get("total")
    if a is not None and b is not None:
        if abs(a - b) < 0.015:
            score += SCORE_EXACT
            why.append("金额一致")
        elif abs(a - b) / max(abs(a), 0.01) <= 0.01:
            score += SCORE_NEAR
            why.append("金额接近")

    plat = (trip.get("platform") or "").replace("出行", "").replace("打车", "")
    seller = inv.get("seller") or ""
    if plat and seller and plat in seller:
        score += SCORE_PLATFORM
        why.append("平台与销售方吻合")

    d = inv.get("date") or ""
    df, dt = trip.get("date_from") or "", trip.get("date_to") or ""
    if d and df and dt and df <= d <= dt:
        score += SCORE_DATERANGE
        why.append("开票日在行程区间内")

    return score, "、".join(why)


def build_pairs(invoices, trips):
    """
    把行程单配到发票上。

    参数：
        invoices: [(行号, {"amount","seller","date"})]
        trips:    [(行号, 行程单解析结果)]

    返回 (attach, pending)：
        attach  = {发票行号: [{"trip":行程单行号, "score","reason"}]}
        pending = [{"trip":行程单行号, "reason":"没配上时的说明"}]

    规则：一份行程单只配一张发票；一张发票可以挂多份行程单
         （分高的先配 —— 先满足「金额完全一致」这种硬匹配）
    """
    cands = []
    for t_idx, t in trips:
        for i_idx, inv in invoices:
            sc, why = match_score(inv, t)
            if sc >= THRESHOLD:
                cands.append((sc, t_idx, i_idx, why))
    cands.sort(key=lambda c: -c[0])          # 高分优先

    used_trip, attach = set(), {}
    for sc, t_idx, i_idx, why in cands:
        if t_idx in used_trip:
            continue
        used_trip.add(t_idx)
        attach.setdefault(i_idx, []).append(
            {"trip": t_idx, "score": sc, "reason": why})

    pending = []
    for t_idx, t in trips:
        if t_idx in used_trip:
            continue
        # 给一句有用的说明，而不是干巴巴的「未配对」
        has_amount = t.get("total") is not None
        pending.append({
            "trip": t_idx,
            "reason": ("本批里没有金额一致的发票"
                       if has_amount else "行程单里没读到合计金额，无法自动配对"),
        })
    return attach, pending
