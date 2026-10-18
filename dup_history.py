# -*- coding: utf-8 -*-
"""
跨批次「重复报销」台账（v2.7）。

## 为什么要有这个文件

原来的「重复票号」只在**当前这一批文件里**找：同一个文件夹里出现两份同票号，
能查出来。但真实场景是——9 月已经处理过的一张票，10 月又从别的渠道发过来，
混进了本月的文件夹。这种重复**跨批次**，用批内比对永远查不出来，
而它恰恰是财务最容易踩的坑（同一张票报两次销）。

## 存什么

只存「处理过哪些票」的最小信息，键是发票号码：

    {
      "253120000002764865": {
        "amount": 94222.21,        # 价税合计，用来区分「红冲重开」
        "date": "2026-09-12",      # 开票日期
        "at": "2026-09-12 14:22",  # 我们处理它的时间
        "folder": "D:/9月发票",     # 上次在哪个文件夹
        "file": "上海某某—94222.21.pdf"
      }
    }

**只存在本机**（%LOCALAPPDATA%\\InvoiceRenamer\\history_index.json），
不联网、不上传、不进安装包。代理记账最忌讳把客户票号传到外面。

## 什么算「处理过」

只有真正执行了重命名/归档（bridge.execute 成功的那部分）才记一笔。
只是「打开看了看」不算 —— 否则第一次扫描就把整个文件夹记成"已报过"，
下次再打开满屏都是警告，等于没有提醒。
"""

import json
import os
import tempfile
import threading
from datetime import datetime


def _default_path():
    # 测试专用：自动化测试跑改名会往台账里写真实票号，
    # 必须能把它指到临时文件去，否则测完用户的软件里全是"已报过"的假提醒。
    custom = os.environ.get("INVOICE_HISTORY_INDEX")
    if custom:
        return custom
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") \
        or os.path.expanduser("~")
    return os.path.join(base, "InvoiceRenamer", "history_index.json")


class HistoryIndex:
    """票号 → 上次处理信息。线程安全，原子落盘。"""

    MAX_ITEMS = 20000          # 兜底上限，超了丢最旧的，避免文件无限膨胀

    def __init__(self, path=None):
        self.path = path or _default_path()
        self._lock = threading.Lock()
        self.map = {}
        self._load()

    # ---------------- 读写 ----------------
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.map = data
        except (OSError, ValueError):
            self.map = {}

    def save(self):
        with self._lock:
            data = dict(self.map)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path),
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)      # 原子替换，断电也不会留下半个文件
        except OSError:
            pass

    # ---------------- 对外 ----------------
    def record(self, entries):
        """
        记一批「刚处理过」的票。

        entries: [{"no":票号, "amount":金额, "date":开票日期,
                   "folder":文件夹, "file":文件名}]
        返回实际新增/更新的条数。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        n = 0
        with self._lock:
            for e in entries or []:
                no = (e.get("no") or "").strip()
                if not no:
                    continue
                self.map[no] = {
                    "amount": e.get("amount"),
                    "date": e.get("date") or "",
                    "at": now,
                    "folder": e.get("folder") or "",
                    "file": e.get("file") or "",
                }
                n += 1
            # 太多了就按处理时间砍掉最旧的一批（正常用几年也到不了）
            if len(self.map) > self.MAX_ITEMS:
                keep = sorted(self.map.items(),
                              key=lambda kv: kv[1].get("at") or "",
                              reverse=True)[:self.MAX_ITEMS]
                self.map = dict(keep)
        if n:
            self.save()
        return n

    def lookup(self, no, amount=None, folder=None):
        """
        查这张票以前处理过没有。

        · 金额不一致 → 不算重复（票号相同但金额变了 = 红冲重开，是正常业务）
        · 同一个文件夹 → 不算重复（用户只是把同一个文件夹又打开看了一遍）
        · 返回 {at, folder, file, amount} 或 None
        """
        no = (no or "").strip()
        if not no:
            return None
        with self._lock:
            rec = self.map.get(no)
        if not rec:
            return None
        if amount is not None and rec.get("amount") is not None:
            try:
                if abs(float(rec["amount"]) - float(amount)) > 0.005:
                    return None
            except (TypeError, ValueError):
                pass
        if folder and rec.get("folder"):
            try:
                if os.path.normcase(os.path.abspath(folder)) == \
                        os.path.normcase(os.path.abspath(rec["folder"])):
                    return None
            except (OSError, ValueError):
                pass
        return rec

    def count(self):
        with self._lock:
            return len(self.map)

    def clear(self):
        with self._lock:
            self.map = {}
        self.save()


INDEX = HistoryIndex()
