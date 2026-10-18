# -*- coding: utf-8 -*-
"""
解析器回归测试（PDF / OFD / XML 三种交付格式）

为什么要有断言：
    之前只有一个「打印各字段值」的脚本，字段改错了也看不出来，
    导致「测试全绿、真实发票却读不到税号和大写金额」。

三层覆盖：
    1) 合成样票（tests/*.pdf）—— 各种版式与边界：个人抬头、红字、
       长公司名换行、号码带空格、老版式收款人/复核人
    2) 真实样本（tests/真实样本/）—— 从电子税务局下载的真实三格式文件
    3) **跨格式一致性** —— 同一张票的 PDF / OFD / XML 必须给出完全相同的字段值。
       这是最有价值的一条：三种解析器实现完全不同，能互相印证，
       任何一边改坏都会立刻暴露。

运行: python tools/test_parser.py [关键字]
"""
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import invoice_parser as ip      # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
REAL = os.path.join(TESTS, "真实样本")

# ---------------------------------------------------------------- 合成样票
# 空串表示「票面上确实没有，应该留空」——这也是断言，防止张冠李戴。
CASES_SYNTH = {
    "dzfp_26312000006376520821_肉界食品（苏州）有限公司_20261010172351.pdf": {
        "type": "增值税专用发票", "no": "26312000006376520821", "date": "20261010",
        "buyer": "肉界食品（苏州）有限公司", "buyer_tax": "91320585MA225CHL2B",
        "seller": "上海西调电子商务有限公司", "seller_tax": "91310116MA7AKP4A33",
        "net": "77130.21", "tax": "771.30", "amount": "77901.51",
        "amount_cn": "柒万柒仟玖佰零壹圆伍角壹分",
        "remark": "202609300000246484", "drawer": "朱成文",
    },
    "dzfp_26624000000064110886_瓜州县某某鲜火锅店（个体工商户）_20261013090153.pdf": {
        "type": "普通发票", "no": "26624000000064110886", "date": "20261012",
        "buyer": "上海某某文化传播有限公司", "buyer_tax": "91310120MA1HR4563F",
        "seller": "瓜州县某某鲜火锅店（个体工商户）", "seller_tax": "92620922MAK2X0M13B",
        "net": "301.49", "tax": "3.01", "amount": "304.50",
        "amount_cn": "叁佰零肆圆伍角整", "remark": "", "drawer": "宋英杰",
    },
    # 购买方为「个人」：左栏税号为空，绝不能被销售方税号顶替
    "dzfp_26312000005847746823_上海某某文化传媒有限公司_20260915141205.pdf": {
        "type": "增值税专用发票", "no": "26312000005847746823", "date": "20260915",
        "buyer": "个人", "buyer_tax": "",
        "seller": "上海某某文化传媒有限公司", "seller_tax": "91310101MA1FLBBBBB",
        "net": "1595.50", "tax": "95.73", "amount": "1691.23",
        "amount_cn": "壹仟陆佰玖拾壹圆贰角叁分",
        "remark": "合同号HT2026088", "drawer": "张会计",
    },
    # 红字（负数）发票
    "dzfp_26312000005847746824_上海某某供应链管理有限公司_20260915160000.pdf": {
        "type": "增值税专用发票", "no": "26312000005847746824", "date": "20260915",
        "buyer": "广州某某物流有限公司", "buyer_tax": "91440101MA5CXXXXXX",
        "seller": "上海某某供应链管理有限公司", "seller_tax": "91310101MA1FLCCCCC",
        "net": "-2000", "tax": "-180", "amount": "-2180",
        "amount_cn": "负贰仟壹佰捌拾圆整", "remark": "", "drawer": "张会计",
    },
    # 长公司名
    "dzfp_26312000005847746825_某某某某某某（上海）某某某某有限公司_20260915170000.pdf": {
        "type": "增值税专用发票", "no": "26312000005847746825", "date": "20260915",
        "buyer": "上海某某某某某某某某某某某某某某某某某某某某有限公司",
        "buyer_tax": "91310115MA1K3DDDDD",
        "seller": "某某某某某某（上海）某某某某有限公司",
        "seller_tax": "91310115MA1K3EEEEE",
        "net": "88888.88", "tax": "5333.33", "amount": "94222.21",
        "amount_cn": "玖万肆仟贰佰贰拾贰圆贰角壹分", "remark": "", "drawer": "张会计",
    },
    # 发票号码被渲染成「26 3 1 2 …」带空格
    "dzfp_26312000005847746826_上海某某信息技术有限公司_20260916111033.pdf": {
        "type": "普通发票", "no": "26312000005847746826", "date": "20260916",
        "buyer": "北京某某贸易有限公司", "buyer_tax": "91110108MA01ZZZZZ",
        "seller": "上海某某信息技术有限公司", "seller_tax": "91310101MA1FLAAAAA",
        "net": "3000", "tax": "180", "amount": "3180",
        "amount_cn": "叁仟壹佰捌拾圆整", "remark": "", "drawer": "李会计",
    },
    # 老版式：收款人 / 复核人 / 开票人 同一行
    "dzfp_26312000005847746827_深圳某某服务有限公司_20260917110927.pdf": {
        "type": "普通发票", "no": "26312000005847746827", "date": "20260917",
        "buyer": "上海某某实业有限公司", "buyer_tax": "91310115MA1K3FFFFF",
        "seller": "深圳某某服务有限公司", "seller_tax": "91440300MA5F1YYYYY",
        "net": "1000", "tax": "60", "amount": "1060",
        "amount_cn": "壹仟零陆拾圆整",
        "remark": "", "drawer": "王五", "payee": "张三", "reviewer": "李四",
    },
}

