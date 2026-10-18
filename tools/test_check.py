# -*- coding: utf-8 -*-
"""
离线风控校验（check_core）的单元测试。

为什么单独建这个文件：
    2026-10-17 用户报「这两张发票提示大小写金额不一致，可我看了是一致的」——
    查下来是 `_int_to_cn()` 生成大写时**漏了跨组的「零」**：
        206867.92 → 算成「贰拾万陆仟捌佰陆拾柒圆玖角贰分」（票面：贰拾万**零**陆仟…）
        290021    → 算成「贰拾玖万贰拾壹圆整」（票面：贰拾玖万**零**贰拾壹圆整）
    校验拿这个错结果去比票面，就误报「不一致」，用户白核对一场。

    **根因是没有测试**：check_core.py 一直没被任何测试覆盖，
    这类纯计算的错误只能靠人肉撞上。所以这里补上。

覆盖：
    1) 整数 → 大写：专挑「零」容易错的形态（万位后接 0、亿位后接 0、整数正好是整万/整亿）
    2) 小数 → 大写：角/分/零角有分/整
    3) 真实票面反向验证：包里每张真票的票面大写都必须和程序算的一致
    4) 反向用例：**真的不一致时必须还能报出来**（别把校验改哑了）
    5) 税号校验位 / 号码位数 / 日期

运行: python tools/test_check.py
"""
import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_core as CC          # noqa: E402
import invoice_parser as IP      # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL = os.path.join(ROOT, "tests", "真实样本")

fails = []
total = passed = 0


def check(cond, msg, extra=None):
    global total, passed
    total += 1
    if cond:
        passed += 1
        print("  ✓ " + msg + ("" if extra is None else "  → " + str(extra)))
    else:
        fails.append(msg + ("" if extra is None else "  → " + str(extra)))
        print("  ✗ " + msg + ("" if extra is None else "  → " + str(extra)))


def section(title):
    print("─" * 72)
    print(title)


