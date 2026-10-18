# -*- coding: utf-8 -*-
"""
逐字段体检：对 tests/ 下所有样票跑一遍解析，打印「命名规则」13 项字段的提取结果。
用来定位哪些字段读不到值。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import invoice_parser as ip

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

FIELDS = [
    ("type", "发票类型"), ("no", "发票号码"), ("date", "开票日期"),
    ("buyer", "购买方名称"), ("buyer_tax", "购买方税号"),
    ("seller", "销售方名称"), ("seller_tax", "销售方税号"),
    ("net", "发票金额"), ("tax", "发票税额"), ("amount", "价税合计"),
    ("amount_cn", "价税合计大写"), ("remark", "备注"), ("drawer", "开票人"),
]

files = sorted(f for f in os.listdir(TESTS) if f.lower().endswith(".pdf"))
print("样票数：%d\n" % len(files))

fail_stat = {k: 0 for k, _ in FIELDS}

for fn in files:
    info = ip.parse_pdf(os.path.join(TESTS, fn))
    vals = ip.field_values(info)
    print("=" * 78)
    print("文件：%s" % fn)
    print("-" * 78)
    for key, label in FIELDS:
        v = vals.get(key, "")
        mark = "  " if str(v).strip() else "✗ "
        if not str(v).strip():
            fail_stat[key] += 1
        print("  %s%-14s %s" % (mark, label, v if str(v).strip() else "（读不到）"))
    print()

print("=" * 78)
print("缺失统计（共 %d 张）" % len(files))
for key, label in FIELDS:
    n = fail_stat[key]
    flag = "  OK" if n == 0 else "  ← 需修"
    print("  %-14s 缺失 %d/%d%s" % (label, n, len(files), flag))