# ---------------------------------------------------------------- 真实样本
# 同一份 expected 同时喂给 PDF / OFD / XML —— 这就是跨格式一致性校验。
REAL_INVOICES = {
    "26312000006305332561": {
        "type": "普通发票", "no": "26312000006305332561", "date": "20261008",
        "buyer": "上海天空之音文化娱乐有限公司", "buyer_tax": "91310115MACGR4GE80",
        "seller": "上海市浦东新区高行镇雷松饮品店（个体工商户）",
        "seller_tax": "92310115MADT68D8XG",
        "net": "841.58", "tax": "8.42", "amount": "850",
        "amount_cn": "捌佰伍拾圆整", "remark": "", "drawer": "雒雪",
    },
    "26312000006492192781": {
        "type": "增值税专用发票", "no": "26312000006492192781", "date": "20261015",
        "buyer": "杭州桢瑞环境科技有限公司", "buyer_tax": "91330103MA2KHM2Y21",
        "seller": "上海强明门业有限公司", "seller_tax": "91310230MA1JYUXK7M",
        "net": "2212.39", "tax": "287.61", "amount": "2500",
        "amount_cn": "贰仟伍佰圆整", "remark": "", "drawer": "雒雪",
    },
    # 长企业名换行：PDF 那版名称被拆成两行，必须拼回来
    "26312000006508622491": {
        "type": "普通发票", "no": "26312000006508622491", "date": "20261015",
        "buyer": "上海市浦东新区高行镇科凡维修技术服务中心（个体工商户）",
        "buyer_tax": "92310115MAEWME51XN",
        "seller": "上海市浦东新区高行镇雷松饮品店（个体工商户）",
        "seller_tax": "92310115MADT68D8XG",
        "net": "17.82", "tax": "0.18", "amount": "18",
        "amount_cn": "壹拾捌圆整", "remark": "", "drawer": "刘志豪",
    },
    # 网约车出行发票（T3/领行）：**标签和值被 PDF 生成器拆成不同文本块**的版式。
    # 「名称：」和它的值中心只差 0.8pt，而左侧竖排的「购」字差了 3.8pt ——
    # 老版按 span 自己聚类时，「名称：」被孤立成单独一行、和值硬拆开，
    # 于是购买方取不到、退化到兜底策略、错命中明细表头里的「项目名称」，
    # 文件名变成「单 价 数 量 金 额 税率/征收率 税 额—23.90.pdf」。
    # 改用 PyMuPDF 的 line 级分组后修正。（用户 2026-10 报的真实 case）
    "26917000000054010237": {
        "type": "普通发票", "no": "26917000000054010237", "date": "20261008",
        "buyer": "上海白泽元宇智能数字科技有限公司",
        "buyer_tax": "91310000MAD0PN3178",
        "seller": "大连领行智享科技有限公司",
        "seller_tax": "91210202MABUGHHA8K",
        "net": "23.20", "tax": "0.70", "amount": "23.90",
        "amount_cn": "贰拾叁圆玖角整", "remark": "", "drawer": "陈乐",
    },
    # 同批的对照组：标签和值本来就在同一个文本块里（这种一直是对的，
    # 留着是为了确保改聚类方式后没把「原本正确的」改坏）
    "26317000003502801074": {
        "type": "普通发票", "no": "26317000003502801074", "date": "20261008",
        "buyer": "上海白泽元宇智能数字科技有限公司",
        "buyer_tax": "91310000MAD0PN3178",
        "seller": "享道出行（上海）科技股份有限公司",
        "seller_tax": "91310115MA1K427762",
        "net": "46.23", "tax": "1.39", "amount": "47.62",
        "amount_cn": "肆拾柒圆陆角贰分", "remark": "", "drawer": "黄一一",
    },
    # 「PDF 硬转」出来的 OFD（Suwell PDF2OFD Creator）：**包里没有 Tags/CustomTag.xml**，
    # 语义标签路一个字段都取不到，OFD 报「未识别到：购买方名称、价税合计」，
    # 而同一张票的 PDF / XML 都正常 —— 用户 2026-10-16 报的就是这个。
    # 修法：没有语义映射时按坐标还原版式（见 invoice_parser 的 OFD 坐标回退）。
    # 这张票同时钉住三个回归：
    #   ① 竖排表头（购/买/方/信/息、备/注）不能粘进购买方名和备注；
    #   ② 小额票的大写「玖元整」只有 3 个字，正则下限写 4 就会丢（三种格式一起丢）；
    #   ③ 有的版式「合计」的**值在标签上一行**，只往下扫会把 net 取成价税合计。
    "26317000003677236018": {
        "type": "普通发票", "no": "26317000003677236018", "date": "20261015",
        "buyer": "灏芮库（上海）干冰设备有限公司",
        "buyer_tax": "91310114067833461K",
        "seller": "上海莘奉金高速公路建设发展有限公司",
        "seller_tax": "913101121321981190",
        "net": "8.74", "tax": "0.26", "amount": "9",
        "amount_cn": "玖元整",
        "remark": "沪AB93659一型货车上海颛桥站入上海大叶公路站莘奉金出"
                  "2026-10-1407:57:12（不可用于增值税进项抵扣）",
        "drawer": "苏珍燕",
    },
    # 用户 2026-10-17 报「提示大小写金额不一致，但我看票面是一致的」的两张票。
    # 票面**是对的**，是程序自己算大写时漏了「万」后面的零（见 check_core._int_to_cn）：
    #     206867.92 → 老代码算出「贰拾万陆仟捌佰陆拾柒圆玖角贰分」（少一个零）
    #     290021    → 老代码算出「贰拾玖万贰拾壹圆整」（少一个零）
    # 这两张票的大写正好卡在「万位后面接的千位是 0」的形态上，是那类 bug 的典型样本。
    "25312000000276454865": {
        "type": "增值税专用发票", "no": "25312000000276454865", "date": "20250831",
        "buyer": "上海欣稞文化传播有限公司",
        "buyer_tax": "91310116MA1J9QDL0G",
        "seller": "上海幄涵文化传播有限公司",
        "seller_tax": "91310120MA1HWBX681",
        "net": "195158.42", "tax": "11709.50", "amount": "206867.92",
        "amount_cn": "贰拾万零陆仟捌佰陆拾柒圆玖角贰分",
        "remark": "", "drawer": "雒雪",
    },
    "25312000000276445990": {
        "type": "增值税专用发票", "no": "25312000000276445990", "date": "20250831",
        "buyer": "广州惜爱婚姻咨询服务有限公司",
        "buyer_tax": "91440106MAE4LKQW2E",
        "seller": "上海炎创网络科技有限公司",
        "seller_tax": "91310113MA1GLB243H",
        "net": "273604.72", "tax": "16416.28", "amount": "290021",
        "amount_cn": "贰拾玖万零贰拾壹圆整",
        "remark": "", "drawer": "陈宁",
    },
}

