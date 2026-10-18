# -*- coding: utf-8 -*-
"""
生成仿真「全电发票（数电票）」PDF，用于验证解析与重命名逻辑。

⚠️ 这份测试数据必须**和真实票一样真**，否则测了等于没测。
   老版本生成器的三个坑（导致「明明代码有 bug，测试却是绿的」）：
     · 税号只写了 12 位（真实是 15/17/18/20 位）
     · 价税合计（大写）写的是「（略）」
     · 发票号码被截断成 8 位
   现在改为：18 位税号、真实大写金额、完整 20 位号码，
   版式坐标也对齐真实数电票（页面 595×397，与电子税务局下载的一模一样）。

运行: python tools/make_test_invoice.py
输出: tests/*.pdf
"""
import os
import sys

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests")

PW, PH = 595, 397         # 真实数电票页面尺寸（≈210mm × 140mm）


# ---------------------------------------------------------------- 金额大写
CN_D = "零壹贰叁肆伍陆柒捌玖"
CN_U = ["", "拾", "佰", "仟"]
CN_B = ["", "万", "亿"]


def _group_cn(g: int) -> str:
    s, zero = "", False
    for pos in range(3, -1, -1):
        d = (g // (10 ** pos)) % 10
        if d == 0:
            zero = True
        else:
            if zero and s:
                s += "零"
            zero = False
            s += CN_D[d] + CN_U[pos]
    return s


def _int_cn(n: int) -> str:
    if n == 0:
        return "零"
    groups = []
    while n > 0:
        groups.append(n % 10000)
        n //= 10000
    out = ""
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g == 0:
            if out and not out.endswith("零"):
                out += "零"
            continue
        out += _group_cn(g) + CN_B[i]
    return out.rstrip("零")


def cn_amount(v: float) -> str:
    """77901.51 -> 柒万柒仟玖佰零壹圆伍角壹分（口径与真实票面一致）"""
    neg = v < 0
    cents = int(round(abs(v) * 100))
    yuan, rest = cents // 100, cents % 100
    jiao, fen = rest // 10, rest % 10
    s = _int_cn(yuan) + "圆"
    if jiao == 0 and fen == 0:
        s += "整"
    elif fen == 0:
        s += CN_D[jiao] + "角整"
    else:
        s += (CN_D[jiao] + "角") if jiao else "零"
        s += CN_D[fen] + "分"
    return ("负" if neg else "") + s


def fmt(v: float) -> str:
    return "%.2f" % v


# ---------------------------------------------------------------- 绘制
# ⚠️ 必须中西文混排：PyMuPDF 内置的 china-s 是**全角**字体，
#    20 位发票号码按 8pt 全角排要 160 点宽，从 x=430 起画会直接冲出页宽被裁掉，
#    于是「号码只剩 16 位、税号只剩 16 位」——测试数据本身就是坏的。
#    真实数电票也是这样混排的：中文走中文字体、数字/字母走西文比例字体。
CJK_EXTRA = "（）￥、，。；：“”‘’"


def _is_cjk(ch: str) -> bool:
    return ("\u4e00" <= ch <= "\u9fff") or ch in CJK_EXTRA


def _runs(text: str):
    """把字符串切成 [(片段, 字体)]，中文用 china-s、数字字母用 helv。"""
    out, buf, cur = [], "", None
    for ch in text:
        font = "china-s" if _is_cjk(ch) else "helv"
        if font != cur:
            if buf:
                out.append((buf, cur))
            buf, cur = "", font
        buf += ch
    if buf:
        out.append((buf, cur))
    return out


def put(page, x, y, text, size=8.5):
    """按真实字宽依次排布中英混排文本，返回结束时的 x（不自动换行）。"""
    for seg, font in _runs(text):
        if not seg:
            continue
        page.insert_text((x, y), seg, fontname=font, fontsize=size)
        x += pymupdf.get_text_length(seg, fontname=font, fontsize=size)
    return x


def build(title, no, date, buyer, buyer_tax, seller, seller_tax,
          items, drawer, remark="", payee="", reviewer="",
          spaced_no=False, legacy_footer=False):
    """
    按真实数电票版式绘制一页。

    items: [(项目名称, 单位, 数量, 单价, 金额, 税率, 税额), ...]
    金额全部由 items 推导，不再手工传，避免出现「价税合计 ≠ 金额 + 税额」的假数据。
    """
    net = sum(i[4] for i in items)
    tax = sum(i[6] for i in items)
    total = net + tax

    page = pymupdf.open().new_page(width=PW, height=PH)
    put(page, 160, 25, title, 12)

    # 发票号码 / 开票日期（右上角）
    if spaced_no:
        # 号码被渲染成「2 6 3 1 2 …」，字符之间多了空格，整串变长，
        # 起点要左移、字号要调小，否则又会被页宽裁掉（这正是老测试数据坏掉的原因）
        put(page, 418, 34, "发票号码：", 8)
        put(page, 462, 34, " ".join(no), 7)
    else:
        put(page, 430, 34, "发票号码：%s" % no, 8)
    put(page, 430, 51, "开票日期：%s" % date, 8)

    # 购销方信息：竖排的栏目标签
    for i, ch in enumerate("购买方信息"):
        put(page, 17, 92 + i * 9, ch, 7)
    for i, ch in enumerate("销售方信息"):
        put(page, 330, 92 + i * 9, ch, 7)

    # 名称行（购买方在左、销售方在右）
    put(page, 38, 93, "名称：%s" % buyer, 8)
    put(page, 350, 93, "名称：%s" % seller, 8)
    # 税号行：购买方为「个人」时左栏标签后面**没有值**，必须照实画
    put(page, 38, 123, "统一社会信用代码/纳税人识别号：%s" % buyer_tax, 8)
    put(page, 350, 123, "统一社会信用代码/纳税人识别号：%s" % seller_tax, 8)

    # 明细表头 + 明细
    hy = 151
    for x, t in ((45, "项目名称"), (120, "规格型号"), (205, "单 位"), (240, "数 量"),
                 (275, "单 价"), (355, "金 额"), (405, "税率/征收率"), (480, "税 额")):
        put(page, x, hy, t, 8)
    iy = 162
    for name, unit, qty, price, amt, rate, tx in items:
        put(page, 13, iy, name, 8)
        put(page, 205, iy, unit, 8)
        put(page, 240, iy, qty, 8)
        put(page, 275, iy, price, 8)
        put(page, 355, iy, fmt(amt), 8)
        put(page, 405, iy, rate, 8)
        put(page, 480, iy, fmt(tx), 8)
        iy += 14

    # 合计行 / 价税合计行
    put(page, 58, 259, "合 计", 8)
    put(page, 350, 259, "¥%s" % fmt(net), 8)
    put(page, 480, 259, "¥%s" % fmt(tax), 8)
    put(page, 48, 277, "价税合计（大写）", 8)
    put(page, 130, 277, cn_amount(total), 8)
    put(page, 420, 277, "（小写）¥%s" % fmt(total), 8)

    # 备注框：标签竖排，内容在框内左上
    put(page, 17, 310, "备", 7)
    put(page, 17, 327, "注", 7)
    if remark:
        put(page, 32, 296, remark, 8)

    if legacy_footer:
        # 老版式：收款人 / 复核人 / 开票人 写在同一行
        parts = []
        if payee:
            parts.append("收款人：%s" % payee)
        if reviewer:
            parts.append("复核人：%s" % reviewer)
        parts.append("开票人：%s" % drawer)
        put(page, 55, 368, "  ".join(parts), 8)
    else:
        put(page, 55, 368, "开票人：%s" % drawer, 8)

    return page


def save(page, name):
    os.makedirs(OUT, exist_ok=True)
    doc = page.parent
    path = os.path.join(OUT, name)
    doc.save(path)
    doc.close()
    print("已生成", path)


def main():
    # 1. 标准专票：带备注内容（对齐用户真实票 26312000006376520821 的版式）
    p = build("电子发票（增值税专用发票）", "26312000006376520821", "2026年10月10日",
              "肉界食品（苏州）有限公司", "91320585MA225CHL2B",
              "上海西调电子商务有限公司", "91310116MA7AKP4A33",
              [("*生产生活服务*信息服务费", "次", "1", "77130.2079207921", 77130.21, "1%", 771.30)],
              drawer="朱成文", remark="202609300000246484")
    save(p, "dzfp_26312000006376520821_肉界食品（苏州）有限公司_20261010172351.pdf")

    # 2. 普通发票：1% 征收率、无备注
    p = build("电子发票（普通发票）", "26624000000064110886", "2026年10月12日",
              "上海某某文化传播有限公司", "91310120MA1HR4563F",
              "瓜州县某某鲜火锅店（个体工商户）", "92620922MAK2X0M13B",
              [("*生产生活服务*餐饮服务", "次", "1", "301.485148514851", 301.49, "1%", 3.01)],
              drawer="宋英杰")
    save(p, "dzfp_26624000000064110886_瓜州县某某鲜火锅店（个体工商户）_20261013090153.pdf")

    # 3. 购买方为「个人」：左栏税号为空 —— 校验不会把销售方税号错配给购买方
    p = build("电子发票（增值税专用发票）", "26312000005847746823", "2026年09月15日",
              "个人", "",
              "上海某某文化传媒有限公司", "91310101MA1FLBBBBB",
              [("*文化服务*广告发布费", "项", "1", "1595.50", 1595.50, "6%", 95.73)],
              drawer="张会计", remark="合同号 HT2026088")
    save(p, "dzfp_26312000005847746823_上海某某文化传媒有限公司_20260915141205.pdf")

    # 4. 红字（负数）发票
    p = build("电子发票（增值税专用发票）", "26312000005847746824", "2026年09月15日",
              "广州某某物流有限公司", "91440101MA5CXXXXXX",
              "上海某某供应链管理有限公司", "91310101MA1FLCCCCC",
              [("*运输服务*货运代理", "项", "1", "-2000.00", -2000.00, "9%", -180.00)],
              drawer="张会计")
    save(p, "dzfp_26312000005847746824_上海某某供应链管理有限公司_20260915160000.pdf")

    # 5. 长公司名（易换行）+ 五位数金额
    p = build("电子发票（增值税专用发票）", "26312000005847746825", "2026年09月15日",
              "上海某某某某某某某某某某某某某某某某某某某某有限公司", "91310115MA1K3DDDDD",
              "某某某某某某（上海）某某某某有限公司", "91310115MA1K3EEEEE",
              [("*信息技术服务*系统维护", "项", "1", "88888.88", 88888.88, "6%", 5333.33)],
              drawer="张会计")
    save(p, "dzfp_26312000005847746825_某某某某某某（上海）某某某某有限公司_20260915170000.pdf")

    # 6. 发票号码被渲染成「26 3 1 2 …」带空格的样子
    p = build("电子发票（普通发票）", "26312000005847746826", "2026年09月16日",
              "北京某某贸易有限公司", "91110108MA01ZZZZZ",
              "上海某某信息技术有限公司", "91310101MA1FLAAAAA",
              [("*技术服务*技术咨询服务", "次", "1", "3000.00", 3000.00, "6%", 180.00)],
              drawer="李会计", spaced_no=True)
    save(p, "dzfp_26312000005847746826_上海某某信息技术有限公司_20260916111033.pdf")

    # 7. 老版式：收款人 / 复核人 / 开票人 同一行
    p = build("电子发票（普通发票）", "26312000005847746827", "2026年09月17日",
              "上海某某实业有限公司", "91310115MA1K3FFFFF",
              "深圳某某服务有限公司", "91440300MA5F1YYYYY",
              [("*信息技术服务*软件开发服务", "项", "1", "1000.00", 1000.00, "6%", 60.00)],
              drawer="王五", payee="张三", reviewer="李四", legacy_footer=True)
    save(p, "dzfp_26312000005847746827_深圳某某服务有限公司_20260917110927.pdf")


if __name__ == "__main__":
    main()