def main():
    # ---------------------------------------------------------------- 1
    section("1) 整数 → 大写（「零」的位置是重灾区）")
    # 期望值按票面标准写法；右边注释说明它卡在哪个坑上
    cases = [
        (0, "零"),
        (1, "壹"),
        (10, "壹拾"),                     # 不是「拾」
        (101, "壹佰零壹"),                # 中间一个零
        (110, "壹佰壹拾"),                # 末尾零不写
        (1000, "壹仟"),
        (1001, "壹仟零壹"),
        (1060, "壹仟零陆拾"),
        (1410, "壹仟肆佰壹拾"),
        (2000, "贰仟"),
        (10000, "壹万"),                  # 整万，不能「壹万零」
        (10006, "壹万零陆"),
        (10060, "壹万零陆拾"),
        (20000, "贰万"),
        (200687, "贰拾万零陆佰捌拾柒"),      # ★ 万位后接千位=0，老代码漏零
        (290021, "贰拾玖万零贰拾壹"),        # ★ 万位后接百位=0，老代码漏零
        (100000, "壹拾万"),
        (1000000, "壹佰万"),
        (1000001, "壹佰万零壹"),
        (1234567, "壹佰贰拾叁万肆仟伍佰陆拾柒"),
        (10000000, "壹仟万"),
        (10006000, "壹仟万零陆仟"),          # ★ 跨组的零（千万位与千位之间空了 3 位）
        (100000000, "壹亿"),                # 整亿，不能「壹亿零万」
        (100000001, "壹亿零壹"),
        (100010000, "壹亿零壹万"),
        (1000000000, "壹拾亿"),
        (10000000000, "壹佰亿"),
    ]
    for v, want in cases:
        got = CC._int_to_cn(v)
        check(got == want, "%-12d → %s" % (v, want), "" if got == want else "实际 " + got)

    # ---------------------------------------------------------------- 2
    section("2) 小数 → 完整大写（角 / 分 / 零角有分 / 整）")
    money = [
        (9, "玖元整", "整"),
        (1060, "壹仟零陆拾圆整", "整"),
        (1410.3, "壹仟肆佰壹拾圆叁角", "只有角"),
        (1060.5, "壹仟零陆拾圆伍角", "只有角（一位小数）"),
        (1060.05, "壹仟零陆拾圆零伍分", "有分无角 → 必须补「零」"),
        (0.5, "零圆伍角", "不足一元"),
        (206867.92, "贰拾万零陆仟捌佰陆拾柒圆玖角贰分", "用户票 1（元转圆后比）"),
        (290021.0, "贰拾玖万零贰拾壹圆整", "用户票 2"),
        (-2180, "负贰仟壹佰捌拾圆整", "红字票带「负」"),
    ]
    for v, want, note in money:
        got = CC._norm_cn(CC.amount_to_cn(v))
        exp = CC._norm_cn(want)
        check(got == exp, "%-12s → %s（%s）" % (v, want, note),
              "" if got == exp else "实际 " + CC.amount_to_cn(v))

    # ---------------------------------------------------------------- 3
    section("3) 真实票面反向验证（票面大写 = 税局给的，就是标准答案）")
    files = []
    for pat in ("*.pdf", "*.ofd", "*.xml"):
        files += glob.glob(os.path.join(REAL, pat))
    n_checked = n_skip = 0
    for p in sorted(files):
        try:
            info = IP.parse_invoice(p)
        except Exception:
            continue
        if info.amount is None or not info.amount_cn:
            continue
        r = CC.amount_cn_ok(info.amount, info.amount_cn)
        if r is None:
            n_skip += 1                    # 红字票：各家写法不统一，按设计不判
            continue
        n_checked += 1
        check(r is True, "%s（%s）" % (os.path.basename(p)[:44], info.amount),
              "" if r else "票面 %s / 程序算 %s"
              % (info.amount_cn, CC.amount_to_cn(info.amount)))
    print("  参与比对 %d 张，按规则跳过 %d 张（红字票）" % (n_checked, n_skip))

    # ---------------------------------------------------------------- 4
    section("4) 反向用例：真的不一致时必须还能报出来")
    class Fake:
        invoice_no = "25312000000276454865"
        buyer_tax = ""
        seller_tax = ""
        amount = 206867.92
        amount_cn = "贰拾万零陆仟捌佰陆拾柒圆玖角壹分"   # 故意改错「分」
        date = "20250831"
        is_red = False

    rs = CC.validate(Fake())
    check(any(r["field"] == "价税合计" for r in rs),
          "大写写成「…玖角壹分」→ 能报出不一致", [r["msg"] for r in rs])

    # 差一分也要报（金额校验不能有容差，跟人工对账一致）
    Fake.amount_cn = "贰拾万零陆仟捌佰陆拾柒圆玖角叁分"
    rs = CC.validate(Fake())
    check(any(r["field"] == "价税合计" for r in rs), "差一分也要报")

    # 用户报的那两张，改对之后必须一条都不报
    for p in sorted(glob.glob(os.path.join(REAL, "dzfp_25312000000276*.pdf"))):
        info = IP.parse_invoice(p)
        rs = CC.validate(info)
        check(not any(r["field"] == "价税合计" for r in rs),
              "%s 不再误报「金额不一致」" % os.path.basename(p)[:40],
              [r["msg"] for r in rs])

    # ---------------------------------------------------------------- 5
    section("5) 税号校验位 / 号码位数 / 日期")
    check(CC.uscc_ok("91310116MA1J9QDL0G") is True, "真税号通过（用户票 1 购买方）")
    check(CC.uscc_ok("91310120MA1HWBX681") is True, "真税号通过（用户票 1 销售方）")
    check(CC.uscc_ok("91440106MAE4LKQW2E") is True, "真税号通过（用户票 2 购买方）")

    bad = "91310116MA1J9QDL1G"            # 末位改掉，校验位必然对不上
    check(CC.uscc_ok(bad) is False, "末位改错 → 判为不对")
    check(CC.uscc_ok("91310115MA1K3XXXXX") is False, "示例里的占位税号 → 判为不对")
    check(CC.uscc_ok("123456789012345") is None, "15 位老税号 → 不判（返回 None）")

    check(CC.invoice_no_ok("25312000000276454865") is True, "20 位数电票号码通过")
    check(CC.invoice_no_ok("12345678") is True, "8 位老式号码通过")
    check(CC.invoice_no_ok("1234567890") is False, "10 位 → 判为异常")
    check(CC.invoice_no_ok("") is None, "空号码不判")

    check(CC.date_ok("20250831") is True, "过去的日期通过")
    check(CC.date_ok("20261332") is False, "13 月 32 日 → 判为非法")
    check(CC.date_ok("20991231") is False, "未来日期 → 判为非法")
    check(CC.date_ok("2025") is None, "不足 8 位不判")

    # ---------------------------------------------------------------- 汇总
    print("═" * 72)
    print("结果：%d / %d 项通过" % (passed, total))
    if fails:
        print("\n未通过：")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("风控校验测试全部通过 ✅")


if __name__ == "__main__":
    main()