ALL_KEYS = ["type", "no", "date", "buyer", "buyer_tax", "seller", "seller_tax",
            "net", "tax", "amount", "amount_cn", "remark", "drawer",
            "payee", "reviewer"]


class R:
    """极简结果收集器"""

    def __init__(self):
        self.total = self.passed = 0
        self.fails = []
        self.covered = {}

    def one(self, label, key, want, got):
        self.total += 1
        got = "" if got is None else str(got)
        if got == want:
            self.passed += 1
            return True
        self.fails.append("%s / %s：期望 %r → 实际 %r" % (label, key, want, got))
        return False


def check_case(r, label, got, want):
    bad = []
    for k in ALL_KEYS:
        if k not in want:
            continue
        if not r.one(label, k, want[k], got.get(k, "")):
            bad.append(k)
    return bad


def load_real(path, name):
    """读一份真实样本 → (格式标签, InvoiceInfo)；不认识返回 (None, None)"""
    low = name.lower()
    if low.endswith(ip.INVOICE_EXTS):
        return os.path.splitext(name)[1].lstrip(".").upper(), ip.parse_invoice(path)
    if low.endswith(".zip"):
        # 压缩包里的 xml：直接在内存里解析 —— 验的正是「客户发来 zip」这个场景
        with zipfile.ZipFile(path) as zf:
            member = next(m for m in zf.namelist() if m.lower().endswith(".xml"))
            raw = zf.read(member)
        info = ip.InvoiceInfo(path)
        info.fmt = "XML"
        ip._parse_xml_bytes(raw, info, path)
        return "ZIP→XML", info
    return None, None


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    r = R()

    # ---------- 1) 合成样票 ----------
    print("═" * 78)
    print("1) 合成样票（tests/*.pdf）")
    for fn, want in CASES_SYNTH.items():
        if only and only not in fn:
            continue
        path = os.path.join(TESTS, fn)
        if not os.path.exists(path):
            r.fails.append("%s → 文件不存在（先跑 tools/make_test_invoice.py）" % fn)
            continue
        info = ip.parse_invoice(path)
        bad = check_case(r, fn[:30], ip.field_values(info), want)
        print("  %s %s" % ("✓" if not bad else "✗ 缺 " + ",".join(bad), fn[:60]))

    # ---------- 2) 真实样本：按票号分组，跨格式比对 ----------
    print("═" * 78)
    print("2) 真实样本（tests/真实样本/）—— 同一张票的三种格式必须完全一致")
    groups = {}
    if os.path.isdir(REAL):
        for n in sorted(os.listdir(REAL)):
            if only and only not in n:
                continue
            try:
                fmt, info = load_real(os.path.join(REAL, n), n)
            except Exception as exc:
                r.fails.append("%s → 读取失败：%s" % (n, exc))
                continue
            if info is None:
                continue
            groups.setdefault(info.invoice_no, []).append((fmt, n, info))

    for no, items in sorted(groups.items()):
        want = REAL_INVOICES.get(no)
        print("  票号 %s" % no)
        if not want:
            r.fails.append("真实样本 %s 没有登记期望值" % no)
            continue
        for fmt, n, info in items:
            bad = check_case(r, n[:30], ip.field_values(info), want)
            r.covered[fmt] = r.covered.get(fmt, 0) + 1
            print("    %s %-8s ok=%s %s" % ("✓" if not bad else "✗", fmt, info.ok,
                                            "" if not bad else "缺 " + ",".join(bad)))
        if len(items) > 1:
            base_fmt, _bn, base = items[0]
            bv = ip.field_values(base)
            for fmt, _n, info in items[1:]:
                ov = ip.field_values(info)
                diff = [k for k in ALL_KEYS if bv.get(k, "") != ov.get(k, "")]
                r.total += 1
                if diff:
                    r.fails.append("跨格式不一致（%s vs %s）：%s"
                                   % (base_fmt, fmt, diff))
                    print("    ✗ 跨格式 %s vs %s → 不一致：%s" % (base_fmt, fmt, diff))
                else:
                    r.passed += 1
                    print("    ✓ 跨格式 %s ↔ %s 全字段一致" % (base_fmt, fmt))

    # ---------- 3) 汇总 ----------
    print("═" * 78)
    cover = "，".join("%s %d 份" % (k, v) for k, v in sorted(r.covered.items()))
    print("格式覆盖：%s" % (cover or "（无）"))
    print("结果：%d / %d 项通过" % (r.passed, r.total))
    if r.fails:
        print("\n未通过：")
        for x in r.fails:
            print("  -", x)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
