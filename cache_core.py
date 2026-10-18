# -*- coding: utf-8 -*-
"""
解析结果缓存（v2.6 新增）。

为什么需要：
    一次扫 800 张票，光 PDF 文本层抽取就要十几秒。而用户关掉软件再打开、
    或者改个设置点「重新识别」，之前的结果全丢，白干一遍。第二次体验极差。

怎么做：
    key = 绝对路径 + 文件大小 + 修改时间(纳秒)
    文件没动过 → 直接复用；动过（重新下载、改名、编辑）→ key 变了，自然失效，
    不需要用户手动清缓存，也不会拿到过期数据。

存哪里：
    %LOCALAPPDATA%\\InvoiceRenamer\\parse_cache.json
    · 不放程序目录 —— 程序可能装在 Program Files，普通用户没写权限
    · 不放发票目录 —— 那是人家的业务文件夹，别塞我们的东西进去

⚠️ CACHE_VERSION：**解析逻辑一改就要 +1**，否则老结果会盖住新逻辑。
   改完 invoice_parser 记得同步升这个号（和 version.py 的版本号是两回事）。
"""

import json
import os
import tempfile
import threading
import time

CACHE_VERSION = 3          # v2.6：加了多票检测 / 加密标记，必须让老缓存失效
APP_DIR_NAME = "InvoiceRenamer"
FILE_NAME = "parse_cache.json"


def cache_dir():
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or tempfile.gettempdir())
    d = os.path.join(base, APP_DIR_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = tempfile.gettempdir()
    return d


def cache_file():
    return os.path.join(cache_dir(), FILE_NAME)


class ParseCache:
    """进程内单例。线程安全（解析是并发的，必须加锁）"""

    def __init__(self, limit=20000):
        self._lock = threading.RLock()
        self._data = {}
        self._dirty = False
        self._limit = limit
        self._load()

    def _load(self):
        try:
            with open(cache_file(), "r", encoding="utf-8") as f:
                raw = json.load(f)
            if raw.get("version") == CACHE_VERSION:
                self._data = raw.get("entries") or {}
        except Exception:
            self._data = {}

    @staticmethod
    def key(path):
        """路径 + 大小 + mtime 组成 key；取不到 stat 就返回空（不缓存）"""
        try:
            st = os.stat(path)
        except OSError:
            return ""
        try:
            norm = os.path.abspath(path).lower()
        except Exception:
            norm = str(path)
        return "%s|%d|%d" % (norm, st.st_size, st.st_mtime_ns)

    def get(self, path):
        k = self.key(path)
        if not k:
            return None
        with self._lock:
            return self._data.get(k)

    def put(self, path, entry):
        k = self.key(path)
        if not k or not entry:
            return
        # 先确认能序列化 —— 存进去一个不能 dump 的对象，会让整个缓存文件写失败
        try:
            json.dumps(entry, ensure_ascii=False)
        except Exception:
            return
        with self._lock:
            self._data[k] = entry
            self._dirty = True
            if len(self._data) > self._limit:
                # 超量就丢最早的（dict 保持插入顺序，够用了）
                for old in list(self._data)[: len(self._data) - self._limit]:
                    self._data.pop(old, None)

    def drop(self, path):
        """
        删掉某一条缓存。

        必须有的场景：加密 PDF 一开始缓存的是「需要密码」的失败结果，
        用户输完密码后如果不清掉，parse_invoice 还会把那条失败记录还回来 ——
        表现就是「密码明明是对的，却一直说需要密码」。
        """
        k = self.key(path)
        if not k:
            return False
        with self._lock:
            hit = self._data.pop(k, None)
            if hit is not None:
                self._dirty = True
            return hit is not None

    def save(self, force=False):
        with self._lock:
            if not self._dirty and not force:
                return False
            data = dict(self._data)
            self._dirty = False
        try:
            tmp = cache_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": CACHE_VERSION,
                           "saved": int(time.time()),
                           "entries": data}, f, ensure_ascii=False)
            os.replace(tmp, cache_file())
            return True
        except Exception:
            return False

    def clear(self):
        with self._lock:
            self._data = {}
            self._dirty = True
        self.save(force=True)
        return True

    def stats(self):
        with self._lock:
            return {"count": len(self._data), "file": cache_file(),
                    "version": CACHE_VERSION}


CACHE = ParseCache()
