# -*- coding: utf-8 -*-
"""
票种体检：把一堆发票丢进来，逐张报告「票种 / 购买方 / 销售方 / 金额 / 备注」读没读到。

用途：新增票种（机动车、航空行程单、收购发票…）时，先跑这个看缺口，
      再决定要不要给解析器补规则。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe tools\\type_probe.py "C:\\某个文件夹"
    .venv\\Scripts\\python.exe tools\\type_probe.py "C:\\a.pdf"      # 单个文件也行

输出：
    · 每张票一行，缺失的字段标 [缺]
    · 末尾按票种汇总：张数 / 五项字段的命中率
"""
import os
import sys
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from invoice_parser import parse_invoice  # noqa: E402

FIELDS = [("buyer", "购买方"), ("seller", "销售方"),
          ("amount", "金额"), ("remark", "备注"),
          ("invoice_no", "号码"), ("date", "日期")]
SUFFIX = (".pdf", ".ofd", ".xml", ".zip")


def collect(target):
    if os.path.isfile(target):
        return [target]
    out = []
    for name in sorted(os.listdir(target)):
        p = os.path.join(target, name)
        if os.path.isfile(p) and os.path.splitext(name)[1].lower() in SUFFIX:
            out.append(p)
    return out


def brief(v, width=22):
    s = "" if v is None else str(v)
    s = s.replace("\n", " ").strip()
    return s if len(s) <= width else s[:width - 1] + "…"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    files = collect(sys.argv[1])
    if not files:
        print("没找到 PDF / OFD / XML 文件：", sys.argv[1])
        return 1

    stats = collections.defaultdict(lambda: collections.Counter())
    counts = collections.Counter()
    problems = []

    print("共 %d 个文件\n" % len(files))
    for i, path in enumerate(files, 1):
        it = parse_invoice(path, use_cache=False)
        kind = it.inv_type or ("行程单" if it.kind == "trip" else "（未识别票种）")
        counts[kind] += 1

        got = []
        for attr, label in FIELDS:
            val = getattr(it, attr, None)
            ok = bool(val) and val != ""
            stats[kind][attr] += 1 if ok else 0
            stats[kind]["total"] += 1
            got.append("%s=%s" % (label, brief(val) if ok else "[缺]"))

        print("[%d] %s" % (i, os.path.basename(path)))
        print("    票种=%s  引擎=%s  %s" % (kind, it.engine or "-", "ok" if it.ok else "失败"))
        print("    " + "  ".join(got))
        if it.message:
            print("    提示：" + it.message)

        missing = [label for attr, label in FIELDS
                   if not (getattr(it, attr, None) or "") and attr in ("buyer", "amount")]
        if missing or not it.ok:
            problems.append((os.path.basename(path), kind, missing, it.message))

    print("\n" + "=" * 72)
    print("按票种汇总")
    print("%-24s %4s  %-6s %-6s %-6s %-6s" % ("票种", "张数", "购买方", "销售方", "金额", "备注"))
    for kind, n in counts.most_common():
        c = stats[kind]
        rate = lambda k: "%d/%d" % (c[k], n)  # noqa: E731
        print("%-24s %4d  %-8s %-8s %-8s %-8s"
              % (kind, n, rate("buyer"), rate("seller"), rate("amount"), rate("remark")))

    if problems:
        print("\n需要关注（购买方或金额没读到 / 整体失败）：")
        for name, kind, missing, msg in problems:
            print("  · %s  [%s]  缺：%s  %s"
                  % (name[:50], kind, "、".join(missing) or "-", msg or ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
