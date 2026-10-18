# -*- coding: utf-8 -*-
"""
PDF 合并
--------------------------------------------------
把若干份 PDF 按给定顺序合成一个文件。

为什么单独一个模块：这是「对文件**内容**」的操作，
跟 rename_core 的「改文件名 / 备份 / 归档」不是一回事，混在一起迟早理不清。

⚠️ 只能合并 PDF。OFD / XML 合不了（PyMuPDF 不支持 OFD 渲染），
   调用方要先把它们挑出去，并且把「跳过了哪些、为什么」明确告诉用户 ——
   否则用户只会以为程序漏了文件。
"""
import os

# PyMuPDF 按需加载（v2.7）：顶层 import 会给启动白白加 0.58 秒，
# 而合并功能大多数人一批只用一两次。
from invoice_parser import _fitz


def _open(path):
    """
    打开 PDF。加密的先试空密码。

    ⚠️ 加密文件必须在这里拦掉并给出人话原因：直接丢给 insert_pdf 会抛
       PyMuPDF 的底层错误，用户看到一串英文只会以为是程序坏了。
    """
    doc = _fitz().open(path)
    if getattr(doc, "needs_pass", False):
        if not doc.authenticate(""):
            doc.close()
            raise ValueError("文件已加密，需要密码才能打开")
    return doc


def page_count(path):
    """读页数。失败返回 0 —— 页数只是展示用，不值得让整件事失败。"""
    try:
        d = _open(path)
        n = d.page_count
        d.close()
        return n
    except Exception:
        return 0


def unique_path(path):
    """
    不覆盖已有文件：`xxx.pdf` → `xxx (2).pdf`。

    ⚠️ 合并产物**绝不能**覆盖用户已有的文件 —— 那可能是他上一次的合并结果。
    """
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while True:
        cand = "%s (%d)%s" % (stem, n, ext)
        if not os.path.exists(cand):
            return cand
        n += 1


def merge(paths, out_path, bookmark=True, on_progress=None):
    """
    按 `paths` 给的顺序合并成一个 PDF。

    返回：
        {"ok", "out", "files", "pages", "bytes", "errors"}
        · out    —— 实际写出的路径（可能与入参不同，见 unique_path）
        · errors —— 中途失败的文件（加密/损坏），不中断整体，只记录

    单份失败**不中断**：10 份里坏了 1 份，另外 9 份照样要合出来，
    最后把坏的那份列给用户看 —— 比整个任务失败有用得多。
    """
    out = _fitz().open()
    toc = []
    page = 1                 # 书签页码从 1 开始
    done, errors = [], []
    total = len(paths)

    try:
        for n, p in enumerate(paths, 1):
            if on_progress:
                on_progress(n - 1, total, "正在合并：" + os.path.basename(p))
            try:
                src = _open(p)
            except Exception as exc:
                errors.append({"file": os.path.basename(p), "error": str(exc)})
                continue
            try:
                out.insert_pdf(src)
                if bookmark:
                    # 用文件名（不含扩展名）做一级书签，页码是这份文件的起始页
                    toc.append([1, os.path.splitext(os.path.basename(p))[0], page])
                page += src.page_count
                done.append(p)
            except Exception as exc:
                errors.append({"file": os.path.basename(p), "error": str(exc)})
            finally:
                src.close()

        if not done:
            raise RuntimeError("没有一份 PDF 能合并成功")

        if bookmark and toc:
            out.set_toc(toc)

        if on_progress:
            on_progress(total, total, "正在写入文件…")

        final = unique_path(out_path)
        # garbage=3 清理冗余对象、deflate 压缩流：发票扫描件动辄几十 MB，
        # 不压一下合并结果会大得离谱
        out.save(final, garbage=3, deflate=True)
        pages = out.page_count
    finally:
        out.close()

    return {"ok": True, "out": final, "files": len(done),
            "pages": pages, "bytes": os.path.getsize(final),
            "errors": errors}
