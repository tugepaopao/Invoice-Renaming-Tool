# -*- coding: utf-8 -*-
"""
桥接层冒烟测试：不启动界面，直接调用 Api 的方法，验证
    · 详情数据（_row）带上了新增字段
    · 「效果」预览确实用当前选中发票的真实值，并能报出读不到的字段

运行: python tools/test_bridge.py
"""
import glob
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymupdf                   # noqa: E402
import invoice_parser as IP      # noqa: E402
import rename_core as RC         # noqa: E402
import bridge                    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

fails = []


def check(cond, msg, extra=None):
    print(("  ✓ " if cond else "  ✗ ") + msg
          + ("" if extra is None else "  → " + str(extra)))
    if not cond:
        fails.append(msg)


def main():
    api = bridge.Api()
    api.items = IP.parse_folder(TESTS)
    api.checked = [it.ok for it in api.items]
    api._recompute_dups()
    print("载入样票：%d 张，其中识别成功 %d 张\n"
          % (len(api.items), sum(1 for it in api.items if it.ok)))

    # ---------- 1. 详情数据字段齐全 ----------
    print("─" * 70)
    print("1) 详情面板数据（_row）")
    idx = next(i for i, it in enumerate(api.items) if "46823" in it.file_name)
    row = api._row(idx)
    for k in ("buyerTax", "sellerTax", "amountCn", "remark", "drawer", "type"):
        check(k in row, "字段存在：%s = %r" % (k, row.get(k)))
    check(row["amountCn"] == "壹仟陆佰玖拾壹圆贰角叁分",
          "大写金额正确：%s" % row["amountCn"])
    check(row["remark"] == "合同号HT2026088", "备注正确：%s" % row["remark"])
    check(row["buyerTax"] == "", "「个人」抬头下购买方税号为空（没被销售方顶替）")

    # ---------- 2. 效果预览用真实值 ----------
    print("\n" + "─" * 70)
    print("2) 规则预览（rule_preview）")
    r = api.rule_preview(["buyer", "amount"], "—", idx)
    check(r["source"] == "real", "数据源标记为当前票实测：%s" % r["source"])
    check(r["preview"] == "个人—1691.23",
          "预览值取自该票：%r" % r["preview"])
    check(r["missing"] == [], "没有缺失字段：%s" % r["missing"])

    # 勾上这张票读不到的「购买方税号」→ 必须报出来，且文件名里不留空档
    r2 = api.rule_preview(["buyer", "buyer_tax", "amount"], "—", idx)
    check(r2["missing"] == ["购买方税号"],
          "读不到的字段被列出：%s" % r2["missing"])
    check(r2["preview"] == "个人—1691.23",
          "空字段被跳过、不留空档：%r" % r2["preview"])

    # 换成有税号的那张票 → 不再报缺失，且值出现在预览里
    idx2 = next(i for i, it in enumerate(api.items) if "肉界食品" in it.file_name)
    r3 = api.rule_preview(["buyer", "buyer_tax", "amount_cn"], "—", idx2)
    check(r3["missing"] == [], "换票后无缺失：%s" % r3["missing"])
    check("91320585MA225CHL2B" in r3["preview"] and "柒万柒仟玖佰零壹圆伍角壹分" in r3["preview"],
          "预览含税号与大写金额：%r" % r3["preview"])

    # ---------- 3. 全字段勾选（压测：13 项一起上） ----------
    print("\n" + "─" * 70)
    print("3) 13 项字段全勾")
    allf = [f["key"] for f in bridge.FIELD_DEFS]
    r4 = api.rule_preview(allf, "_", idx2)
    check(r4["source"] == "real" and r4["missing"] == [],
          "13 项全部读到：missing=%s" % r4["missing"])
    print("     预览 = %s" % r4["preview"][:120])

    # ---------- 4. 空列表退回示例 ----------
    print("\n" + "─" * 70)
    print("4) 没有发票时退回内置示例")
    empty = bridge.Api()
    r5 = empty.rule_preview(["buyer", "amount"], "—")
    check(r5["source"] == "demo" and r5["preview"],
          "示例模式：source=%s preview=%r" % (r5["source"], r5["preview"]))

    # ---------- 5. 参数串位防卫 ----------
    print("\n" + "─" * 70)
    print("5) 参数串位防卫")
    # 踩过的坑：idx 原排在 suffix 后面，前端按位置只传 3 个参数时
    # sel 落到了 suffix 上 —— 预览永远显示第 1 张票，文件名尾巴还多出一个数字。
    # 现在 idx 排在 suffix 前面（三参数调用天然正确），并且对 suffix 做类型检查。
    r6 = api.rule_preview(["buyer", "amount"], "—", idx)
    check(r6["preview"] == "个人—1691.23",
          "第 3 个位置传行号 → 生效为选中第 %d 张：%r" % (idx, r6["preview"]))
    r7 = api.rule_preview(["buyer", "amount"], "—", None, idx)   # 故意串位
    check(r7["preview"] == api.rule_preview(["buyer", "amount"], "—", 0)["preview"],
          "行号误传到 suffix 时被丢弃、不污染文件名：%r" % r7["preview"])

    # ---------- 6. 拖放导入 ----------
    print("\n" + "─" * 70)
    print("6) 拖放导入（文件 / 文件夹 / 压缩包）")
    import shutil
    import tempfile
    ws = tempfile.mkdtemp(prefix="invoice_drop_")
    drop = bridge.Api()

    # 造一个工作区：一个子文件夹装 2 张票、1 个压缩包、1 个不支持的格式
    sub = os.path.join(ws, "客户A")
    os.makedirs(sub)
    pdf = os.path.join(TESTS, "dzfp_26312000005847746823_上海某某文化传媒有限公司_20260915141205.pdf")
    ofd_src = os.path.join(TESTS, "真实样本")
    shutil.copy(pdf, os.path.join(sub, os.path.basename(pdf)))
    real_ofd = next(f for f in os.listdir(ofd_src) if f.lower().endswith(".ofd"))
    shutil.copy(os.path.join(ofd_src, real_ofd), os.path.join(sub, real_ofd))
    n_real = len(os.listdir(sub))
    real_zip = next(f for f in os.listdir(ofd_src) if f.lower().endswith(".zip"))
    zip_dst = os.path.join(ws, real_zip)
    shutil.copy(os.path.join(ofd_src, real_zip), zip_dst)
    txt = os.path.join(ws, "说明.txt")
    open(txt, "w", encoding="utf-8").write("不是发票")

    def reset():
        drop.task = drop._idle_task()

    # ① 只拖一个文件夹 → 等价于「选择文件夹」
    drop.accept_drop([sub])
    check(drop.folder == os.path.abspath(sub) and not drop.files,
          "拖文件夹 → 切换为文件夹模式", drop.folder)
    reset()

    # ② 拖压缩包 → 解到自己旁边的「xxx_解压」，并把它当作来源
    r = drop.accept_drop([zip_dst])
    ex_dir = os.path.join(ws, os.path.splitext(real_zip)[0] + "_解压")
    check(os.path.isdir(ex_dir), "压缩包被解到 zip 同级的「_解压」文件夹", ex_dir)
    check(len(r["extracted"]) == 1 and not r["skipped"],
          "导入摘要报告了解压结果：%s" % r)
    check(drop.folder == ex_dir, "解压后切换到解压目录", drop.folder)
    reset()

    # ③ 再解一次：不能删/覆盖已有内容
    marker = os.path.join(ex_dir, "别动我.txt")
    open(marker, "w", encoding="utf-8").write("已有内容")
    drop.accept_drop([zip_dst])
    check(os.path.exists(marker), "重复导入不会清空/覆盖已有内容")
    reset()

    # ④ 混拖：文件 + 文件夹 + 压缩包 + 不支持格式 → 汇总成文件清单
    r = drop.accept_drop([pdf, sub, zip_dst, txt])
    check(drop.files and not drop.folder,
          "混拖 → 切换为文件清单模式（%d 个）" % len(drop.files))
    check(any(f.lower().endswith(".ofd") for f in drop.files), "清单含 OFD")
    check(any(f.lower().endswith(".xml") for f in drop.files), "清单含解压出来的 XML")
    check(any(f.lower().endswith(".pdf") for f in drop.files), "清单含 PDF")
    check(any("说明.txt" in s for s in r["skipped"]),
          "不支持的格式被计入 skipped：%s" % r["skipped"])
    reset()

    # ⑤ 只拖不支持的东西 → 明确报错而不是静默
    r = drop.accept_drop([txt])
    check(r["ok"] is False and "没有找到" in r["msg"], "无可用文件时明确报错：%s" % r["msg"])
    reset()

    # ⑥ 目录穿越防护
    evil = os.path.join(ws, "evil.zip")
    import zipfile as _zf
    with _zf.ZipFile(evil, "w") as z:
        z.writestr("../../pwned.txt", "x")
        z.writestr("__MACOSX/junk.txt", "x")
        z.writestr("ok.txt", "x")
    ex2, err, _fresh = bridge.Api._extract_zip(evil)
    check(not err and not os.path.exists(os.path.join(os.path.dirname(ws), "pwned.txt"))
          and not os.path.exists(os.path.join(tempfile.gettempdir(), "pwned.txt")),
          "压缩包里的 ../ 路径被拦截（zip slip）", err or "已拦截")
    check(not os.path.isdir(os.path.join(ex2, "__MACOSX")), "跳过 __MACOSX")

    # ---------- 6b. 嵌套文件夹 + 压缩包递归 ----------
    print("\n" + "─" * 70)
    print("6b) 拖一个总文件夹：递归 + 压缩包解套（v2.4）")
    import zipfile as _z2

    ws3 = tempfile.mkdtemp(prefix="invoice_nested_")
    root = os.path.join(ws3, "白泽报销")
    os.makedirs(os.path.join(root, "子文件夹"))
    open(os.path.join(root, "根目录发票.pdf"), "w").close()
    open(os.path.join(root, "子文件夹", "子目录发票.pdf"), "w").close()
    # 压缩包里套一个文件夹，文件夹里放 PDF
    with _z2.ZipFile(os.path.join(root, "报销.zip"), "w") as z:
        z.writestr("里层/a.pdf", "x")
        z.writestr("里层/b.pdf", "x")
    # 终极嵌套：压缩包 → 文件夹 → 压缩包 → PDF
    import io as _io
    inner = _io.BytesIO()
    with _z2.ZipFile(inner, "w") as zi:
        zi.writestr("深层发票.pdf", "x")
    with _z2.ZipFile(os.path.join(root, "嵌套.zip"), "w") as z:
        z.writestr("一层/二层.zip", inner.getvalue())

    a2 = bridge.Api()
    a2.task = a2._idle_task()
    # Api() 会读用户真实的 config.json，recursive 可能本来就是开的 ——
    # 必须先按到「关」才能验证「拖文件夹会自动打开它」
    a2.cfg["recursive"] = False
    r = a2.accept_drop([root])
    check(r.get("ok") and r.get("mode") == "folder", "拖总文件夹 → 文件夹模式")
    check(r.get("sub") is True, "自动打开了「包含子文件夹」（并在返回值里如实报告）")
    check(a2.cfg["recursive"] is True, "递归开关写回了配置（界面开关会跟着变）")
    check(len(r.get("extracted") or []) == 3,
          "3 个压缩包被解开（含嵌套里那层）", r.get("extracted"))
    check(not r.get("skipped"), "没有跳过项", r.get("skipped"))

    a2.task = a2._idle_task()
    # 递归扫描应该能找到全部 5 个 PDF
    found = bridge.IP._walk_invoice_files(root, True)
    names = sorted(os.path.basename(p) for p in found)
    check(len(found) == 5, "递归扫到 5 个 PDF（根 1 + 子目录 1 + 压缩包 2 + 嵌套 1）", names)
    check(any("深层发票" in n for n in names), "最深那层的 PDF 也被找到")

    # 重复导入不应重复解压，也不能覆盖已有内容
    marker = os.path.join(root, "报销_解压", "别动我.txt")
    open(marker, "w", encoding="utf-8").write("已有")
    a2.task = a2._idle_task()
    # 绕开「3 秒内同一批路径防重复触发」的保护，模拟用户过一会儿又拖了一次
    a2._lastDrop = ("", 0.0)
    r2 = a2.accept_drop([root])
    a2.task = a2._idle_task()
    check(len(r2.get("extracted") or []) == 0,
          "重复导入不再报「新解压」", r2.get("extracted"))
    check(a2.importInfo.get("reused") == 3,
          "如实报告 3 个是复用的", a2.importInfo.get("reused"))
    check(os.path.exists(marker), "复用时不覆盖已有文件")
    shutil.rmtree(ws3, ignore_errors=True)

    # ---------- 6c. set_all 只作用于指定行 ----------
    print("\n" + "─" * 70)
    print("6c) set_all 的 idxs 参数（搜索时「全选」只选可见行）")
    a3 = bridge.Api()
    a3.items = [bridge.IP.InvoiceInfo("x%d.pdf" % i) for i in range(5)]
    for it in a3.items:
        it.ok = True
    a3.checked = [False] * 5

    a3.set_all(True)                                  # 老式两参数调用
    check(a3.checked == [True] * 5, "不带 idxs 时行为不变（全选）")
    a3.checked = [False] * 5
    a3.set_all(True, False, [1, 3])                   # 只选 1、3 行
    check(a3.checked == [False, True, False, True, False],
          "带 idxs 时只勾选指定的行", a3.checked)
    a3.set_all(False, False, [1, 3])
    check(a3.checked == [False] * 5, "同样支持「只取消这几行」")
    # 非法索引要被安静丢掉，不能越界
    a3.set_all(True, False, [0, 99, -1, "x", None])
    check(a3.checked == [True, False, False, False, False],
          "非法索引被过滤掉（不越界、不报错）", a3.checked)
    a3.checked = [False] * 5
    a3.set_all(True, False, [])
    check(a3.checked == [False] * 5, "空数组 = 什么都不选（而不是全选）")

    shutil.rmtree(ws, ignore_errors=True)

    # ---------- 6d. remove_rows：从列表移除（不删文件） ----------
    print("\n" + "─" * 70)
    print("6d) 移除勾选的行")
    import invoice_parser as _IP

    def _mk_api(n, marked=None):
        a = bridge.Api()
        a.items = [_IP.InvoiceInfo("x%d.pdf" % i) for i in range(n)]
        for i, it in enumerate(a.items):     # 列表推导的 i 不会外泄，必须重新枚举
            it.ok = True
            it.buyer = "公司%d" % i
        a.checked = [True] * n
        a.edited = dict(marked or {})
        a.task = a._idle_task()
        return a

    a4 = _mk_api(5)
    r = a4.remove_rows([1, 3])
    check(r.get("ok") and r["removed"] == 2, "移除 2 行", r.get("removed"))
    check(len(a4.items) == 3, "列表只剩 3 行", len(a4.items))
    check([it.buyer for it in a4.items] == ["公司0", "公司2", "公司4"],
          "留下的是没被勾选的那几行", [it.buyer for it in a4.items])
    check(len(a4.checked) == len(a4.items), "勾选状态数组跟着一起收缩（不会错位）")

    # 手动改过的名字必须跟着行走，不能串到别的行上
    a5 = _mk_api(5, {0: "手改0", 3: "手改3", 4: "手改4"})
    a5.remove_rows([1, 2])
    check(a5.edited.get(0) == "手改0", "前面的自定义名不受影响")
    check(a5.edited.get(1) == "手改3" and a5.edited.get(2) == "手改4",
          "后面的自定义名索引被正确重排（不会串行）", a5.edited)

    # 发票被移除时，挂在它下面的行程单要一起走（否则会变成孤儿）
    a6 = _mk_api(3)
    a6.pairs = {"attach": {0: [{"trip": 2, "score": 100, "reason": "金额一致"}]},
                "pending": [], "pendingMap": {}}
    r = a6.remove_rows([0])
    check(r["removed"] == 2, "移除发票时连带移除它挂的行程单", r["removed"])
    check(len(a6.items) == 1, "只剩没被牵连的那行")

    bad = a6.remove_rows([])
    check(bad.get("ok") is False, "没勾选时明确报错", bad.get("msg"))
    bad = a6.remove_rows([99, -1, "x"])
    check(bad.get("ok") is False, "全是越界/脏行号时明确报错", bad.get("msg"))
    check(len(a6.items) == 1, "越界行号没有误删任何东西")

    # 混着合法值时：合法的照常移除，脏值丢掉（不能让一个脏值废掉整批）
    a7 = _mk_api(3)
    a7.remove_rows(["x", 0, None, 99])
    check(len(a7.items) == 2, "混入脏值时只处理合法的行号", len(a7.items))

    # ---------- 7. 拖放监听器装配 ----------
    print("\n" + "─" * 70)
    print("7) 拖放监听器装配（app._wire_file_drop）")
    # 这条是回归防线：`webview.DOMEventHandler` 在顶层根本不存在
    # （正确路径是 webview.dom.DOMEventHandler）。当时外面套了 try/except，
    # 于是拖放**静默失效** —— 界面正常、拖文件没反应，只有打包后的
    # 端到端探针才抓出来。这里用假窗口把装配逻辑单独跑一遍。
    import app as app_mod

    class _Holder:
        """模拟 pywebview 的事件容器：支持 `events.drop += handler`"""
        def __init__(self, sink):
            self.sink = sink

        def __iadd__(self, h):
            self.sink.append(h)
            return self

    class _Ev:
        """模拟 EventContainer：任意事件名都按需生成容器"""
        def __init__(self):
            self.handlers = []

        def __getattr__(self, name):
            holder = _Holder(self.handlers)
            setattr(self, name, holder)
            return holder

    class _El:
        def __init__(self):
            self.events = _Ev()

    class _Dom:
        def __init__(self):
            self.body = _El()

    class _Win:
        def __init__(self):
            self.dom = _Dom()

    target_api = bridge.Api()
    got = []
    target_api.accept_drop = lambda paths: got.append(list(paths))
    win = _Win()
    app_mod._wire_file_drop(win, target_api)
    check(len(win.dom.body.events.handlers) == 1,
          "注册了 1 个 drop 监听器（必须有：pywebview 只在此监听器存在时才记录路径）",
          len(win.dom.body.events.handlers))
    check(getattr(target_api, "_dropErr", "") == "",
          "装配过程没有异常", getattr(target_api, "_dropErr", ""))
    if win.dom.body.events.handlers:
        h = win.dom.body.events.handlers[0]
        # 主通道：pywebview 回配成功时（本机实测能成功），回调拿到
        # pywebviewFullPath 就该直接导入 —— 这是最准的一条数据。
        h.callback({"dataTransfer": {"files": [
            {"pywebviewFullPath": "C:/a.pdf", "name": "a.pdf"},
            {"name": "b.pdf"},                       # 没有路径 → 要能扛住
            {"pywebviewFullPath": "", "name": "c.pdf"},
        ]}})
        check(got == [["C:/a.pdf"]],
              "回配成功时主通道直接导入（只带真实路径的那些）", got)
        check(any(x["stage"] == "event" for x in target_api._dropLog),
              "记下了诊断日志，便于排查")

        # 回配全失败 → 退化到从宿主记录表里捞
        from webview.dom import _dnd_state as _ds
        _ds["paths"] = [("d.pdf", "C:/d.pdf")]
        h.callback({"dataTransfer": {"files": [{"name": "d.pdf"}]}})
        check(got[-1] == ["C:/d.pdf"],
              "回配失败时从宿主记录表兜底捞出路径", got[-1] if got else None)
        check(_ds["paths"] == [], "兜底捞完要清表，避免下次重复")

        # 畸形事件不能抛异常
        h.callback({})
        h.callback({"dataTransfer": {"files": [{"bad": 1}, None, "x"]}})
        h.callback({"dataTransfer": None})
        check(True, "畸形/空事件不抛异常")

    # ---------- 8. take_drop_paths（补救通道） ----------
    print("\n" + "─" * 70)
    print("8) take_drop_paths（补救通道）")
    # 主通道是 app._wire_file_drop 里的回调（拿 pywebviewFullPath）。
    # 这条是兜底：pywebview 回配失败时，把宿主记录表里剩下的捞出来。
    from webview.dom import _dnd_state
    drop2 = bridge.Api()
    drop2.task = drop2._idle_task()

    ws2 = tempfile.mkdtemp(prefix="invoice_take_")
    p1 = os.path.join(ws2, os.path.basename(pdf))
    shutil.copy(pdf, p1)

    _dnd_state["paths"] = [(os.path.basename(p1), p1)]
    r = drop2.take_drop_paths()
    check(r.get("ok") is True, "捞到路径并触发导入：%s" % r.get("mode"))
    check(p1 in (drop2.files or []), "路径进了待处理清单")
    check(_dnd_state["paths"] == [], "捞走后清表（不会重复导入）")
    drop2.task = drop2._idle_task()

    # 表空了 = 主通道已经处理完（正常情况）→ 必须**安静**返回，
    # 绝不能报错，否则用户会遇到「明明导进去了却弹报错」。
    r = drop2.take_drop_paths()
    check(r.get("ok") is False and r.get("quiet") is True,
          "没东西可捞时安静返回（不谎报失败）：%s" % r)

    # 中文名 + 重名都无所谓（不做名字匹配，这正是补救通道的价值）
    cn = os.path.join(ws2, "中文发票名称（测试）.pdf")
    shutil.copy(pdf, cn)
    _dnd_state["paths"] = [("x.pdf", cn), ("x.pdf", cn)]   # 故意同名同路径
    r = drop2.take_drop_paths()
    check(r.get("ok") is True and cn in (drop2.files or []),
          "中文名/同名文件照样能导入（不依赖文件名匹配）")
    check((drop2.files or []).count(cn) == 1, "重复路径被去重")

    # 防重复：两条通道可能同时触发，同一批路径在时间窗内只处理一次
    drop2.task = drop2._idle_task()
    _dnd_state["paths"] = [(os.path.basename(p1), p1)]
    drop2.take_drop_paths()
    before_files = list(drop2.files or [])
    r = drop2.accept_drop([p1])          # 立即再来一次同样的路径
    check(r.get("mode") == "dedup",
          "同一批路径短时间内重复触发会被忽略（防两条通道各导一次）", r.get("mode"))
    check(list(drop2.files or []) == before_files, "重复触发没有改变待处理清单")

    shutil.rmtree(ws2, ignore_errors=True)

    # ---------- 7. 「添加文件」是累加，不是覆盖 ----------
    # 业主 2026-10-17 报：第二次点「添加文件」，上一次的整批被覆盖了。
    # 根因是 bridge.add_files 里写了 self.files = list(res)。
    print("\n" + "─" * 70)
    print("7) 添加文件是累加（不是覆盖）")
    other = os.path.join(
        TESTS, "dzfp_26312000005847746827_深圳某某服务有限公司_20260917110927.pdf")
    add = bridge.Api()
    add.items = [IP.parse_invoice(pdf)]      # 假装列表里已经有一张
    add.checked = [True]
    add.files = []
    n_base = len(add.items)
    added = add._append_paths([other])        # 再加**另一份**
    check(len(add.files) == n_base + 1, "已有 1 行时再加 1 份 → 清单变 2 条",
          "%d 行 → %d 条" % (n_base, len(add.files)))
    check(added == 1, "新增计数 = 1", added)
    check(os.path.abspath(pdf) in [os.path.abspath(x) for x in add.files],
          "原来那一份还在（没被覆盖）", [os.path.basename(x) for x in add.files])

    # 真实流程里 add_files 之后会 rescan，items 跟着刷新 —— 这里手动补上，
    # 否则下一次 _append_paths 还会把同一份当成"新的"
    add.items = add.items + [IP.parse_invoice(other)]
    added2 = add._append_paths([other])       # 再加同一份
    check(added2 == 0 and len(add.files) == n_base + 1,
          "同一份重复添加不会多出一行", "新增 %d 条 / 共 %d 条" % (added2, len(add.files)))

    # 打底必须是「当前列表里的行」，不能是旧的 files ——
    # 否则用户用行末垃圾桶删掉的那几行，会因为下一次添加而复活
    add.items = []                            # 模拟：列表被删空了
    add._append_paths([other])
    got = [os.path.abspath(x) for x in add.files]
    check(os.path.abspath(pdf) not in got,
          "已移除的行不会因为再次添加而复活", [os.path.basename(x) for x in got])

    # ---------- 8. v2.6：缓存 / 长路径 / 按批撤销 / 多票拆分 ----------
    print("\n" + "─" * 70)
    print("8) v2.6 新能力")

    # 8a 解析缓存：命中要一致、drop 要能清掉
    from cache_core import CACHE, CACHE_VERSION
    CACHE.clear()
    cached_src = os.path.join(
        TESTS, "dzfp_26312000005847746823_上海某某文化传媒有限公司_20260915141205.pdf")
    fresh = IP.parse_invoice(cached_src, use_cache=False)
    CACHE.put(cached_src, IP.info_to_dict(fresh))
    hit = IP.parse_invoice(cached_src)
    check(hit.buyer == fresh.buyer and hit.amount == fresh.amount
          and hit.invoice_no == fresh.invoice_no,
          "缓存命中后字段与原解析一致",
          "%s / %s" % (hit.buyer, hit.amount))
    check(not hasattr(hit, "raw_text") or hit.raw_text == "",
          "缓存不保存 raw_text（省空间，也没人读）")
    CACHE.drop(cached_src)
    check(CACHE.get(cached_src) is None, "drop() 之后这条缓存确实没了")
    CACHE.clear()

    # 8b 长路径：短路径不动，超长路径加 \\?\ 前缀
    short = os.path.join(TESTS, "a.pdf")
    check(RC.long_path(short) == os.path.abspath(short),
          "短路径不加前缀（避免 UNC 那些边角情况）")
    deep = "C:\\" + "\\".join(["很长的客户公司名称"] * 26) + "\\票.pdf"
    check(len(deep) >= 250, "测试路径本身够长（%d 字符）" % len(deep))
    lp = RC.long_path(deep)
    check(lp.startswith("\\\\?\\"), "超长路径自动加 \\\\?\\ 前缀", lp[:12])

    # 8c 按批撤销：两批改名，只回滚第一批
    ws = tempfile.mkdtemp(prefix="undo_")
    f1 = os.path.join(ws, "a.txt")
    f2 = os.path.join(ws, "b.txt")
    open(f1, "w").close()
    open(f2, "w").close()

    class _P:
        def __init__(s, src, dst, action="rename"):
            s.src, s.dst, s.action = src, dst, action

    r1 = RC.Runner([_P(f1, os.path.join(ws, "a_新.txt"))])
    r1.run()
    RC.append_log(ws, r1.log)
    r2 = RC.Runner([_P(f2, os.path.join(ws, "b_新.txt"))])
    r2.run()
    RC.append_log(ws, r2.log)

    hist = RC.history(ws)
    check(len(hist) == 2, "两批改名在历史里是两条", "%d 条" % len(hist))
    check(hist[0]["batch"] != hist[1]["batch"], "两批的批次号不同")
    check(all(h["undoable"] == 1 for h in hist), "两批都标着可撤销 1 个")

    _ops, done = RC.undo_batch(ws, r1.batch_id)
    check(done == 1, "撤销第一批：还原 1 个", done)
    check(os.path.exists(f1) and not os.path.exists(os.path.join(ws, "a_新.txt")),
          "第一批已经改回原名")
    check(os.path.exists(os.path.join(ws, "b_新.txt")),
          "**第二批没被动**（这正是老版本做不到的）")
    hist2 = RC.history(ws)
    first = [h for h in hist2 if h["batch"] == r1.batch_id][0]
    check(first["undoable"] == 0, "被撤的那一批不再标可撤销")
    shutil.rmtree(ws, ignore_errors=True)

    # 8d 多票 PDF：3 张票合并成一个文件 → 能识别、能拆、拆完各自能读
    ws2 = tempfile.mkdtemp(prefix="multi_")
    merged = os.path.join(ws2, "客户合并件.pdf")
    doc = pymupdf.open()
    for p in sorted(glob.glob(os.path.join(TESTS, "*.pdf")))[:3]:
        d = pymupdf.open(p)
        doc.insert_pdf(d)
        d.close()
    doc.save(merged)
    doc.close()

    mi = IP.parse_invoice(merged, use_cache=False)
    check(mi.multi is not None and mi.multi.get("count") == 3,
          "识别出「一个 PDF 里有 3 张票」",
          (mi.multi or {}).get("count"))
    made, err = IP.split_pdf_by_groups(merged, (mi.multi or {}).get("groups") or [])
    check(len(made) == 3, "拆成了 3 个文件", "%d 个 %s" % (len(made), err or ""))
    okc = 0
    for m in made:
        ii = IP.parse_invoice(m, use_cache=False)
        if ii.ok:
            okc += 1
    check(okc == 3, "拆出来的每一个都能正常读出字段", "%d/3" % okc)
    shutil.rmtree(ws2, ignore_errors=True)

    print("\n" + "=" * 70)
    if fails:
        print("未通过 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("桥接层冒烟测试全部通过 ✅")
if __name__ == "__main__":
    main()
