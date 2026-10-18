# -*- coding: utf-8 -*-
"""
PDF 合并的回归测试

覆盖的都是**真实会踩到的边界**：顺序、加密、损坏文件、不覆盖已有文件、
以及「勾选里混着 OFD 怎么办」。
跑法: python tools/test_merge.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymupdf                        # noqa: E402
import merge_core as MC               # noqa: E402
import bridge                         # noqa: E402

fails = []


def check(cond, msg, extra=None):
    print(("  ✓ " if cond else "  ✗ ") + msg
          + ("" if extra is None else "  → " + str(extra)))
    if not cond:
        fails.append(msg)


def make_pdf(path, pages, text=""):
    """
    造一份 N 页的 PDF（每页写一个可辨识的标识，用来验证合并后的页序）。

    ⚠️ 标识必须用 **ASCII**：PyMuPDF 内置字体不含中文，
       写中文进 PDF 会被渲染成替代字符，读回来就不是原来那串了
       （踩过：4 条页序断言全挂，查半天才发现是测试自己写错了）。
       文件名本身照样可以用中文 —— 那测的是文件系统路径，不受影响。
    """
    doc = pymupdf.open()
    for i in range(pages):
        p = doc.new_page(width=200, height=120)
        p.insert_text((20, 60), text or "FILE", fontsize=11)
        p.insert_text((20, 90), "page %d" % (i + 1), fontsize=9)
    doc.save(path)
    doc.close()
    return path


def main():
    ws = tempfile.mkdtemp(prefix="merge_test_")
    out = os.path.join(ws, "out")
    os.makedirs(out)
    try:
        a = make_pdf(os.path.join(ws, "A_2页.pdf"), 2, "FILE_A")
        b = make_pdf(os.path.join(ws, "B_1页.pdf"), 1, "FILE_B")
        c = make_pdf(os.path.join(ws, "C_3页.pdf"), 3, "FILE_C")
        broken = os.path.join(ws, "坏文件.pdf")
        open(broken, "wb").write(b"this is not a pdf at all")

        # ---------- 1. 页数读取 ----------
        print("\n" + "─" * 70)
        print("1) 页数读取")
        check(MC.page_count(a) == 2, "读 2 页文件", MC.page_count(a))
        check(MC.page_count(c) == 3, "读 3 页文件", MC.page_count(c))
        check(MC.page_count(broken) == 0, "坏文件返回 0 而不是抛异常")

        # ---------- 2. 基本合并 ----------
        print("\n" + "─" * 70)
        print("2) 基本合并")
        r = MC.merge([a, b, c], os.path.join(out, "合并.pdf"))
        check(r["ok"] and r["files"] == 3, "3 份都合进去了", r["files"])
        check(r["pages"] == 6, "总页数 = 2+1+3 = 6", r["pages"])
        check(os.path.exists(r["out"]), "输出文件已生成", os.path.basename(r["out"]))
        check(r["bytes"] > 0, "输出文件非空", str(r["bytes"]) + " 字节")

        # 验证页序：第 1 页应来自 A，最后 1 页来自 C
        d = pymupdf.open(r["out"])
        t_first = d[0].get_text().strip().splitlines()[0]
        t_last = d[-1].get_text().strip().splitlines()[0]
        check("FILE_A" in t_first, "首页来自第 1 个文件", t_first)
        check("FILE_C" in t_last, "末页来自最后 1 个文件", t_last)
        d.close()

        # ---------- 3. 顺序就是传入顺序 ----------
        print("\n" + "─" * 70)
        print("3) 顺序 = 传入顺序（前端上下调整要生效）")
        r2 = MC.merge([c, b, a], os.path.join(out, "倒序.pdf"))
        d = pymupdf.open(r2["out"])
        t0 = d[0].get_text().strip().splitlines()[0]
        check("FILE_C" in t0, "把 C 排到第一位，合并后首页就是 C", t0)
        d.close()

        # ---------- 4. 书签 ----------
        print("\n" + "─" * 70)
        print("4) 书签")
        r3 = MC.merge([a, b, c], os.path.join(out, "带书签.pdf"), bookmark=True)
        d = pymupdf.open(r3["out"])
        toc = d.get_toc()
        check(len(toc) == 3, "生成了 3 条书签", len(toc))
        check(toc[0][2] == 1 and toc[1][2] == 3 and toc[2][2] == 4,
              "书签页码指向各文件的起始页（1 / 3 / 4）",
              [t[2] for t in toc])
        d.close()
        r4 = MC.merge([a, b], os.path.join(out, "无书签.pdf"), bookmark=False)
        d = pymupdf.open(r4["out"])
        check(len(d.get_toc()) == 0, "关掉书签后没有目录")
        d.close()

        # ---------- 5. 不覆盖已有文件 ----------
        print("\n" + "─" * 70)
        print("5) 不覆盖已有文件")
        target = os.path.join(out, "同名.pdf")
        open(target, "wb").write(b"EXISTING")
        r5 = MC.merge([a], target)
        check(r5["out"] != target, "同名时自动换名，不覆盖",
              os.path.basename(r5["out"]))
        check(open(target, "rb").read() == b"EXISTING", "原文件内容未被改动")
        check("(2)" in os.path.basename(r5["out"]), "新名字带序号 (2)",
              os.path.basename(r5["out"]))
        check(MC.unique_path("Z:/绝对不存在的目录/x.pdf").endswith("x.pdf"),
              "目标不存在时原样返回")

        # ---------- 6. 坏文件：跳过但不中断 ----------
        print("\n" + "─" * 70)
        print("6) 坏文件跳过、整体不失败")
        r6 = MC.merge([a, broken, b], os.path.join(out, "含坏文件.pdf"))
        check(r6["ok"] and r6["files"] == 2, "好的 2 份照样合出来", r6["files"])
        check(r6["pages"] == 3, "页数只算成功的（2+1）", r6["pages"])
        check(len(r6["errors"]) == 1, "坏文件被记进 errors", r6["errors"])
        check("坏文件.pdf" in r6["errors"][0]["file"], "errors 里有文件名")

        # ---------- 7. 全都失败要报错 ----------
        print("\n" + "─" * 70)
        print("7) 全失败时明确报错")
        try:
            MC.merge([broken], os.path.join(out, "x.pdf"))
            check(False, "应当抛异常")
        except RuntimeError as exc:
            check("没有一份" in str(exc), "抛出可读的中文错误", exc)

        # ---------- 8. 加密 PDF ----------
        print("\n" + "─" * 70)
        print("8) 加密 PDF")
        enc = os.path.join(ws, "加密.pdf")
        d = pymupdf.open()
        d.new_page()
        d.save(enc, encryption=pymupdf.PDF_ENCRYPT_AES_256,
               owner_pw="owner", user_pw="secret")
        d.close()
        check(MC.page_count(enc) == 0, "加密文件读不到页数（返回 0）")
        r8 = MC.merge([a, enc], os.path.join(out, "含加密.pdf"))
        check(r8["files"] == 1, "加密的那份被跳过，其余正常合并", r8["files"])
        check(len(r8["errors"]) == 1 and "密码" in r8["errors"][0]["error"],
              "错误信息是中文、说清是密码问题", r8["errors"])

        # ---------- 9. bridge.merge_preview：只挑勾选的 PDF ----------
        print("\n" + "─" * 70)
        print("9) 弹窗数据源 merge_preview")
        import invoice_parser as IP
        api = bridge.Api()
        api.folder = ws
        api.items = [
            IP.InvoiceInfo(a), IP.InvoiceInfo(b), IP.InvoiceInfo(c),
            IP.InvoiceInfo(broken),
        ]
        api.items[0].fmt = "PDF"
        api.items[1].fmt = "PDF"
        api.items[2].fmt = "OFD"          # 非 PDF，应被跳过
        api.items[3].fmt = "PDF"
        # 只勾 0、2、3
        api.checked = [True, False, True, True]
        pv = api.merge_preview()
        check(pv["ok"], "返回 ok")
        check([x["i"] for x in pv["items"]] == [0, 3],
              "只收录**勾选的 PDF**（0 和 3，跳过了未勾选的 1 和 OFD 的 2）",
              [x["i"] for x in pv["items"]])
        check([x["fmt"] for x in pv["skipped"]] == ["OFD"],
              "非 PDF 进 skipped 并带格式", pv["skipped"])
        check(pv["totalPages"] == 2, "总页数只算 PDF 的", pv["totalPages"])
        check(pv["defaultName"].endswith("_2份.pdf"), "默认文件名带份数",
              pv["defaultName"])
        check(pv["defaultDir"] == ws, "默认保存到源文件夹")

        # ---------- 10. merge_run：索引顺序 & 非法索引 ----------
        print("\n" + "─" * 70)
        print("10) merge_run")
        api2 = bridge.Api()
        api2.folder = out
        api2.items = [IP.InvoiceInfo(a), IP.InvoiceInfo(b)]
        api2.items[0].fmt = "PDF"
        api2.items[1].fmt = "PDF"
        # 倒序传索引；顺带塞两个非法索引，应被安静忽略
        r10 = api2.merge_run([1, 0, 99, -1], out, "顺序测试.pdf", True)
        check(r10.get("ok") is True, "任务已启动", r10)
        for _ in range(60):                     # 等后台任务跑完
            import time
            if not api2.task["busy"]:
                break
            time.sleep(0.1)
        pl = api2.task["payload"]
        check(pl and pl["ok"], "后台任务顺利完成", api2.task.get("error") or "")
        d = pymupdf.open(pl["out"])
        t0 = d[0].get_text().strip().splitlines()[0]
        check("FILE_B" in t0, "按前端给的顺序合并（B 在前）", t0)
        d.close()

        bad = api2.merge_run([], out, "x.pdf")
        check(bad.get("ok") is False and "没有勾选" in bad.get("msg", ""),
              "没勾选时明确报错", bad.get("msg"))
        bad2 = api2.merge_run([0], os.path.join(ws, "不存在的目录"), "x.pdf")
        check(bad2.get("ok") is False and "不存在" in bad2.get("msg", ""),
              "保存位置不存在时提前拦截", bad2.get("msg"))
        # 文件名里混进路径 → 只取文件名，不能写到别处
        api2.task = api2._idle_task()
        r11 = api2.merge_run([0], out, "../../偷跑.pdf", False)
        for _ in range(60):
            import time
            if not api2.task["busy"]:
                break
            time.sleep(0.1)
        got = api2.task["payload"]["out"]
        check(os.path.dirname(os.path.normpath(got)) == os.path.normpath(out),
              "文件名里带路径时只在目标目录生成（不越界）",
              os.path.basename(got))

    finally:
        shutil.rmtree(ws, ignore_errors=True)

    print("\n" + "=" * 70)
    if fails:
        print("未通过 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("PDF 合并测试全部通过 ✅")


if __name__ == "__main__":
    main()
