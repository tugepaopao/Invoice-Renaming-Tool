# -*- coding: utf-8 -*-
"""
命令行版：不带界面，直接批量重命名（可放进计划任务 / 批处理脚本）

用法示例：
    python cli.py "D:\\发票"                       # 预览，不改动任何文件
    python cli.py "D:\\发票" --run                 # 原地重命名
    python cli.py "D:\\发票" --run --copy          # 复制到「重命名输出」文件夹
    python cli.py "D:\\发票" --run --distribute    # 按购买方分文件夹归档
    python cli.py "D:\\发票" --run --no-backup     # 关闭执行前自动备份
    python cli.py "D:\\发票" --template "{buyer}—{amount}" --amount fixed
    python cli.py "D:\\发票" --undo                # 撤销上一次操作
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import invoice_parser as IP
import rename_core as RC
from rename_core import BACKUP_SUBDIR, OUT_SUBDIR


def main():
    ap = argparse.ArgumentParser(description="全电发票批量重命名（命令行版）")
    ap.add_argument("folder", help="发票 PDF 所在文件夹")
    ap.add_argument("--run", action="store_true", help="真正执行（默认仅预览）")
    ap.add_argument("--copy", action="store_true",
                    help="复制到「%s」而不是原地改名" % OUT_SUBDIR)
    ap.add_argument("--distribute", action="store_true",
                    help="按购买方公司名分到子文件夹（归档）")
    ap.add_argument("--no-backup", action="store_true",
                    help="不做执行前备份（默认会备份到「%s」）" % BACKUP_SUBDIR)
    ap.add_argument("--recursive", action="store_true", help="包含子文件夹")
    ap.add_argument("--template", default="{buyer}—{amount}",
                    help="命名模板，默认 {buyer}—{amount}")
    ap.add_argument("--amount", default="smart",
                    choices=["smart", "fixed", "int"], help="金额格式")
    ap.add_argument("--prefix", default="", help="金额前缀，如 ¥")
    ap.add_argument("--dedup", default="index",
                    choices=["index", "noshort", "skip"], help="重名处理方式")
    ap.add_argument("--undo", action="store_true", help="撤销上一次操作")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        print("[错误] 文件夹不存在：", folder)
        return 1

    # ---------------- 撤销 ----------------
    if args.undo:
        ops = RC.pending_undo(folder)
        if not ops:
            print("没有可撤销的操作。")
            return 0
        _ops, done = RC.undo_folder(folder)
        print("已还原 %d / %d 个文件。" % (done, len(ops)))
        return 0

    # ---------------- 扫描 ----------------
    results = IP.parse_folder(folder, args.recursive)
    if not results:
        print("未找到 PDF 文件。")
        return 0

    dups = RC.find_duplicates([(i, r.invoice_no) for i, r in enumerate(results)])
    n, total, pos, neg = RC.summarize([r.amount for r in results])

    for r in results:
        print("%s%s" % ("OK " if r.ok else "!! ", r.file_name))
        print("    购买方=%s  金额=%s  号码=%s%s" % (
            r.buyer or "—",
            r.amount if r.amount is not None else "—",
            r.invoice_no or "—",
            "  ⚠ 重复" if r.invoice_no in dups else ""))
        if not r.ok:
            print("    原因：%s" % r.message)

    ok = sum(1 for r in results if r.ok)
    print("\n共 %d 张：识别成功 %d，失败 %d" % (len(results), ok, len(results) - ok))
    print("价税合计：¥ %s（正数 %s / 红字 %s）" % (
        "{:,.2f}".format(total), "{:,.2f}".format(pos), "{:,.2f}".format(neg)))
    if dups:
        print("⚠ 发现 %d 组重复票号，请确认没有重复入账：" % len(dups))
        for no, idxs in dups.items():
            print("   %s  ×%d" % (no, len(idxs)))

    # ---------------- 计划 ----------------
    entries = [RC.Entry(r.path,
                        IP.build_name(r, args.template, args.amount, args.prefix),
                        r.invoice_no, r.buyer, r.amount, r.ok)
               for r in results]
    plan = RC.build_plan(entries, copy_mode=args.copy,
                         dedup=args.dedup, distribute=args.distribute)

    print("-" * 70)
    for p in plan:
        if p.action == "skip":
            print("跳过：%s（目标文件名已存在）" % p.src_name)
        elif p.action == "same":
            print("无需改动：%s" % p.dst_name)
        else:
            print("%s\n  → %s" % (p.src_name, p.dst_name))
        if p.warn:
            print("    ⚠ %s" % p.warn)

    if not args.run:
        print("\n【预览模式】未改动任何文件。加 --run 参数才会真正执行。")
        return 0

    # ---------------- 执行 ----------------
    runner = RC.Runner(plan,
                       do_backup=not args.no_backup,
                       backup_root=folder,
                       on_progress=lambda d, t, nm: print(
                           "  [%d/%d] %s" % (d, t, nm), flush=True))
    runner.run()

    RC.append_log(folder, runner.log)

    print("\n完成：成功 %d，失败 %d，跳过 %d"
          % (runner.done, runner.failed, runner.skipped))
    if runner.backup_dir:
        print("原文件已备份到：%s（%d 个）"
              % (runner.backup_dir, runner.done))
    if runner.backup_failed:
        print("⚠ %d 个文件备份失败" % len(runner.backup_failed))
    print("（操作记录已写入 %s，可用 --undo 还原）" % RC.LOG_NAME)
    return 0 if runner.failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
