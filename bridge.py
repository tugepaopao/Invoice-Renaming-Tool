# -*- coding: utf-8 -*-
"""
前后端桥接层
--------------------------------------------------
pywebview 的 js_api 对象。前端通过 window.pywebview.api.xxx() 调用这里的方法。

路径约定（重要）：
  · 资源目录（web/index.html 等）：随程序打包，用 _MEIPASS
  · 数据目录（config.json）：必须落在「exe 所在目录」，否则卸载/退出就丢
    —— 这是之前 tkinter 版在 exe 下丢掉设置的根因
"""
import csv
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import check_core as CC
import dup_history as DH
import invoice_parser as IP
import merge_core as MC
import pair_core as PC
import preview_core as PV
import rename_core as RC
from cache_core import CACHE
from version import APP_NAME, AUTHOR, VERSION


# ---------------------------------------------------------------- 路径
def resource_dir():
    """静态资源目录（web/ 所在处）"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """
    配置等需要持久保存的数据目录。

    优先放 exe 同级目录 —— 这样整个文件夹拷到别的电脑，设置也跟着走。
    但如果那个目录不可写（例如装到了 Program Files 且没有管理员权限），
    自动回退到 %APPDATA%\\发票批量重命名工具，避免设置存不下来。
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))

    if os.access(base, os.W_OK):
        return base

    alt = os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"),
        "发票批量重命名工具")
    try:
        os.makedirs(alt, exist_ok=True)
    except Exception:
        pass
    return alt


CONFIG_PATH = os.path.join(data_dir(), "config.json")

DEFAULTS = {
    "template": "{buyer}—{amount}",
    # ---- 勾选式「命名规则」（v2.1 起）----
    # fields 是有序数组：顺序 = 用户勾选后拖拽排定的先后顺序
    "fields": ["buyer", "amount"],
    "separator": "—",
    "amountMode": "smart",
    "currencyPrefix": "",
    "dedup": "index",
    "mode": "rename",
    "distribute": False,
    "backup": True,
    "recursive": False,
    "lastFolder": "",
    # 命名模板库（v2.6）：每套存 {id, name, fields, separator, amountMode, currencyPrefix}
    # 存在配置里，跟着用户走；最多留 12 套（见 save_template）
    "templates": [],
    # 跨批次重复报销预警（v2.7）：按票号记忆处理过的票，扫描时提醒
    "crossCheck": True,
    # 界面记住的视图设置（v2.7）：排序方式 + 规则卡是否折叠
    "view": {"sort": "file", "folded": True},
}

# 可勾选字段清单（顺序即面板里的展示顺序）
#   key  : 占位符名，与 invoice_parser.build_name 的 data 键一致
#   label: 界面上显示的中文名
#   ex   : 示例值 —— 只用于「效果」预览的演示，不在字段列表里显示
#   hint : 鼠标悬停时的补充说明
#
# ⚠️ 「发票金额」与「价税合计」是两个不同字段（票面术语）：
#      发票金额 = 票面「合计 金额」栏 → 不含税
#      发票税额 = 票面「合计 税额」栏
#      价税合计 = 上面两者之和 → 含税
#    历史配置里的 amount 字段标签由「发票金额」改为「价税合计」，
#    取值逻辑没变，所以老规则生成的文件名不变。
FIELD_DEFS = [
    {"key": "type",       "label": "发票类型",     "ex": "增值税专用发票",
     "hint": "专票 / 普票"},
    {"key": "no",         "label": "发票号码",     "ex": "26312000005847746821",
     "hint": "20 位发票号码"},
    {"key": "date",       "label": "开票日期",     "ex": "20260915",
     "hint": "格式如 20260915"},
    {"key": "buyer",      "label": "购买方名称",   "ex": "上海某某科技有限公司",
     "hint": "付款方公司名"},
    {"key": "buyer_tax",  "label": "购买方税号",   "ex": "91310115MA1K3XXXXX",
     "hint": "购买方纳税人识别号"},
    {"key": "seller",     "label": "销售方名称",   "ex": "深圳某某服务有限公司",
     "hint": "开票方公司名"},
    {"key": "seller_tax", "label": "销售方税号",   "ex": "91440300MA5F1YYYYY",
     "hint": "销售方纳税人识别号"},
    {"key": "net",        "label": "发票金额",     "ex": "1000",
     "hint": "票面「合计 金额」栏，不含税"},
    {"key": "tax",        "label": "发票税额",     "ex": "60",
     "hint": "票面「合计 税额」栏"},
    {"key": "amount",     "label": "价税合计",     "ex": "1060",
     "hint": "含税总额，金额 + 税额"},
    {"key": "amount_cn",  "label": "价税合计大写", "ex": "壹仟零陆拾圆整",
     "hint": "票面大写金额"},
    {"key": "remark",     "label": "备注",         "ex": "合同号 HT2026088",
     "hint": "票面备注栏"},
    {"key": "drawer",     "label": "开票人",       "ex": "张会计",
     "hint": "票面开票人"},
]

# 界面清单外的历史字段：老配置里可能勾过，保留能力但不放进选择列表
LEGACY_FIELD_LABELS = {
    "noshort": "发票号码后8位",
    "code": "发票代码",
    "payee": "收款人",
    "reviewer": "复核人",
}

FIELD_KEYS = {f["key"] for f in FIELD_DEFS} | set(LEGACY_FIELD_LABELS)
FIELD_LABEL_OF = dict({f["key"]: f["label"] for f in FIELD_DEFS},
                      **LEGACY_FIELD_LABELS)

# 可选分隔符（界面上的快捷按钮）
SEPARATORS = ["—", "-", "_", "·", " ", ""]

# 老版本 template → 新版本 (fields, separator) 的反解表
_TPL_TO_FIELDS = {
    "{buyer}—{amount}": (["buyer", "amount"], "—"),
    "{buyer}-{amount}": (["buyer", "amount"], "-"),
    "{amount}—{buyer}": (["amount", "buyer"], "—"),
    "{buyer}—{amount}({noshort})": (["buyer", "amount", "noshort"], "—"),
    "{date}_{buyer}_{amount}": (["date", "buyer", "amount"], "_"),
    "{buyer}_{amount}_{noshort}": (["buyer", "amount", "noshort"], "_"),
}

# 旧版字段名兼容映射（build_name 的 data 键 → 新 key）
_LEGACY_KEYS = {"buyer": "buyer", "seller": "seller", "amount": "amount",
                "no": "no", "noshort": "noshort", "date": "date",
                "type": "type", "code": "code", "buyer_tax": "buyer_tax",
                "seller_tax": "seller_tax", "net": "net", "tax": "tax",
                "amount_cn": "amount_cn", "remark": "remark",
                "payee": "payee", "reviewer": "reviewer", "drawer": "drawer"}


def template_from_fields(fields, sep="—"):
    """
    把字段列表 + 分隔符合成模板字符串：
        ['date', 'buyer'] + '_'  →  '{date}_{buyer}'

    这个 template 是留给「命令行版」和老配置读的（图形界面走 build_name 的
    fields 分支，不依赖它），但必须拼对 —— 花括号位置错了占位符会整体失效。
    """
    fields = [f for f in (fields or []) if f in FIELD_KEYS]
    if not fields:
        fields = ["buyer", "amount"]
    return str(sep if sep is not None else "—").join(
        "{%s}" % f for f in fields)



def normalize_config(cfg):
    """
    把配置规整成新结构，并保持 template 与 fields 一致。

    兼容两种历史数据：
      1. 老版本只有 template（没有 fields）→ 尽量反解成 fields；
         反解不出来（用户自定义过模板）就把整串当成「自定义内容」保留。
      2. 新版本有 fields → 由 fields + separator 反推 template。
    这样后端其余地方继续读 cfg["template"] 就行，不必到处改。
    """
    if not isinstance(cfg, dict):
        return dict(DEFAULTS)

    fields = cfg.get("fields")
    if isinstance(fields, list) and fields:
        fields = [f for f in fields if f in FIELD_KEYS]
    else:
        fields = None

    if not fields:
        # 尝试从老 template 反解：先查已知预设表，再按分隔符拆分推断
        old = str(cfg.get("template") or "").strip()
        hit = _TPL_TO_FIELDS.get(old)
        if hit:
            fields = list(hit[0])
            cfg["separator"] = hit[1]
        else:
            for sep in ["—", "-", "_", "·", " "]:
                if sep not in old:
                    continue
                parts = [p.strip() for p in old.split(sep)]
                if len(parts) < 2:
                    continue
                # 每一段都得是「{已知字段}」才认，否则说明是用户自写模板
                cand = []
                for p in parts:
                    if len(p) > 2 and p.startswith("{") and p.endswith("}"):
                        k = p[1:-1].strip()
                        if k in FIELD_KEYS:
                            cand.append(k)
                            continue
                    cand = []
                    break
                if cand:
                    fields = cand
                    cfg["separator"] = sep
                    break
        if not fields:
            fields = ["buyer", "amount"]      # 最终兜底

    cfg["fields"] = fields

    sep = cfg.get("separator")
    if sep is None:
        sep = "—"
    cfg["separator"] = sep

    # template 以 fields + separator 为准（用户自定义的整串模板除外，见下）
    cfg["template"] = template_from_fields(fields, sep)

    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


def _vtuple(v):
    """把 2.8.7 变成 (2, 8, 7)，用于比较版本大小（非数字部分按 0 处理）"""
    out = []
    for part in re.split(r"[.\-]", str(v or "")):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    return normalize_config(cfg)


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def money(v):
    """1060.0 -> '1,060.00'"""
    if v is None:
        return ""
    return "{:,.2f}".format(v)


def size_text(n):
    """文件大小：给「重复票取舍台」看谁大谁小（单位习惯用 1KB=1024）"""
    try:
        n = float(n)
    except Exception:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return ""


def mtime_text(ts):
    """修改时间：2026-10-15 17:22"""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def dlg_kind(name):
    """
    取对话框类型。pywebview 6 起推荐 webview.FileDialog.XXX，
    旧的 webview.XXX_DIALOG 已弃用。这里做兼容。
    """
    import webview
    fd = getattr(webview, "FileDialog", None)
    if fd is not None:
        return getattr(fd, name)
    return getattr(webview, {
        "FOLDER": "FOLDER_DIALOG",
        "OPEN": "OPEN_DIALOG",
        "SAVE": "SAVE_DIALOG",
    }[name])


# ---------------------------------------------------------------- API
class Api:
    def __init__(self):
        # 注意：以下两个属性必须用「下划线开头」命名。
        # pywebview 生成 JS API 时会 dir() 遍历 js_api 对象的所有公开属性，
        # 遇到「不是方法、但带 __module__」的对象会递归进去（util.get_functions）。
        # pywebview 的 Window 对象内部挂着庞大的原生对象树，递归会直接爆栈
        # （日志里的 window.native.AccessibilityObject...maximum recursion depth
        # 就是这么来的），进而导致整个 JS API 生成失败 ——
        # 前端表现为 window.pywebview.api.get_state is not a function，
        # 界面停在一片空白/初始状态。下划线开头的属性会被跳过，彻底避开。
        self._window = None
        self.cfg = load_config()
        self.folder = self.cfg.get("lastFolder", "")
        self.files = []                 # 手动添加时的文件列表
        # v2.6：拆开后的「原始整份 PDF」要留在磁盘上，但别再进列表
        # （否则一重扫又冒出来，用户会以为没拆成功）。按绝对路径记。
        self._skip_paths = set()
        self.items = []                 # [InvoiceInfo]
        self.checked = []               # [bool]
        self.edited = {}                # index -> 自定义新名（不含 .pdf）
        self.dups = {}                  # 票号 -> [index]
        # 行号 -> {"gid": 组号, "pos": 组内第几份, "total": 该组共几份}
        # 光标「票号重复」用户在一屏十几行里分不出哪几张是一伙的，
        # 加组号后「组2 第1/3份」就能一眼对上，删的时候不会删错。
        self.dup_groups = {}
        # 上一次拖放导入的摘要（解压到哪、跳过了什么），随快照发给前端做提示。
        # seq 递增是为了让前端知道「这是新的一条」，否则每次重新扫描都会再弹一遍。
        self.importInfo = {"seq": 0, "extracted": [], "skipped": [], "msg": ""}
        self._importSeq = 0
        # 拖放链路是否真的挂上了（由 app.py 的 _wire_file_drop 回填，供自动化断言）
        self._dropReady = False
        self._dropErr = ""
        # 最近几次拖放收到的原始路径。
        # 为什么必须留着：拖放失败时页面只能看到「没反应」，
        # 到底是「事件没到」「路径没拿到」还是「拿到了但没认出文件」完全分不清。
        # 把原始输入留下来，排查和用户反馈都能对得上。
        self._dropLog = []
        # 拖放导入有两条通道（Python 侧的 drop 回调 / 前端调的 take_drop_paths），
        # 它们可能都触发。用「路径签名 + 时间窗」去重，避免同一批文件被扫两遍
        # （扫两遍会让列表先被清空再重建，用户看到闪一下）。
        self._lastDrop = ("", 0.0)
        # ---- 凭证配对与离线校验（v2.5）----
        # pairs["attach"] = {发票行号: [{"trip":行程单行号, "score", "reason"}]}
        # pairs["pending"] = [{"trip":行号, "reason"}]   行程单没配到发票
        self.pairs = {"attach": {}, "pending": []}
        self.checks = {}                # {行号: [校验结果]}，rescan 时算一次缓存住
        # ---- v2.7：两条「事后发现」的信息，都在 _compute_analysis 里算 ----
        # 跨批次重复报销：{行号: {"at","folder","file","amount"}}
        self.seen_before = {}
        # 红字票 → 疑似对应的蓝票：{行号: {"i","file","date","amount"}}
        self.red_match = {}
        # 反向：蓝票 → 冲它的红字票（列表上标「被红冲」）
        self.red_by = {}
        # ---- v2.7：流式扫描的临时状态 ----
        # 大文件夹（几百张）整批读完才刷屏，用户会以为卡死。
        # 现在每读完一张就填进 _live_items，poll 时带一小批行给前端先画出来。
        self._live_items = []
        self._live_total = 0
        self._live_count = 0
        self._live_dirty = False
        self._lock = threading.Lock()
        self.task = self._idle_task()

    # ---------------- 内部 ----------------
    @staticmethod
    def _idle_task():
        return {"busy": False, "kind": "", "done": 0, "total": 0,
                "msg": "", "payload": None, "error": ""}

    def _start(self, kind, fn):
        with self._lock:
            if self.task["busy"]:
                return {"ok": False, "msg": "已有任务在进行中"}
            self.task = {"busy": True, "kind": kind, "done": 0, "total": 0,
                         "msg": "准备中…", "payload": None, "error": ""}

        def run():
            try:
                payload = fn()
                with self._lock:
                    self.task.update(busy=False, payload=payload, msg="完成")
            except Exception as exc:
                with self._lock:
                    self.task.update(busy=False, error=str(exc), msg="失败")

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def _progress(self, done, total, msg=""):
        with self._lock:
            self.task["done"] = done
            self.task["total"] = total
            if msg:
                self.task["msg"] = msg

    # ---------------- 列表快照 ----------------
    def _new_name(self, idx):
        return self._new_name_of(idx, self.items[idx])

    def _new_name_of(self, idx, it):
        """
        算某一行的新文件名。

        v2.7 把「行号 → 取 self.items」这一步拆出来，是为了**流式扫描**：
        扫描过程中 self.items 还没成形，但界面要先把读到的行画出来，
        所以得能拿一个"还没进列表"的 item 单独算名字。
        """
        if idx in self.edited:
            return self.edited[idx]

        # 行程单：名字**跟着发票走**（发票名 + _行程单），
        # 这样两个文件在资源管理器里排序后天然相邻，报销时一眼看出是一套。
        # 老做法是拿它自己的字段拼名，结果「哈啰出行—1410.30」这种名字
        # 跟对应发票看不出半点关系，等于白起。
        if it.kind == "trip":
            owner = self._owner_of_trip(idx)
            if owner is not None:
                base = self.edited.get(owner) or \
                    IP.build_name(self.items[owner], self.cfg["template"],
                                  self.cfg["amountMode"],
                                  self.cfg["currencyPrefix"],
                                  fields=self.cfg.get("fields"),
                                  sep=self.cfg.get("separator", "—"),
                                  suffix=self.cfg.get("suffix", ""))
                return "%s_行程单" % base
            t = it.trip or {}
            bits = [t.get("platform") or "行程单"]
            if t.get("trips"):
                bits.append("%d趟" % t["trips"])
            return "行程单_%s" % "_".join(bits)

        return IP.build_name(it, self.cfg["template"], self.cfg["amountMode"],
                             self.cfg["currencyPrefix"],
                             fields=self.cfg.get("fields"),
                             sep=self.cfg.get("separator", "—"),
                             suffix=self.cfg.get("suffix", ""))

    def _owner_of_trip(self, trip_idx):
        """反查：这份行程单挂在哪个发票下（没挂则 None）"""
        for inv_idx, items in self.pairs.get("attach", {}).items():
            for a in items:
                if a["trip"] == trip_idx:
                    return inv_idx
        return None

    def _row(self, idx, finals=None):
        return self._row_from(idx, self.items[idx], finals)

    def _row_from(self, idx, it, finals=None):
        """
        生成列表一行的数据。

        v2.7 起可以传入「还没进 self.items 的 item」（流式扫描时要边读边画）；
        平时一律走 _row()，不要直接调这个。
        """
        dup = bool(it.invoice_no and it.invoice_no in self.dups)
        # 文件大小/时间取一次就够，别在下面重复 stat 四遍
        try:
            _st = os.stat(it.path)
            _size, _mtime = _st.st_size, _st.st_mtime
        except OSError:
            _size, _mtime = 0, 0
        auto = self._new_name_of(idx, it) + os.path.splitext(it.file_name)[1].lower()
        final = (finals or {}).get(idx, auto)
        renamed = final != auto          # 因为重名被自动加了序号

        is_trip = (it.kind == "trip")
        attached = self.pairs.get("attach", {}).get(idx) or []
        pending = self.pairs.get("pendingMap", {})
        checks = self.checks.get(idx) or []

        status, level = "就绪", "ok"
        if not it.ok:
            status, level = (it.message or "未识别"), "bad"
        elif it.multi:
            # 一个 PDF 里装了好几张票 —— 必须让用户看见，否则后面几张全按第一张算
            status, level = "多张票 ×%d" % (it.multi.get("count") or 2), "dup"
        elif is_trip:
            t = it.trip or {}
            status = "行程单 · %d 趟" % (t.get("trips") or 0)
            level = "trip"
            if idx in pending:
                status, level = "行程单 · 待配对", "tripwait"
        elif checks and CC.worst_level(checks) == "error":
            status, level = "号码异常", "bad"
        elif checks:
            status, level = "待核对", "dup"
        elif idx in self.edited:
            status = "已手动修改"
        elif dup:
            # 后面带「×3」= 这张票一共 3 份。
            # ⚠️ v2.5.5 起**不再**显示「组N 第x份」、也不再用每组一种颜色 ——
            #    业主反馈一屏十几个彩色小标太花，眼睛累。
            #    现在统一一个颜色（琥珀），靠「×N」知道总份数，
            #    具体是哪几份列在右栏「重复票号」清单里，悬停也能看到同组文件名。
            n = (self.dup_groups.get(idx) or {}).get("total", 2)
            status, level = "票号重复 ×%d" % n, "dup"
        elif renamed:
            status, level = "重名·已区分", "dup"
        elif it.is_red:
            status = "红字票"
        return {
            "i": idx,
            "file": it.file_name,
            "path": it.path,
            "fmt": it.fmt or "",          # PDF / OFD / XML，列表上要标出来
            "buyer": it.buyer or "",
            "seller": it.seller or "",
            "amount": it.amount,
            "amountText": money(it.amount),
            "amountShort": IP.format_amount(it.amount, self.cfg["amountMode"],
                                            self.cfg["currencyPrefix"]),
            "no": it.invoice_no or "",
            "date": it.date or "",
            # ---- 扩展字段：详情面板展示用 ----
            "type": it.inv_type or "",
            "code": it.inv_code or "",
            "tax": it.tax,
            "taxText": money(it.tax),
            "net": it.net,
            "netText": money(it.net),
            "buyerTax": it.buyer_tax or "",
            "sellerTax": it.seller_tax or "",
            "amountCn": it.amount_cn or "",
            "remark": it.remark or "",
            "drawer": it.drawer or "",
            "newName": final,
            "autoName": auto,
            "deduped": renamed,
            # ---- 凭证类型与配对（v2.5）----
            "kind": it.kind,               # invoice / trip
            "trip": ({"platform": (it.trip or {}).get("platform", ""),
                      "trips": (it.trip or {}).get("trips", 0),
                      "total": (it.trip or {}).get("total"),
                      "totalText": money((it.trip or {}).get("total")),
                      "from": (it.trip or {}).get("date_from", ""),
                      "to": (it.trip or {}).get("date_to", ""),
                      "rows": len((it.trip or {}).get("rows") or []),
                      } if is_trip else None),
            # 这张发票下面挂的行程单（前端据此渲染附属行）。
            # 直接把摘要带上，前端不用回表查 —— 附属行要显示
            # 「哈啰出行 · 26趟 · 07-21~09-18 · ¥1,410.30」这些信息。
            "attached": [{"i": a["trip"], "score": a["score"],
                          "reason": a["reason"],
                          **self._trip_brief(a["trip"])} for a in attached],
            # 这张行程单为什么没配上（配上了则为空）
            "pairReason": pending.get(idx, ""),
            # 离线风控校验结果（空数组 = 没发现问题）
            "checks": checks,
            "checkLevel": CC.worst_level(checks),
            "ok": it.ok,
            "edited": idx in self.edited,
            "checked": bool(self.checked[idx]) if idx < len(self.checked) else False,
            "dup": dup,
            # 重复组：组号 + 组内第几份，列表据此显示「组2 · 第1/3份」
            "dupGroup": self.dup_groups.get(idx),
            # 同组的其它几份（文件名），用来挂在悬停提示上 ——
            # 用户不必去别处翻，鼠标一停就知道「跟谁撞了」
            "dupOthers": ([{"i": j, "file": self.items[j].file_name}
                           for j in self.dups.get(it.invoice_no, []) if j != idx]
                          if dup else []),
            "red": it.is_red,
            # ---- v2.6 ----
            # 文件本身的信息：取舍台要并排比大小/时间，光看文件名判断不了留哪份
            "size": _size,
            "sizeText": size_text(_size),
            "mtime": _mtime,
            "mtimeText": mtime_text(_mtime),
            # 一个 PDF 里有多张票：{"count": n, "groups": [...]}
            "multi": it.multi,
            # 加密 PDF：前端据此弹「输入密码」
            "needPassword": bool(it.need_password),
            # ---- v2.7 ----
            # 跨批次重复报销：这张票以前处理过吗（同一文件夹不算，金额不同不算）
            "seenBefore": self.seen_before.get(idx),
            # 红字票关联到的蓝票：{"i","file","date","amount"}，没匹配到则为 None
            "redMatch": self.red_match.get(idx),
            # 反向：这张蓝票被哪张红字票冲了（没有则为 None）
            "redBy": self.red_by.get(idx),
            "level": level,
            "status": status,
        }

    def _final_names(self):
        """按当前勾选与规则，算出每一行最终会用到的文件名"""
        idxs, entries = self._build_entries()
        if not entries:
            return {}
        plan = RC.build_plan(entries,
                             copy_mode=(self.cfg["mode"] == "copy"),
                             dedup=self.cfg["dedup"],
                             distribute=self.cfg["distribute"])
        out = {}
        for p in plan:
            if p.dst:
                out[idxs[p.index]] = p.dst_name
        return out

    def _stats(self):
        amounts = [it.amount for it in self.items]
        n, total, pos, neg = RC.summarize(amounts)
        return {
            "count": len(self.items),
            "okCount": sum(1 for it in self.items if it.ok),
            "badCount": sum(1 for it in self.items if not it.ok),
            "checkedCount": sum(1 for c in self.checked if c),
            "amountCount": n,
            "total": money(total),
            "totalRaw": total,
            "pos": money(pos),
            "neg": money(neg),
            "dupCount": len(self.dups),
            "dupRows": sum(len(v) for v in self.dups.values()),
            # v2.7：跨批次「以前处理过的票」有几张，汇总条据此决定要不要提示
            "seenCount": len(self.seen_before),
            # 红字票张数（含匹配到/没匹配到蓝票的）
            "redCount": sum(1 for it in self.items
                            if it.kind == "invoice" and it.ok and it.is_red),
        }

    def _snapshot(self):
        finals = self._final_names()
        return {
            "cfg": self.cfg,
            "folder": self.folder,
            "manual": bool(self.files),
            "rows": [self._row(i, finals) for i in range(len(self.items))],
            "stats": self._stats(),
            "fields": FIELD_DEFS,          # 勾选面板的可选字段清单
            "legacyLabels": LEGACY_FIELD_LABELS,   # 老字段的中文名（仅用于显示已选项）
            "separators": SEPARATORS,      # 分隔符快捷按钮
            "outSubdir": RC.OUT_SUBDIR,
            "backupSubdir": RC.BACKUP_SUBDIR,
            "version": VERSION,            # 左上角显示，用户一眼知道装的是哪版
            "author": AUTHOR,              # 作者署名（顶栏版本号旁边）
            "importInfo": self.importInfo,
            "dropReady": self._dropReady,
            "dropErr": self._dropErr,
            "dropLog": self._dropLog,
            "frozen": bool(getattr(sys, "frozen", False)),
            "configPath": CONFIG_PATH,
        }

    def _compute_analysis(self):
        """
        扫描完成后算两件事（都在 rescan 里跑一次、结果缓存）：
          1. **凭证配对** —— 把行程单挂到对应发票下面
          2. **离线风控校验** —— 号码位数 / 税号校验位 / 大小写金额 / 开票日期

        为什么要缓存：校验要遍历所有票面字段，每次 render 都算会明显卡顿。
        票没变（没重新扫描）就不用重算。
        """
        invs, trips = [], []
        for i, it in enumerate(self.items):
            if it.kind == "trip" and it.trip:
                trips.append((i, it.trip))
            elif it.kind == "invoice" and it.ok:
                invs.append((i, {"amount": it.amount, "seller": it.seller,
                                 "date": it.date}))
        try:
            attach, pending = PC.build_pairs(invs, trips)
        except Exception:
            attach, pending = {}, []      # 配对失败绝不能拖垮扫描
        self.pairs = {"attach": attach, "pending": pending,
                      # 预建好查找表：_row 每行都要查「我为什么没配上」
                      "pendingMap": {p["trip"]: p["reason"] for p in pending}}

        self.checks = {}
        for i, it in enumerate(self.items):
            if it.kind != "invoice" or not it.ok:
                continue
            try:
                res = CC.validate(it)
            except Exception:
                res = []
            if res:
                self.checks[i] = res

        self._compute_cross_batch()
        self._compute_red_match()

    def _compute_cross_batch(self):
        """
        跨批次重复报销预警（v2.7）。

        判据（缺一不可，否则会天天误报，用户很快就学会忽略它）：
          · 票号在历史台账里出现过
          · 金额一致（金额不同 = 红冲重开，正常业务）
          · 不在同一个文件夹（同一个文件夹再打开一次不算"又报了一次"）
        """
        self.seen_before = {}
        if not self.cfg.get("crossCheck", True):
            return
        invs = [(i, it) for i, it in enumerate(self.items)
                if it.kind == "invoice" and it.ok and it.invoice_no]
        if not invs:
            return
        base = self.folder or RC.common_base_dir([it.path for _, it in invs])
        for i, it in invs:
            try:
                rec = DH.INDEX.lookup(it.invoice_no, it.amount, base)
            except Exception:
                rec = None
            if rec:
                self.seen_before[i] = rec

    def _compute_red_match(self):
        """
        红字票 → 疑似对应的蓝票（v2.7）。

        判据：同销售方 + 金额正好相反 + 开票日期相近（<=120 天），取日期最近的一张。
        只是**推测**，界面写「很可能是这张」，允许用户不认 —— 绝不据此改文件名。
        """
        self.red_match = {}
        self.red_by = {}
        blues, reds = [], []
        for i, it in enumerate(self.items):
            if it.kind != "invoice" or not it.ok or it.amount is None:
                continue
            (reds if it.amount < 0 else blues).append(i)
        if not reds or not blues:
            return

        def day_num(s):
            """把 2026-08-12 / 20260812 统一成可比的整数（解析不了就给 None）"""
            d = "".join(ch for ch in str(s or "") if ch.isdigit())
            return int(d[:8]) if len(d) >= 8 else None

        used = set()
        for r in reds:
            red = self.items[r]
            want = -red.amount
            cands = []
            for b in blues:
                blue = self.items[b]
                if blue.amount is None or abs(blue.amount - want) > 0.005:
                    continue
                # 销售方：能取到就要求一致（取不到就不作为否决条件）
                if red.seller and blue.seller and \
                        red.seller.strip() != blue.seller.strip():
                    continue
                dr, db = day_num(red.date), day_num(blue.date)
                gap = abs(dr - db) if (dr and db) else None
                if gap is not None and gap > 120:
                    continue
                cands.append((gap if gap is not None else 9999, b))
            cands.sort(key=lambda x: (x[0], x[1]))
            pick = None
            for gap, b in cands:
                if b not in used:
                    pick = b
                    break
            if pick is None and cands:
                pick = cands[0][1]          # 都被占用了也先挂一个，别让用户摸不着
            if pick is None:
                continue
            used.add(pick)
            blue = self.items[pick]
            self.red_match[r] = {"i": pick, "file": blue.file_name,
                                 "date": blue.date or "",
                                 "amount": money(blue.amount)}
            self.red_by[pick] = {"i": r, "file": red.file_name,
                                 "amount": money(red.amount)}

    def _recompute_dups(self):
        self.dups = RC.find_duplicates(
            [(i, it.invoice_no) for i, it in enumerate(self.items)])
        # 给每个重复组编号（1、2、3…），并记下「这一份是组内第几份/共几份」。
        # 编号按票号排序 —— 同一批数据每次编出来的号一致，不会刷新一下就变。
        self.dup_groups = {}
        for gid, no in enumerate(sorted(self.dups), 1):
            idxs = self.dups[no]
            for pos, i in enumerate(idxs, 1):
                self.dup_groups[i] = {"gid": gid, "pos": pos, "total": len(idxs)}

    # ---------------- 对外：初始化 ----------------
    def get_state(self):
        """
        返回界面所需的全部状态。
        出异常时不能直接抛给前端（前端只会看到一句无法定位的报错），
        所以这里兜住并把完整堆栈写到程序目录下的「启动错误.log」。
        """
        try:
            return self._snapshot()
        except Exception:
            import traceback
            tb = traceback.format_exc()
            try:
                with open(os.path.join(data_dir(), "启动错误.log"),
                          "a", encoding="utf-8") as f:
                    f.write("=" * 60 + "\n" + tb + "\n")
            except Exception:
                pass
            raise

    def poll(self):
        with self._lock:
            t = dict(self.task)
            busy, kind = t["busy"], t["kind"]
            if not busy:
                self.task = self._idle_task()
        # 扫描途中：把已经读出来的行顺带捎给前端（v2.7 流式显示）
        if busy and kind == "scan":
            part = self._live_payload()
            if part:
                t["partial"] = part
        return t

    # ---------------- 对外：选择来源 ----------------
    def choose_folder(self):
        import webview
        try:
            res = self._window.create_file_dialog(dlg_kind("FOLDER"))
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
        if not res:
            return {"ok": False, "msg": "已取消"}
        folder = res[0] if isinstance(res, (list, tuple)) else res
        self.folder = folder
        self.files = []
        self._skip_paths = set()        # 换文件夹 = 重新开始，旧的「已拆原件」记录作废
        self.cfg["lastFolder"] = folder
        save_config(self.cfg)
        self.rescan()
        return {"ok": True, "folder": folder}

    def _append_paths(self, picked):
        """
        把新选 / 新拖进来的文件**追加**到当前清单（按路径去重）。

        ⚠️ 2026-10-17 业主报：第二次「添加文件」把上一次的整批**覆盖**了。
           根因是这里原来写 `self.files = list(res)`。
           现在用「**当前列表里的文件**」打底再接新的 —— 而不是用上一次的
           self.files 打底：用户可能刚「移除」掉几行，那些不该因为再加一次就复活。

        返回实际新增的条数（全是重复时就是 0，界面据此提示「没有新增」）。
        """
        # ⚠️ 打底只用 self.items（**当前列表里真实存在的那些**），
        #    不能用 self.files —— files 是「上次扫过的全量」，
        #    用户用行末垃圾桶删掉的几行还在 files 里，拿它打底会让删掉的行复活。
        #    items 与 files 一一对应（解析失败的也会留一个 item），所以这样最准。
        base = [it.path for it in self.items]
        merged, seen = [], set()
        for p in base:                       # 已有的一份不少地留住
            ap = os.path.abspath(p)
            k = os.path.normcase(ap)
            if k in seen:
                continue
            seen.add(k)
            merged.append(ap)
        added = 0
        for p in picked:                     # 新选的接在后面，重复的跳过
            ap = os.path.abspath(p)
            k = os.path.normcase(ap)
            if k in seen:
                continue
            seen.add(k)
            merged.append(ap)
            added += 1
        self.files = merged
        return added

    def add_files(self):
        import webview
        try:
            res = self._window.create_file_dialog(
                dlg_kind("OPEN"), allow_multiple=True,
                file_types=("发票文件 (*.pdf;*.ofd;*.xml;*.zip)",
                            "PDF 文件 (*.pdf)", "OFD 文件 (*.ofd)",
                            "XML 文件 (*.xml)", "压缩包 (*.zip)",
                            "所有文件 (*.*)"))
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
        if not res:
            return {"ok": False, "msg": "已取消"}
        added = self._append_paths([p for p in res if p])
        self.folder = ""          # 已经是「手动添加」的清单，文件夹不再代表这一批
        self.rescan()
        return {"ok": True, "count": len(self.files), "added": added}

    # ---------------- 对外：拖放导入 ----------------
    ZIP_SUBDIR_SUFFIX = "_解压"

    def take_drop_paths(self):
        """
        **补救通道**：前端在 drop 之后调一次，捞走手里还剩下的拖入路径。

        定位说明（别把它当主通道）：
          主通道是 app.py 里那个 pywebview drop 回调 —— 它拿得到
          `pywebviewFullPath`（WebView2 给的真实路径），最准。
          但 pywebview 回配路径的那段逻辑（webview/util.py）有三个致命点：
            1. `file['name']` 一旦没被序列化出来就是 KeyError，
               异常会把整段掀翻，**连主回调都不会被调用**；
            2. 用文件名做严格相等匹配，中文名/重名都可能配不上；
            3. 配不上时只 `continue`，完全不报错。
          而它配上一个就从 `_dnd_state['paths']` 里删一个。
          所以本方法的作用是：**主通道失败时，把表里剩下的捞出来**。
          主通道成功时表已经被清空，这里就什么也没有 —— 此时安静返回，
          绝不能报错（否则用户会遇到「明明导进去了却弹报错」）。
        """
        try:
            from webview.dom import _dnd_state
        except Exception as exc:
            self._log_drop("error", ["读不到 _dnd_state：%s" % exc])
            return {"ok": False, "quiet": True,
                    "msg": "拖放组件不可用，请用「选择文件夹」"}

        raw = [(n, p) for (n, p) in _dnd_state.get("paths", []) if p]
        _dnd_state["paths"].clear()

        if not raw:
            # 表是空的：要么主通道已经处理完了（正常），要么宿主压根没记录到
            # （极少见）。两种情况前端都不该弹错误 —— 它会先看列表有没有内容。
            return {"ok": False, "quiet": True}

        paths = list(dict.fromkeys(p for (_n, p) in raw))
        self._log_drop("paths", ["补救通道取到 %d 条真实路径" % len(paths)])
        return self.accept_drop(paths)

    def accept_drop(self, paths):
        """
        接收拖进窗口的路径（文件 / 文件夹 / 压缩包）。

        由 app.py 里 pywebview 的 DOM drop 回调调用，**不是**普通 JS API。
        原因：浏览器出于安全不会把文件的**绝对路径**交给 JS，而我们恰恰要靠
        绝对路径去原地改名。WebView2 里真实路径只存在于宿主侧，
        pywebview 通过 DOM 事件回调把它塞进 `pywebviewFullPath` 交给 Python。
        所以这条链路只能由 Python 侧发起，前端只负责画拖拽高亮。

        规则：
          · 只拖了一个文件夹 → 等价于「选择文件夹」，并且**自动打开递归**：
            用户拖文件夹的意图就是「我要这个文件夹里的全部发票」，
            客户发来的总文件夹里常常夹着子文件夹和压缩包，不递归就拿不全
          · 其余情况 → 汇总成一个文件清单（等价于「添加文件」）
          · 压缩包 → 解到旁边的「xxx_解压」文件夹；**文件夹里藏的压缩包也一并解开**，
            包括「压缩包里的文件夹里还有压缩包」这种嵌套（最多 3 层）
          · 不认识的格式会被跳过并在界面上说明，不静默忽略
        """
        paths = [p for p in (paths or []) if p]
        # 同一批路径在 3 秒内重复到达 = 两条通道各触发了一次，只处理第一次。
        sig = "|".join(sorted(p.lower() for p in paths))
        now = time.time()
        if sig and sig == self._lastDrop[0] and now - self._lastDrop[1] < 3.0:
            self._log_drop("skip", ["同一批路径重复触发，忽略（防两条通道各导一次）"])
            return {"ok": True, "mode": "dedup", "count": len(self.items)}
        self._lastDrop = (sig, now)

        self._log_drop("drop", paths)
        folders, files, zips, skipped = [], [], [], []
        for raw in paths:
            if not raw:
                continue
            p = os.path.abspath(str(raw))
            if not os.path.exists(p):
                skipped.append("%s（路径不存在）" % os.path.basename(p))
                continue
            low = p.lower()
            if os.path.isdir(p):
                folders.append(p)
            elif low.endswith(IP.ARCHIVE_EXTS):
                zips.append(p)
            elif low.endswith(IP.INVOICE_EXTS):
                files.append(p)
            else:
                skipped.append("%s（不支持的格式）" % os.path.basename(p))

        # 压缩包：直接拖进来的 + 藏在拖进来的文件夹里的（含多层嵌套），统统解开
        exp = {"made": [], "reused": 0, "failed": [], "seen": set()}
        for z in zips:
            dest, err, fresh = self._extract_zip(z)
            exp["seen"].add(z)
            if err:
                exp["failed"].append((os.path.basename(z), err))
                continue
            if fresh:
                exp["made"].append(dest)
            else:
                exp["reused"] += 1
            folders.append(dest)
        for d in list(folders):
            sub = self._expand_archives(d, exclude=exp["seen"])
            exp["made"] += sub["made"]
            exp["reused"] += sub["reused"]
            exp["failed"] += sub["failed"]
            exp["seen"] |= sub["seen"]
        for name, err in exp["failed"]:
            skipped.append("%s（%s）" % (name, err))

        self._log_drop("classified", ["目录 %d / 发票文件 %d / 压缩包 %d / 解压 %d / 跳过 %d"
                                      % (len(folders), len(files), len(zips),
                                         len(exp["made"]), len(skipped))])
        # 只拖了一个文件夹、且没有单独的文件 —— 当成「选择文件夹」
        if len(folders) == 1 and not files:
            turned_on = False
            if not self.cfg["recursive"]:
                # 自动打开递归并把状态写回配置：界面上的开关必须和实际行为一致，
                # 否则用户会看到「开关明明关着，子文件夹却被扫了」，一头雾水。
                self.cfg["recursive"] = True
                save_config(self.cfg)
                turned_on = True
            self.folder = folders[0]
            self.files = []
            self._set_import_info(exp, skipped)
            self.rescan()
            return {"ok": True, "mode": "folder", "sub": turned_on,
                    "extracted": exp["made"], "skipped": skipped}

        picked = list(files)
        for d in folders:
            # 拖进来的文件夹一律递归：用户要的是「里面的全部发票」
            picked.extend(IP._walk_invoice_files(d, True))
        picked = list(dict.fromkeys(picked))        # 去重且保持顺序

        if not picked:
            self._log_drop("result", ["清单为空（扫过 %d 个目录、%d 个文件）"
                                      % (len(folders), len(files))])
            msg = "没有找到可识别的发票文件（支持 PDF / OFD / XML）"
            self._set_import_info(exp, skipped, msg)
            return {"ok": False, "msg": msg, "skipped": skipped}

        # 拖进来的文件也是**追加**，跟「添加文件」保持同一套语义。
        # （只有「拖一个文件夹」才是替换 —— 那是「选择文件夹」的等价物）
        added = self._append_paths(picked)
        self.folder = ""
        self._log_drop("result", ["本批 %d 个（新增 %d）：%s"
                                  % (len(self.files), added,
                                     os.path.basename(picked[0]))])
        self._set_import_info(exp, skipped)
        self.rescan()
        return {"ok": True, "mode": "files", "count": len(self.files),
                "added": added,
                "extracted": exp["made"], "skipped": skipped}

    def _log_drop(self, stage, detail):
        """记录一次拖放的中间结果（最多留最近 12 条，供界面/排查查看）"""
        try:
            self._dropLog.append({"t": datetime.now().strftime("%H:%M:%S"),
                                  "stage": stage, "detail": detail})
            del self._dropLog[:-12]
        except Exception:
            pass

    def _set_import_info(self, exp=None, skipped=None, msg=""):
        """
        记录本次拖放的结果，供前端提示。

        `exp` 是 _extract_zip/_expand_archives 的汇总：
        {"made": [...新解压的目录], "reused": n, ...}
        —— 「新解压」和「复用已有」要分开报，否则用户重复拖同一个文件夹时
        会看到「已解压 3 个压缩包」这种假消息。
        """
        exp = exp if isinstance(exp, dict) else {}
        self._importSeq += 1
        self.importInfo = {"seq": self._importSeq,
                           "extracted": [os.path.basename(p) for p in
                                         (exp.get("made") or [])],
                           "reused": int(exp.get("reused") or 0),
                           "skipped": list(skipped or []),
                           "msg": msg}

    @classmethod
    def _extract_zip(cls, zpath):
        """
        把压缩包解到它旁边的「xxx_解压」文件夹。
        返回 (目标文件夹, 错误信息, 是否新建)。

        「是否新建」用来区分「这次真的解开了」和「目录本来就在、直接复用」——
        统计时不能把复用的也算成刚解压的，否则用户重复拖同一个文件夹，
        会看到「已解压 3 个压缩包」这种假消息。

        安全约定（不能破）：
          · 目标文件夹**已存在就复用**：不删、不清空、不覆盖同名文件 ——
            绝不碰用户已经有的东西
          · 过滤目录穿越（zip slip：压缩包里塞 `../../x` 这种成员名）
          · 跳过 __MACOSX 与隐藏文件
          · 原位解压失败（只读盘、网络盘）就退回系统临时目录，并把
            真实位置回传前端告知用户
        """
        stem = os.path.splitext(os.path.basename(zpath))[0]
        dest = os.path.join(os.path.dirname(zpath), stem + cls.ZIP_SUBDIR_SUFFIX)
        fresh = not os.path.isdir(dest)          # 先判断，再创建
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError:
            dest = os.path.join(tempfile.gettempdir(), APP_NAME + "_导入", stem)
            fresh = not os.path.isdir(dest)
            try:
                os.makedirs(dest, exist_ok=True)
            except OSError as exc:
                return "", "无法创建解压目录：%s" % exc, False

        try:
            root = os.path.abspath(dest)
            with zipfile.ZipFile(zpath) as zf:
                for m in zf.infolist():
                    if m.is_dir():
                        continue
                    parts = [x for x in m.filename.replace("\\", "/").split("/")
                             if x not in ("", ".")]
                    if not parts or ".." in parts:
                        continue
                    if parts[0] == "__MACOSX" or parts[-1].startswith("."):
                        continue
                    dst = os.path.abspath(os.path.join(root, *parts))
                    if dst != root and not dst.startswith(root + os.sep):
                        continue
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if os.path.exists(dst):
                        continue                    # 不覆盖
                    with zf.open(m) as src, open(dst, "wb") as out:
                        shutil.copyfileobj(src, out)
        except Exception as exc:
            return "", "无法解压：%s" % exc, False
        return dest, "", fresh

    # 压缩包嵌套解压的层数上限：防「压缩包里套压缩包」被无限展开
    ARCHIVE_MAX_DEPTH = 3

    def _expand_archives(self, folder, exclude=()):
        """
        把 `folder` 里**任意层级**的压缩包都就地解开，包括
        「压缩包里的文件夹里还有压缩包」这种嵌套。

        为什么需要它：用户拖进来的往往是客户给的**一个总文件夹**，里面
        夹着子文件夹和压缩包，发票散落各处。我们的目标就是「拖一个总文件夹，
        拿到里面全部 PDF」，所以得替他把压缩包都揭开。

        规则与单拖一个压缩包时**完全一致**（复用 _extract_zip）：
        解到旁边「xxx_解压」、已存在就复用、绝不覆盖、过滤 zip slip。

        返回 {"made": [新解压的目录], "reused": 复用个数,
              "failed": [(压缩包名, 原因)], "seen": {处理过的压缩包路径}}
        """
        made, failed = [], []
        reused = 0
        seen = set(exclude or ())
        queue = [(folder, 0)]
        while queue:
            cur, depth = queue.pop(0)
            if depth > self.ARCHIVE_MAX_DEPTH or not os.path.isdir(cur):
                continue
            try:
                walker = list(os.walk(cur))
            except OSError:
                continue
            for root, _dirs, names in walker:
                for n in names:
                    if not n.lower().endswith(IP.ARCHIVE_EXTS):
                        continue
                    z = os.path.join(root, n)
                    if z in seen:
                        continue
                    seen.add(z)
                    dest, err, fresh = self._extract_zip(z)
                    if err:
                        failed.append((n, err))
                        continue
                    if fresh:
                        made.append(dest)
                    else:
                        reused += 1
                    # 解压出来的目录里可能还有压缩包 —— 继续挖一层
                    queue.append((dest, depth + 1))
        return {"made": made, "reused": reused, "failed": failed, "seen": seen}

    def _scan_items(self, on_item=None):
        """
        按当前来源扫一遍，返回 [InvoiceInfo]（**不**改 self.items）。

        rescan 和「拆分多票 PDF」共用这一段：拆完也要重扫，逻辑只有一份，
        免得两边哪天走岔了。`_skip_paths` 里的文件会被跳过 —— 拆开的原始整份
        还在磁盘上，但不该再回列表里冒充一张票。
        """
        skip = self._skip_paths
        if self.files:
            paths = [p for p in self.files if os.path.abspath(p) not in skip]
            out, total = [], len(paths)
            for i, p in enumerate(paths, 1):
                item = IP.parse_invoice(p)
                out.append(item)
                if on_item:
                    on_item(i - 1, item, total)
                self._progress(i, total, os.path.basename(p))
            return out
        out = IP.parse_folder(self.folder, self.cfg["recursive"],
                              progress=self._progress, on_item=on_item)
        if skip:
            out = [it for it in out if os.path.abspath(it.path) not in skip]
        return out

    # ---------------- 流式扫描（v2.7） ----------------
    def _live_on_item(self, i, item, total):
        """
        每读完一张就登记一下，让 poll() 能把「已经读出来的行」先发给界面。

        ⚠️ 只在解析线程里跑。这里只做 list 赋值，重活（拼行数据）留给 poll，
           否则会把解析线程拖慢 —— 那等于用变慢换取"看起来快"。
        """
        with self._lock:
            while len(self._live_items) <= i:
                self._live_items.append(None)
            self._live_items[i] = item
            self._live_total = total
            self._live_count += 1
            # 每攒够 8 张（或最后一张）才让前端重画一次：
            # 1000 行重绘 25ms，1 张重画一次会把界面拖垮
            if self._live_count >= 8 or self._live_count >= total:
                self._live_dirty = True
                self._live_count = 0

    def _live_payload(self):
        """把已读出的行拼成前端可直接渲染的 payload（没攒够就返回 None）"""
        with self._lock:
            if not self._live_dirty:
                return None
            self._live_dirty = False
            items = list(self._live_items)
            total = self._live_total
        rows = []
        for i, it in enumerate(items):
            if it is None:
                continue
            try:
                rows.append(self._row_from(i, it))
            except Exception:
                continue          # 单行拼不出来不能让整个流式显示停掉
        return {"rows": rows, "total": total}

    def rescan(self):
        if not self.folder and not self.files:
            return {"ok": False, "msg": "请先选择文件夹"}
        if self.folder and not os.path.isdir(self.folder):
            return {"ok": False, "msg": "文件夹不存在了，请重新选择：%s" % self.folder}

        def work():
            with self._lock:
                self._live_items = []
                self._live_total = 0
                self._live_count = 0
                self._live_dirty = False
            out = self._scan_items(on_item=self._live_on_item)
            self.items = out
            self.checked = [it.ok for it in out]
            self.edited = {}
            self._recompute_dups()
            self._compute_analysis()      # 配对 + 离线校验（结果缓存）
            CACHE.save()                  # 解析缓存落盘（下次开软件直接命中）
            return self._snapshot()

        return self._start("scan", work)

    def remove_rows(self, idxs):
        """
        把指定的行**从列表里移除**。

        ⚠️ 刻意只移除、**不删磁盘文件**：
           删文件不可逆，而「这几张我不想处理」的诉求用移除就满足了；
           移错了重新扫描一下就回来。要真删文件得另做一个带二次确认的入口，
           不能藏在这个按钮里。
        """
        # 逐个转换、坏值丢掉：不能让一个脏值把整批移除都搞失败
        # （和 set_all 的处理保持一致）
        clean = set()
        for v in (idxs or []):
            try:
                clean.add(int(v))
            except (TypeError, ValueError, OverflowError):
                continue
        if not clean:
            # 措辞不能写「没有勾选」—— 现在行末的垃圾桶是逐行删，跟勾选无关
            return {"ok": False, "msg": "没有要移除的行"}
        want = sorted(clean)

        drop = {i for i in want if 0 <= i < len(self.items)}
        if not drop:
            return {"ok": False, "msg": "没有可移除的行"}

        # 连带移除挂在这张发票下面的行程单 —— 否则它会变成一份
        # 没有发票可配的「孤儿」行程单，徒增困惑
        for i in list(drop):
            for a in self.pairs.get("attach", {}).get(i, []):
                drop.add(a["trip"])

        self.items = [it for i, it in enumerate(self.items) if i not in drop]
        self.checked = [c for i, c in enumerate(self.checked) if i not in drop]

        # edited 的键是行号，移除后要重排，否则改过的名字会串到别的行上
        before = sorted(drop)
        self.edited = {
            old - sum(1 for d in before if d < old): name
            for old, name in self.edited.items() if old not in drop
        }

        self._recompute_dups()
        self._compute_analysis()
        return {"ok": True, "removed": len(drop), "snapshot": self._snapshot()}

    def clear(self):
        """
        清空列表。
        同时把「上次打开的文件夹」也忘掉 —— 否则下次启动又会自动把旧数据扫回来，
        用户点「清空列表」就是想要一个干净的开始。
        """
        self.items, self.checked, self.edited, self.dups = [], [], {}, {}
        self.folder, self.files = "", []
        self.cfg["lastFolder"] = ""
        save_config(self.cfg)
        return self._snapshot()

    # ---------------- 对外：规则 ----------------
    def set_rule(self, key, value):
        if key in DEFAULTS:
            self.cfg[key] = value
            normalize_config(self.cfg)      # fields/separator 变了要同步 template
            save_config(self.cfg)
            # 关掉/打开「重复报销预警」要立刻重算一次，否则界面上那批标记
            # 要等下次扫描才变 —— 用户会以为开关没生效。
            if key == "crossCheck":
                try:
                    self._compute_cross_batch()
                except Exception:
                    pass
        return self._snapshot()

    def set_rules(self, patch):
        for k, v in (patch or {}).items():
            if k in DEFAULTS:
                self.cfg[k] = v
        normalize_config(self.cfg)
        save_config(self.cfg)
        return self._snapshot()

    def set_fields(self, fields, separator=None, suffix=None):
        """
        设置勾选式命名规则。

        fields 是「按用户排定顺序」的字段 key 数组；顺序即文件名中出现的先后。
        这里做防御性过滤：非法 key 直接丢掉，避免前端出 bug 时把整个配置写坏。
        """
        if not isinstance(fields, (list, tuple)):
            return {"ok": False, "msg": "字段参数格式不对"}
        clean = [f for f in fields if f in FIELD_KEYS]
        if not clean:
            return {"ok": False, "msg": "至少勾选一个字段"}
        self.cfg["fields"] = clean
        if separator is not None:
            self.cfg["separator"] = separator
        if suffix is not None:
            self.cfg["suffix"] = str(suffix)
        normalize_config(self.cfg)
        save_config(self.cfg)
        return self._snapshot()

    def rule_preview(self, fields=None, separator=None, idx=None, suffix=None):
        """
        生成一条预览文件名，供勾选面板实时显示。

        ⚠️ 优先用**当前选中那张真实发票**的值。
           老版本只用内置示例值，结果是这样：用户明明读不到税号和大写金额，
           预览里却显示得漂漂亮亮 —— 等于把「字段没生效」这个问题藏起来了，
           用户只能靠事后改名才发现。（2026-10 用户就反馈过这个）
        现在还会把「这张票读不到的字段」列出来，让面板直接说清是哪几个字段。

        列表为空（没导入发票）时才退回内置示例值，保证空列表也能看到规则长什么样。

        ⚠️ 参数顺序：idx 排在 suffix **前面**。
           前端是 `call("rule_preview", fields, sep, sel)` 这样按位置传的，
           之前把 idx 放在 suffix 后面，结果 sel 落到了 suffix 上：
           面板永远显示第 1 张票，文件名尾巴还多出一个数字（真机就是这么挂的）。
           把 idx 提前，让「自然而然的三参数调用」就是对的。
        """
        # 防卫：万一还是有调用方把行号塞到了 suffix（比如老版本前端），
        # 不要悄悄拿它去拼文件名。
        if suffix is not None and not isinstance(suffix, str):
            suffix = None
        if idx is not None and not isinstance(idx, int):
            idx = None

        flds = [f for f in (fields or self.cfg.get("fields") or []) if f in FIELD_KEYS]
        sep = self.cfg.get("separator") if separator is None else separator
        sfx = self.cfg.get("suffix") if suffix is None else suffix
        flds = flds or ["buyer", "amount"]

        target = None
        if isinstance(idx, int) and 0 <= idx < len(self.items):
            target = self.items[idx]
        elif self.items:
            target = self.items[0]

        if target is not None:
            # 走和正式改名**完全相同**的 build_name，保证预览所见即所得
            name = IP.build_name(target, amount_mode=self.cfg["amountMode"],
                                 currency_prefix=self.cfg["currencyPrefix"],
                                 fields=flds, sep=sep, suffix=sfx)
            vals = IP.field_values(target, self.cfg["amountMode"],
                                   self.cfg["currencyPrefix"])
            missing = [FIELD_LABEL_OF.get(k, k) for k in flds
                       if not (vals.get(k) or "").strip()]
            return {"ok": True, "preview": name, "source": "real",
                    "file": target.file_name, "missing": missing,
                    "template": template_from_fields(flds, sep)}

        ex = {f["key"]: f["ex"] for f in FIELD_DEFS}
        parts = [ex.get(k, "") for k in flds]
        name = str(sep).join(p for p in parts if p)
        if sfx:
            name += str(sfx)
        return {"ok": True, "preview": IP.sanitize(name) if name else "",
                "source": "demo", "missing": [],
                "template": template_from_fields(flds, sep)}

    # ---------------- 对外：行操作 ----------------
    def toggle_row(self, idx, checked=None):
        if 0 <= idx < len(self.checked):
            self.checked[idx] = (not self.checked[idx]) if checked is None \
                else bool(checked)
        return self._snapshot()

    def set_all(self, checked, only_ok=False, idxs=None):
        """
        批量勾选 / 取消。

        `idxs` 可选：只作用于这些行号。列表被搜索或筛过时，「全选」应该
        只选中**看得见的那几条** —— 否则用户搜出 3 条点全选，却把隐藏的
        200 条也一起勾上，接着一改名就是一堆误操作。

        ⚠️ idxs 加在参数表**末尾**：老的两参数调用 set_all(true) /
           set_all(true, True) 语义完全不变。
        """
        if idxs is None:
            targets = range(len(self.items))
        else:
            targets = [i for i in idxs
                       if isinstance(i, int) and 0 <= i < len(self.items)]
        for i in targets:
            it = self.items[i]
            self.checked[i] = bool(checked) and (it.ok if only_ok else True)
        return self._snapshot()

    def set_new_name(self, idx, name):
        if not (0 <= idx < len(self.items)):
            return {"ok": False, "msg": "行号越界"}
        name = (name or "").strip()
        # 剥掉任意受支持的扩展名（用户可能把 .ofd / .xml 一起粘进来）
        _low = name.lower()
        for _e in IP.INVOICE_EXTS:
            if _low.endswith(_e):
                name = name[:-len(_e)]
                break
        name = RC.safe_name(name, "")
        if not name:
            self.edited.pop(idx, None)      # 清空 = 恢复自动命名
        else:
            self.edited[idx] = name
        return self._snapshot()

    def fill_manual(self, idx, buyer, amount):
        """未识别项人工补录：写入购买方与金额，自动生成新名字"""
        if not (0 <= idx < len(self.items)):
            return {"ok": False, "msg": "行号越界"}
        it = self.items[idx]
        buyer = (buyer or "").strip()
        if buyer:
            it.buyer = buyer
            it.ok = True
        amt_txt = str(amount or "").strip().replace(",", "").replace("¥", "")
        if amt_txt:
            try:
                it.amount = float(amt_txt)
                it.ok = True
            except ValueError:
                return {"ok": False, "msg": "金额格式不对，请输入数字"}
        if it.message.startswith("未识别") or it.message.startswith("未读取"):
            it.message = ""
        self.edited.pop(idx, None)
        self.checked[idx] = it.ok
        self._recompute_dups()
        return self._snapshot()

    def open_pdf(self, idx):
        if 0 <= idx < len(self.items):
            try:
                os.startfile(self.items[idx].path)
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "msg": str(exc)}
        return {"ok": False, "msg": "行号越界"}

    def reveal(self, idx):
        """在资源管理器中定位文件"""
        if not (0 <= idx < len(self.items)):
            return {"ok": False, "msg": "行号越界"}
        try:
            subprocess.Popen(["explorer", "/select,",
                              os.path.normpath(self.items[idx].path)])
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def open_folder(self, which="src"):
        target = self.folder
        if which == "backup":
            target = os.path.join(self.folder or "", RC.BACKUP_SUBDIR)
        if not target or not os.path.isdir(target):
            return {"ok": False, "msg": "文件夹不存在"}
        try:
            os.startfile(target)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    # ---------------- 对外：预览计划 ----------------
    def _build_entries(self):
        entries, idxs = [], []
        for i in range(len(self.items)):
            if not self.checked[i]:
                continue
            it = self.items[i]
            entries.append(RC.Entry(it.path, self._new_name(i),
                                    it.invoice_no, it.buyer, it.amount, it.ok))
            idxs.append(i)
        return idxs, entries

    def plan_preview(self):
        idxs, entries = self._build_entries()
        plan = RC.build_plan(entries,
                             copy_mode=(self.cfg["mode"] == "copy"),
                             dedup=self.cfg["dedup"],
                             distribute=self.cfg["distribute"])
        rows, conflicts, warns = [], 0, 0
        for p in plan:
            rec = {"i": idxs[p.index], "src": p.src_name, "dst": p.dst_name,
                   "action": p.action, "warn": p.warn}
            if p.action == "skip":
                conflicts += 1
            if p.warn:
                warns += 1
            if len(rows) < 8:
                rows.append(rec)
        real = [p for p in plan if p.action in ("rename", "copy", "move")]
        return {
            "total": len(plan),
            "real": len(real),
            "moves": sum(1 for p in plan if p.action == "move"),
            "copies": sum(1 for p in plan if p.action == "copy"),
            "renames": sum(1 for p in plan if p.action == "rename"),
            "same": sum(1 for p in plan if p.action == "same"),
            "skipped": conflicts,
            "warns": warns,
            "sample": rows,
            "distribute": self.cfg["distribute"],
            "backup": self.cfg["backup"],
            "copyMode": self.cfg["mode"] == "copy",
        }

    # ---------------- 对外：执行 ----------------
    def execute(self):
        idxs, entries = self._build_entries()
        if not entries:
            return {"ok": False, "msg": "没有勾选任何发票"}

        plan = RC.build_plan(entries,
                             copy_mode=(self.cfg["mode"] == "copy"),
                             dedup=self.cfg["dedup"],
                             distribute=self.cfg["distribute"])

        backup_root = self.folder or RC.common_base_dir([e.src for e in entries])

        def work():
            runner = RC.Runner(plan,
                               do_backup=self.cfg["backup"],
                               backup_root=backup_root,
                               on_progress=lambda d, t, nm:
                               self._progress(d, t, nm))
            runner.run()
            base = RC.common_base_dir([p.src for p in plan if p.action in
                                       ("rename", "copy")])
            RC.append_log(base, runner.log)
            # v2.7：把这一批「真正处理过的票」记进跨批次台账，
            # 下次在别的文件夹再遇到同一张票就能提醒「几月已报过」。
            # 只用成功的记录 —— 失败的没改名，不该算"报过"。
            try:
                self._remember_processed(runner.log, base)
            except Exception:
                pass
            return {
                "done": runner.done,
                "failed": runner.failed,
                "skipped": runner.skipped,
                "backup": runner.backup_dir,
                "backupFailed": len(runner.backup_failed),
                "errors": [{"file": os.path.basename(r["from"]),
                            "error": r.get("error", "")}
                           for r in runner.log if r["mode"] == "error"][:20],
            }

        return self._start("execute", work)

    def _remember_processed(self, log, folder):
        """
        把「这次真的处理掉了哪些票」记进跨批次台账。

        只认改名/归档/复制成功的那几条 —— 失败的没落盘，不该算"报过"。
        行程单没有发票号码，不记（台账是按票号索引的）。
        """
        by_path = {}
        for it in self.items:
            if it.kind == "invoice" and it.invoice_no:
                by_path[os.path.normcase(os.path.abspath(it.path))] = it
        rows, seen = [], set()
        for r in log or []:
            if r.get("mode") not in ("rename", "move", "copy"):
                continue
            src = r.get("from") or ""
            if not src:
                continue
            it = by_path.get(os.path.normcase(os.path.abspath(src)))
            if not it or it.invoice_no in seen:
                continue
            seen.add(it.invoice_no)
            rows.append({"no": it.invoice_no, "amount": it.amount,
                         "date": it.date, "folder": folder,
                         "file": os.path.basename(r.get("to") or it.file_name)})
        if rows:
            DH.INDEX.record(rows)

    def undo(self):
        base = self.folder or (os.path.dirname(self.items[0].path)
                               if self.items else "")
        if not base:
            return {"ok": False, "msg": "找不到记录文件所在的文件夹"}
        # ⚠️ v2.7：只撤**最近一批**。
        #    老版本这里调的是 undo_folder()，会把历史里所有能撤的记录一次全撤 ——
        #    连做三次改名后点「撤销上次」，三次全退回去了，按钮名和实际行为对不上。
        #    想撤更早的批次，去「操作历史」里按批次撤（那里本来就是按批的）。
        groups = RC.history(base)
        batch = ""
        for g in groups:
            if g.get("undoable"):
                batch = g.get("batch") or ""
                break
        if not batch:
            return {"ok": False, "msg": "没有可撤销的操作"}
        ops = [r for r in RC.read_log(base)
               if (r.get("batch") or "") == batch and RC._undoable(r)]
        if not ops:
            return {"ok": False, "msg": "这一批已经撤过了"}

        def work():
            _ops, done = RC.undo_batch(base, batch)
            return {"restored": done, "total": len(ops),
                    "batch": batch}

        return self._start("undo", work)

    def undo_count(self):
        """「撤销上次」当前能撤几条 —— 只算**最近一批**（v2.7 语义修正）"""
        base = self.folder or (os.path.dirname(self.items[0].path)
                               if self.items else "")
        if not base:
            return 0
        for g in RC.history(base):
            if g.get("undoable"):
                return g["undoable"]
        return 0

    # ================================================================
    # v2.6 新增能力
    # ================================================================

    # ---------------- 票面预览 ----------------
    def preview(self, idx, page=0, target=760):
        """
        右栏票面预览：返回 PNG（data URL）+ 关键字段在页面上的相对坐标。

        坐标归一化到 0~1：前端用百分比定位高亮框，
        窗口拉宽拉窄、图片缩放都不用重新算。

        `target`：渲染目标宽度。列表里的小预览用默认 760；
        点开放大看的时候传 1500，放大两倍字都还是清楚的。
        """
        try:
            i = int(idx)
            page = int(page or 0)
            target = int(target or 760)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "参数不对"}
        if not (0 <= i < len(self.items)):
            return {"ok": False, "msg": "行号超出范围"}
        it = self.items[i]
        if it.need_password:
            return {"ok": False, "msg": "这张票是加密的，先输入密码", "needPassword": True}

        r = PV.render(it.path, page, target_width=max(360, min(2400, target)))
        if r.get("ok"):
            r["rects"] = PV.field_rects(it.path, r.get("page", 0), it)
            r["file"] = it.file_name
            r["fmt"] = it.fmt
        return r

    # ---------------- 重复票取舍台 ----------------
    def dup_overview(self):
        """所有重复组的明细（格式 / 大小 / 时间 / 金额），供「取舍台」并排展示"""
        groups = []
        for gid, no in enumerate(sorted(self.dups), 1):
            idxs = self.dups[no]
            items = []
            for i in idxs:
                it = self.items[i]
                try:
                    st = os.stat(it.path)
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    size, mtime = 0, 0
                items.append({
                    "i": i,
                    "file": it.file_name,
                    "fmt": it.fmt,
                    "size": size,
                    "sizeText": size_text(size),
                    "mtime": mtime,
                    "mtimeText": mtime_text(mtime),
                    "amountText": money(it.amount),
                    "buyer": it.buyer or "",
                    "checked": bool(self.checked[i]) if i < len(self.checked) else False,
                })
            # 推荐保留哪一份：PDF 优先（打印/归档最通用），同格式取体积大的
            best_i, best_score = None, None
            for x in items:
                score = ((2 if x["fmt"] == "PDF" else 1 if x["fmt"] == "OFD" else 0),
                         x["size"])
                if best_score is None or score > best_score:
                    best_score, best_i = score, x["i"]
            groups.append({"gid": gid, "no": no, "total": len(idxs),
                           "items": items, "suggest": best_i})
        return {"ok": True, "groups": groups}

    def keep_one(self, gid, keep):
        """保留指定的一份，同组其余从列表移除（**不动磁盘文件**）"""
        try:
            gid = int(gid)
            keep = int(keep)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "参数不对"}
        target_no = None
        for g, no in enumerate(sorted(self.dups), 1):
            if g == gid:
                target_no = no
                break
        if target_no is None:
            return {"ok": False, "msg": "没有这一组"}
        idxs = self.dups.get(target_no) or []
        if keep not in idxs:
            return {"ok": False, "msg": "要保留的那份不在这组里"}
        drop = [i for i in idxs if i != keep]
        if not drop:
            return {"ok": False, "msg": "这组只有一份，不用处理"}
        return self.remove_rows(drop)

    # ---------------- 多票 PDF 拆分 ----------------
    def split_multi(self, idx):
        """
        把一个「里面装了好几张票」的 PDF 按页拆开。

        拆完把**原文件**从列表里去掉（磁盘上一个字节都不动），
        再把拆出来的新文件读进来 —— 走同一个 _scan_items，逻辑只有一套。
        """
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "行号无效"}
        if not (0 <= i < len(self.items)):
            return {"ok": False, "msg": "行号超出范围"}
        it = self.items[i]
        multi = it.multi or {}
        groups = multi.get("groups") or []
        if len(groups) < 2:
            return {"ok": False, "msg": "这个文件里只有一张票，不用拆"}

        src = it.path

        def work():
            made, err = IP.split_pdf_by_groups(src, groups)
            if not made:
                return {"ok": False, "msg": err or "没有拆出任何页"}
            self._skip_paths.add(os.path.abspath(src))
            if self.files:
                for m in made:
                    if m not in self.files:
                        self.files.append(m)
            out = self._scan_items()
            self.items = out
            self.checked = [x.ok for x in out]
            self.edited = {}
            self._recompute_dups()
            self._compute_analysis()
            CACHE.save()
            return {"ok": True, "made": made, "count": len(made),
                    "snapshot": self._snapshot()}

        return self._start("split", work)

    # ---------------- 操作历史 / 按批撤销 ----------------
    def history(self):
        """改名/归档的批次列表（新的在前），供「操作历史」弹窗"""
        base = self.folder or (os.path.dirname(self.items[0].path)
                               if self.items else "")
        if not base:
            return {"ok": True, "groups": [], "base": ""}
        return {"ok": True, "groups": RC.history(base), "base": base}

    def undo_batch(self, batch):
        """只回滚某一批。老版本只能「全部撤销」，误撤过一次就全回去了"""
        base = self.folder or (os.path.dirname(self.items[0].path)
                               if self.items else "")
        if not base:
            return {"ok": False, "msg": "找不到记录文件所在的文件夹"}
        if not batch:
            return {"ok": False, "msg": "缺少批次号"}
        recs = [r for r in RC.read_log(base) if (r.get("batch") or "") == batch]
        if not recs:
            return {"ok": False, "msg": "没有这一批的记录"}

        def work():
            _ops, done = RC.undo_batch(base, batch)
            return {"restored": done, "total": len(recs), "batch": batch}

        return self._start("undo", work)

    # ---------------- 加密 PDF ----------------
    def set_pdf_password(self, idx, pwd):
        """给加密 PDF 补上密码，立刻重解析这一张"""
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "行号无效"}
        if not (0 <= i < len(self.items)):
            return {"ok": False, "msg": "行号超出范围"}
        pwd = (pwd or "").strip()
        if not pwd:
            return {"ok": False, "msg": "密码不能为空"}
        it = self.items[i]
        IP.PDF_PASSWORDS[os.path.abspath(it.path)] = pwd
        # ⚠️ 必须清掉这一条的解析缓存：上次缓存的是「需要密码」的结果，
        #    不清的话设完密码还是拿回原来那条失败记录，用户会以为密码错了。
        CACHE.drop(it.path)
        info = IP.parse_invoice(it.path)
        if info.need_password:
            return {"ok": False, "msg": "密码不对，再试一次"}
        self.items[i] = info
        self.checked[i] = info.ok
        self._recompute_dups()
        self._compute_analysis()
        return {"ok": True, "snapshot": self._snapshot()}

    # ---------------- 命名模板库 ----------------
    def save_template(self, name):
        """把当前的命名规则存成一套模板（最多留 12 套）"""
        name = (name or "").strip()[:20] or "未命名"
        tpls = list(self.cfg.get("templates") or [])
        tid = "T%d" % int(time.time() * 1000 % 10 ** 9)
        tpls.append({
            "id": tid, "name": name,
            "fields": list(self.cfg.get("fields") or []),
            "separator": self.cfg.get("separator", "—"),
            "amountMode": self.cfg.get("amountMode", "smart"),
            "currencyPrefix": self.cfg.get("currencyPrefix", ""),
        })
        self.cfg["templates"] = tpls[-12:]
        save_config(self.cfg)
        return {"ok": True, "templates": self.cfg["templates"], "id": tid}

    def apply_template(self, tid):
        """套用一套模板"""
        for t in (self.cfg.get("templates") or []):
            if t.get("id") == tid:
                self.cfg["fields"] = list(t.get("fields") or ["buyer", "amount"])
                self.cfg["separator"] = t.get("separator", "—")
                self.cfg["amountMode"] = t.get("amountMode", "smart")
                self.cfg["currencyPrefix"] = t.get("currencyPrefix", "")
                self.cfg["template"] = template_from_fields(
                    self.cfg["fields"], self.cfg["separator"])
                save_config(self.cfg)
                return {"ok": True, "snapshot": self._snapshot()}
        return {"ok": False, "msg": "找不到这套模板"}

    def delete_template(self, tid):
        tpls = [t for t in (self.cfg.get("templates") or []) if t.get("id") != tid]
        self.cfg["templates"] = tpls
        save_config(self.cfg)
        return {"ok": True, "templates": tpls}

    # ---------------- 改名对照表 ----------------
    def export_mapping(self):
        """
        导出「原文件名 → 新文件名」对照表（CSV）。

        台账记的是**结果**，对照表记的是**过程**：
        审计或客户问「这张票你动过吗、原来叫什么」，查这张表就行。
        """
        if not self.items:
            return {"ok": False, "msg": "列表为空"}
        import webview
        default = "改名对照表_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M")
        try:
            res = self._window.create_file_dialog(
                dlg_kind("SAVE"), save_filename=default,
                file_types=("CSV 表格 (*.csv)", "所有文件 (*.*)"))
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
        if not res:
            return {"ok": False, "msg": "已取消"}
        path = res if isinstance(res, str) else res[0]
        if not str(path).lower().endswith(".csv"):
            path = str(path) + ".csv"
        try:
            from export_core import write_mapping
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            rows = []
            for i in range(len(self.items)):
                r = self._row(i)
                r["time"] = now
                rows.append(r)
            return write_mapping(path, rows, {"folder": self.folder})
        except Exception as exc:
            return {"ok": False, "msg": "导出失败：%s" % exc}

    # ---------------- 解析缓存 ----------------
    def cache_info(self):
        s = CACHE.stats()
        return {"ok": True, "count": s["count"], "file": s["file"],
                "version": s["version"]}

    def clear_cache(self):
        CACHE.clear()
        return {"ok": True, "msg": "解析缓存已清空，下次扫描会重新读一遍"}

    # ---------------- 自定义标题栏（v2.8.0） ----------------
    # 前端顶栏那三个按钮调这里。为什么不用浏览器自带的窗口控制：
    # 系统标题栏已经被去掉（见 app.py 的 CUSTOM_FRAME）。
    def _hwnd(self):
        try:
            return int(self._window.native.Handle.ToInt64())
        except Exception:
            return 0

    def _is_max(self):
        """
        是否已最大化。

        ⚠️ 不能读 WinForms 的 WindowState：自定义标题栏是"外部改窗口样式"实现的，
           .NET 记的 RestoreBounds 和我们看到的边框对不上。直接用 Win32 的 IsZoomed，
           它看的是窗口真实状态，跟样式怎么改无关。
        """
        hwnd = self._hwnd()
        if not hwnd:
            return False
        try:
            return bool(ctypes.windll.user32.IsZoomed(hwnd))
        except Exception:
            return False

    def win_meta(self):
        """告诉前端：当前是不是"自定义标题栏"模式（决定要不要显示那三个按钮）"""
        return {"ok": True, "custom": bool(getattr(self, "_custom_frame", False))}

    def win_is_max(self):
        return {"ok": True, "max": self._is_max()}

    def win_debug(self):
        """诊断用：返回窗口样式位与尺寸，确认"有缩放边、没标题栏"（自动化测试也会用）"""
        hwnd = self._hwnd()
        if not hwnd:
            return {"ok": False, "msg": "没有窗口句柄"}
        try:
            from ctypes import wintypes
            u = ctypes.windll.user32
            style = u.GetWindowLongW(hwnd, -16)
            rect = wintypes.RECT()
            crect = wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(rect))
            u.GetClientRect(hwnd, ctypes.byref(crect))
            # 命中测试：问 Windows「右下角这 3 个像素算不算缩放边」。
            # 返回 HTBOTTOMRIGHT(17) 就说明**拖边缩放真的可用** ——
            # 光看 WS_THICKFRAME 位不够，WinForms 有可能把 NCHITTEST 拦成 HTCLIENT。
            hit_br = None
            if not u.IsZoomed(hwnd):
                x = rect.right - 3
                y = rect.bottom - 3
                hit_br = u.SendMessageW(hwnd, 0x0084, 0, ((y & 0xFFFF) << 16) | (x & 0xFFFF))
            import sys as _sys
            _app = _sys.modules.get("app")
            _ref = getattr(_app, "_wndproc_ref", {}) if _app else {}
            _back = None
            try:
                _back = window_color = self._window.native.BackColor.ToArgb() & 0xFFFFFF
            except Exception:
                _back = None
            return {
                "ok": True, "hwnd": hwnd, "style": style,
                "wndproc": bool(_ref.get("installed")), "back": _back,
                "thick": bool(style & 0x00040000),      # WS_THICKFRAME
                "caption": bool(style & 0x00C00000),    # WS_CAPTION
                "w": rect.right - rect.left, "h": rect.bottom - rect.top,
                "cw": crect.right - crect.left, "ch": crect.bottom - crect.top,
                "max": self._is_max(), "hit_br": hit_br,
            }
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def win_minimize(self):
        hwnd = self._hwnd()
        if not hwnd:
            return {"ok": False, "msg": "没有窗口句柄"}
        try:
            ctypes.windll.user32.ShowWindow(hwnd, 6)      # SW_MINIMIZE
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def win_toggle_max(self):
        """
        最大化 / 还原。

        ⚠️ 不用 pywebview 的 maximize()/restore()（那是 WinForms WindowState）：
           在"外部去掉标题栏"之后，WinForms 还原时会用自己记的 RestoreBounds，
           和实际边框差 8px —— 每还原一次窗口就缩小 16px（实测）。
           用 Win32 ShowWindow 则完全由系统管，来回切尺寸不变。
        """
        hwnd = self._hwnd()
        if not hwnd:
            return {"ok": False, "msg": "没有窗口句柄"}
        try:
            from ctypes import wintypes
            u = ctypes.windll.user32
            if u.IsZoomed(hwnd):
                u.ShowWindow(hwnd, 9)      # SW_RESTORE
                # 系统"还原"会按它自己记的矩形放，和我们的边框差 8px（实测每还原一次小 16px）。
                # 所以还原后 120ms 再按还原前记下的矩形摆一次，尺寸才稳住。
                r = getattr(self, "_restore_rect", None)
                if r:
                    def put():
                        try:
                            u.SetWindowPos(hwnd, 0, r[0], r[1], r[2], r[3],
                                           0x0004 | 0x0010)   # NOZORDER | NOACTIVATE
                        except Exception:
                            pass
                    threading.Timer(0.12, put).start()
            else:
                rect = wintypes.RECT()
                u.GetWindowRect(hwnd, ctypes.byref(rect))
                self._restore_rect = (rect.left, rect.top,
                                      rect.right - rect.left, rect.bottom - rect.top)
                u.ShowWindow(hwnd, 3)      # SW_MAXIMIZE
            return {"ok": True, "max": self._is_max()}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def win_close(self):
        try:
            self._window.destroy()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def win_drag(self):
        """
        开始拖动窗口。

        做法：ReleaseCapture + WM_NCLBUTTONDOWN(HTCAPTION) —— 让 Windows 以为
        用户按在了标题栏上，之后的移动、贴边分屏、拖到顶部最大化，全由系统接管。

        ⚠️ SendMessage 会**阻塞到鼠标松开**，所以必须丢到后台线程里发，
           否则 pywebview 的调用线程会被占用好几秒。
        """
        hwnd = self._hwnd()
        if not hwnd:
            return {"ok": False}

        def go():
            try:
                user32 = ctypes.windll.user32
                user32.ReleaseCapture()
                user32.SendMessageW(hwnd, 0x00A1, 2, 0)   # WM_NCLBUTTONDOWN, HTCAPTION
            except Exception:
                pass

        threading.Thread(target=go, daemon=True).start()
        return {"ok": True}

    def win_drag_to(self, screen_x, screen_y):
        """
        拖动结束后把窗口放到指定位置（拖动过程中前端不跟踪，用不上；
        留着是为了将来支持"拖到屏幕另一半自动分屏"之类）。当前不调用。
        """
        try:
            self._window.move(int(screen_x), int(screen_y))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    # ---------------- 诊断包 / 更新检查（v2.8.7） ----------------
    HOMEPAGE_REPO = "tugepaopao/Invoice-Renaming-Tool"

    def export_diagnostics(self):
        """
        导出诊断包：环境 + 组件自检 + 配置（脱敏）+ 本批概况，打包成 zip。

        为什么需要：上次「pymupdf 缺 _extra.pyd」只能靠用户截图，
        这张图还只能看到半行报错。有了诊断包，用户点一下、把 zip 发过来就能定位。

        ⚠️ 绝不包含发票内容、也不含完整磁盘路径（只留文件夹名）。
        """
        try:
            import datetime
            import platform

            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            target = None
            for d in ("Desktop", "Downloads"):
                cand = os.path.join(os.path.expanduser("~"), d)
                if os.path.isdir(cand):
                    target = cand
                    break
            target = target or tempfile.gettempdir()
            path = os.path.join(target, "发票工具诊断包_%s.zip" % stamp)

            # ---- 组件自检：打包版缺 pyd 时，这里一眼能看出来 ----
            res = resource_dir()
            pdir = os.path.join(res, "pymupdf")
            comp = {}
            for name in ("_extra.pyd", "_mupdf.pyd", "__init__.py"):
                p2 = os.path.join(pdir, name)
                comp[name] = ("存在 %.1f MB" % (os.path.getsize(p2) / 1024 / 1024)
                              if os.path.exists(p2) else "缺失")

            env = [
                "全电发票批量重命名工具 · 诊断包",
                "生成时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "版本：%s" % VERSION,
                "",
                "── 运行环境 ──",
                "打包运行：%s" % bool(getattr(sys, "frozen", False)),
                "Python：%s" % sys.version.split()[0],
                "系统：%s %s" % (platform.system(), platform.release()),
                "64 位：%s" % (sys.maxsize > 2 ** 32),
                "CPU 核心：%s" % (os.cpu_count() or "?"),
                "exe：%s" % sys.executable,
                "资源目录：%s" % res,
                "数据目录：%s" % data_dir(),
                "配置：%s" % CONFIG_PATH,
                "",
                "── 解析组件 ──",
            ]
            env += ["%s：%s" % (k, v) for k, v in comp.items()]
            try:
                import pymupdf
                env.append("pymupdf 版本：%s" % (pymupdf.version[0],))
                env.append("pymupdf 位置：%s" % getattr(pymupdf, "__file__", "?"))
            except Exception as exc:
                env.append("pymupdf 导入失败：%s" % exc)

            # ---- 配置脱敏：只保留文件夹名 ----
            cfg = {}
            try:
                cfg = dict(load_config() or {})
            except Exception:
                pass
            if cfg.get("lastFolder"):
                cfg["lastFolder"] = "…\\" + os.path.basename(
                    str(cfg["lastFolder"]).rstrip("\\/"))

            rows = self.items or []
            summary = {
                "发票数": len(rows),
                "已识别": sum(1 for r in rows if getattr(r, "ok", False)),
                "格式分布": {},
                "票种分布": {},
            }
            for r in rows:
                summary["格式分布"][r.fmt or "?"] = summary["格式分布"].get(r.fmt or "?", 0) + 1
                t = (r.inv_type or "未识别")
                summary["票种分布"][t] = summary["票种分布"].get(t, 0) + 1

            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("环境与组件.txt", "\r\n".join(env))
                z.writestr("配置（已脱敏）.json",
                           json.dumps(cfg, ensure_ascii=False, indent=2))
                z.writestr("本批概况.json",
                           json.dumps(summary, ensure_ascii=False, indent=2))
            return {"ok": True, "path": path}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def cn_amount(self, value):
        """金额 → 中文大写（打印报销单的「合计」用）"""
        try:
            import check_core as CC
            return {"ok": True, "text": CC.amount_to_cn(float(value or 0))}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def reveal_file(self, path):
        """在资源管理器里定位到刚导出的文件"""
        try:
            if path and os.path.exists(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                return {"ok": True}
            return {"ok": False, "msg": "文件不在了"}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def check_update(self):
        """
        检查有没有新版本（只查 GitHub Releases，不下载）。

        设计取舍：不做静默下载安装 —— 一个财务工具在后台替换自己的 exe，
        风险比省几次点击大得多。发现新版本就提示，用户点「打开发布页」自己下。
        """
        try:
            import urllib.request
            import urllib.error
            url = ("https://api.github.com/repos/%s/releases/latest" % self.HOMEPAGE_REPO)
            req = urllib.request.Request(url, headers={
                "User-Agent": "invoice-renamer", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            latest = str(data.get("tag_name") or "").lstrip("vV")
            page = data.get("html_url") or ("https://github.com/" + self.HOMEPAGE_REPO + "/releases")
            return {"ok": True, "latest": latest, "current": VERSION,
                    "url": page, "notes": (data.get("body") or "")[:1200],
                    "newer": _vtuple(latest) > _vtuple(VERSION)}
        except urllib.error.HTTPError as exc:
            page = ("https://github.com/%s/releases" % self.HOMEPAGE_REPO)
            if exc.code in (403, 429):
                # GitHub 对未登录请求限量（每小时 60 次），用完就是这个错
                return {"ok": False, "url": page,
                        "msg": "GitHub 的查询次数用完了（每个网络每小时 60 次）。\n"
                               "过一会儿再试，或者直接去发布页看。"}
            return {"ok": False, "url": page,
                    "msg": "检查更新失败（HTTP %s）。可以先去发布页看看。" % exc.code}
        except Exception as exc:
            page = ("https://github.com/%s/releases" % self.HOMEPAGE_REPO)
            return {"ok": False, "url": page,
                    "msg": "连不上 GitHub（%s）。\n公司网络可能拦了，可以先去发布页看看。" % exc}

    # ---------------- 项目主页（v2.8.1） ----------------
    HOMEPAGE = "https://github.com/tugepaopao/Invoice-Renaming-Tool"

    def open_homepage(self, url=None):
        """
        点「作者 豪崽子」时打开项目主页。

        和之前发邮件一样走 Python 的 webbrowser，而不是页面里的 <a target="_blank">：
        WebView2 对"打开外部浏览器"有导航策略，直接写链接多数时候点了没反应。
        """
        try:
            import webbrowser
            target = url or self.HOMEPAGE
            if webbrowser.open(target):
                return {"ok": True, "url": target}
        except Exception:
            pass
        # 兜底：把网址复制到剪贴板
        copied = False
        try:
            subprocess.run("clip", input=self.HOMEPAGE.encode("ascii"), check=True,
                           shell=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            copied = True
        except Exception:
            copied = False
        return {"ok": False, "url": self.HOMEPAGE,
                "msg": ("打不开浏览器，网址已复制：%s" % self.HOMEPAGE) if copied
                       else ("请手动访问 %s" % self.HOMEPAGE)}

    # ---------------- 跨批次台账（v2.7） ----------------
    def history_stats(self):
        """跨批次台账里记了多少张票 —— 给「操作历史」弹窗显示与清空用"""
        return {"ok": True, "count": DH.INDEX.count(), "file": DH.INDEX.path}

    def clear_history_index(self):
        """
        清空跨批次台账。

        ⚠️ 只清「我处理过哪些票」这份记忆，不动发票目录、也不动操作历史记录。
        换电脑、换年度时用得上。
        """
        DH.INDEX.clear()
        self.seen_before = {}
        return {"ok": True, "msg": "跨批次记录已清空，下次不会再提示「以前处理过」"}

    # ---------------- 视图设置（v2.7） ----------------
    def save_view(self, patch=None):
        """
        记住排序方式、规则卡是否折叠这些「看的人自己的习惯」。

        为什么不放在 set_rule 里：那些是**改文件名**的规则，改一次要生效到文件上；
        这些纯粹是界面偏好，混在一起会让「规则」这个概念变糊。
        """
        view = dict(self.cfg.get("view") or {})
        view.update(patch or {})
        self.cfg["view"] = view
        save_config(self.cfg)
        return {"ok": True, "view": view}

    # ---------------- 对外：合并 PDF ----------------
    def merge_preview(self):
        """
        合并弹窗的数据源：勾选的 PDF（按列表顺序）+ 被跳过的非 PDF。

        ⚠️ 只有 PDF 能合并（OFD / XML 合不了，PyMuPDF 不支持 OFD 渲染）。
           跳过的文件必须在这里显式列出来并说明原因 ——
           静默少几个文件，用户只会以为程序漏了。
        """
        items, skipped = [], []
        for i, it in enumerate(self.items):
            if i >= len(self.checked) or not self.checked[i]:
                continue
            if (it.fmt or "").upper() == "PDF":
                items.append({
                    "i": i,
                    "file": it.file_name,
                    "name": os.path.splitext(it.file_name)[0],
                    "pages": MC.page_count(it.path),
                })
            else:
                skipped.append({"file": it.file_name, "fmt": it.fmt or "未知"})

        base = self.folder or RC.common_base_dir([it.path for it in self.items])
        return {
            "ok": True,
            "items": items,
            "skipped": skipped,
            "totalPages": sum(x["pages"] for x in items),
            "defaultDir": base,
            "defaultName": "合并_%s_%d份.pdf"
                           % (datetime.now().strftime("%Y%m%d"), len(items)),
        }

    def choose_merge_dir(self, current=""):
        """选合并结果的保存位置（只选目录；文件名在弹窗的输入框里填）"""
        import webview
        try:
            res = self._window.create_file_dialog(dlg_kind("FOLDER"),
                                                  directory=current or "")
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
        if not res:
            return {"ok": False, "msg": "已取消"}
        return {"ok": True, "dir": res[0] if isinstance(res, (list, tuple)) else res}

    def merge_run(self, idxs, out_dir, out_name, bookmark=True):
        """
        执行合并。`idxs` 是行号数组，**顺序即合并顺序**（前端可上下调整）。

        单份失败不中断（加密 / 损坏的跳过），最后把失败清单带回前端 ——
        10 份里坏 1 份，另外 9 份照样得合出来。
        """
        paths = []
        for raw in (idxs or []):
            try:
                it = self.items[int(raw)]
            except (IndexError, ValueError, TypeError):
                continue
            if (it.fmt or "").upper() == "PDF" and os.path.exists(it.path):
                paths.append(it.path)
        if not paths:
            return {"ok": False, "msg": "没有勾选可合并的 PDF"}

        out_dir = out_dir or self.folder or os.path.dirname(paths[0])
        if not os.path.isdir(out_dir):
            return {"ok": False, "msg": "保存位置不存在：%s" % out_dir}

        name = (out_name or "").strip() or \
            "合并_%s.pdf" % datetime.now().strftime("%Y%m%d")
        # 只取文件名部分：万一用户手输进路径，也别把文件写到别的目录去
        name = os.path.basename(name)
        if not name.lower().endswith(".pdf"):
            name += ".pdf"

        return self._start("merge", lambda: MC.merge(
            paths, os.path.join(out_dir, name),
            bookmark=bool(bookmark), on_progress=self._progress))

    def reveal_path(self, path):
        """在资源管理器里定位某个文件（合并完成后用）"""
        try:
            if path and os.path.exists(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                return {"ok": True}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
        return {"ok": False, "msg": "文件不存在"}

    # ---------------- 对外：导出 ----------------
    # ---------------- 对外：台账 / 批量操作 / 查验 ----------------
    @staticmethod
    def _unique_dir(path):
        """
        目录不覆盖：已存在且**非空**就换成 `name (2)`。

        ⚠️ 直接复用同名目录会把两次打包的内容混在一起，用户很难分辨谁是谁。
           空目录（比如上次建了没用）可以复用，省得堆一串 (2)(3)。
        """
        if not os.path.exists(path):
            return path
        try:
            if not os.listdir(path):
                return path
        except OSError:
            pass
        n = 2
        while True:
            cand = "%s (%d)" % (path, n)
            if not os.path.exists(cand):
                return cand
            n += 1

    def _trip_brief(self, idx):
        """
        行程单的展示摘要（给附属行用）。

        取不到就返回空壳而不是抛异常 —— 界面渲染不能因为一条数据缺失就整体崩掉。
        """
        try:
            t = self.items[idx].trip or {}
        except (IndexError, AttributeError):
            t = {}
        return {"platform": t.get("platform") or "行程单",
                "trips": t.get("trips") or 0,
                "totalText": money(t.get("total")),
                "from": t.get("date_from") or "",
                "to": t.get("date_to") or "",
                "rows": len(t.get("rows") or [])}

    def _ledger_rows(self):
        """
        组装台账行。

        发票行带上「附件」和「校验」两列 —— 这两个信息在 Excel 里比在界面上更有用：
        交报销 / 做底稿时，别人看到的是一张自解释的表。
        """
        finals = self._final_names()
        attach = self.pairs.get("attach", {})
        pending = {p["trip"]: p["reason"] for p in self.pairs.get("pending", [])}
        rows = []
        for i in range(len(self.items)):
            it = self.items[i]
            r = self._row(i, finals)
            if it.kind == "trip":
                t = it.trip or {}
                r["kindText"] = "行程单"
                r["attachText"] = "%s · %d 趟" % (t.get("platform") or "行程单",
                                                  t.get("trips") or 0)
                r["checkText"] = pending.get(i, "")
            else:
                r["kindText"] = "发票"
                names = []
                for a in (r.get("attached") or []):
                    tt = self.items[a["i"]].trip or {}
                    names.append("%s %d趟" % (tt.get("platform") or "行程单",
                                             tt.get("trips") or 0))
                r["attachText"] = "、".join(names)
                r["checkText"] = "；".join(c["msg"] for c in (r.get("checks") or []))
            rows.append(r)
        return rows

    def export_excel(self):
        """
        导出 Excel 台账。

        为什么必须做 xlsx 而不是接着用 CSV：
            **20 位发票号码用 Excel 打开 CSV 会变科学计数法、末几位丢失**，
            而 CSV 格式没有「单元格格式」这个概念，从 CSV 侧根本解决不了。
        """
        if not self.items:
            return {"ok": False, "msg": "列表为空"}
        import webview
        default = "发票台账_%s.xlsx" % datetime.now().strftime("%Y%m%d_%H%M")
        try:
            res = self._window.create_file_dialog(
                dlg_kind("SAVE"), save_filename=default,
                file_types=("Excel 工作簿 (*.xlsx)", "所有文件 (*.*)"))
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
        if not res:
            return {"ok": False, "msg": "已取消"}
        path = res if isinstance(res, str) else res[0]
        if not str(path).lower().endswith(".xlsx"):
            path = str(path) + ".xlsx"
        try:
            from export_core import write_ledger
            return write_ledger(path, self._ledger_rows(),
                                {"folder": self.folder})
        except Exception as exc:
            return {"ok": False, "msg": "导出失败：%s" % exc}

    def rename_batch(self, idxs, op, text, text2=""):
        """
        批量修改「新文件名」。

        ⚠️ 只改**待改的名字**（内存里的），不碰磁盘 ——
           真正的改名仍然由「开始重命名」统一下手，这样才能整体撤销。
           批量操作最怕「一改就落盘、错了没法回退」。

        op: prefix 加前缀 / suffix 加后缀（扩展名前）/ replace 替换 / clear 恢复自动
        """
        try:
            idxs = [int(i) for i in (idxs or [])]
        except (TypeError, ValueError):
            return {"ok": False, "msg": "行号格式不对"}
        text = (text or "").strip()
        text2 = (text2 or "").strip()
        if not idxs:
            return {"ok": False, "msg": "没有选中任何行"}
        if op in ("prefix", "suffix") and not text:
            return {"ok": False, "msg": "请填写要添加的内容"}
        if op == "replace" and not text:
            return {"ok": False, "msg": "请填写要被替换掉的内容"}

        n = 0
        for i in idxs:
            if not (0 <= i < len(self.items)):
                continue
            it = self.items[i]
            if it.kind == "trip":
                continue        # 行程单的名字跟着发票走，不给单独改
            base = self.edited.get(i) or self._new_name(i)
            if op == "prefix":
                new = text + base
            elif op == "suffix":
                new = base + text
            elif op == "replace":
                if text not in base:
                    continue
                new = base.replace(text, text2)
            elif op == "clear":
                self.edited.pop(i, None)
                n += 1
                continue
            else:
                return {"ok": False, "msg": "不支持的操作：%s" % op}
            new = RC.safe_name(new, "")
            if new:
                self.edited[i] = new
                n += 1
        return {"ok": True, "changed": n, "snapshot": self._snapshot()}

    def pack_reimbursement(self, idxs, out_dir, name, with_list=True):
        """
        把选中的凭证打包到一个文件夹（报销时一次性交出去）。

        选了发票会**自动带上它挂着的行程单** —— 报销本来就要一起交，
        让用户再手动挑一遍纯属折腾。

        v2.7 两处增强：
          · `with_list=True` 时在包里多生成一个「报销清单.xlsx」，
            报销单交上去对方要的就是这张纸（几张、多少钱）。
          · 复制改成多线程：U 盘/网盘上 200 个大文件能快 2~3 倍。
        """
        try:
            idxs = [int(i) for i in (idxs or [])]
        except (TypeError, ValueError):
            return {"ok": False, "msg": "行号格式不对"}
        if not idxs:
            return {"ok": False, "msg": "没有选中任何凭证"}

        attach = self.pairs.get("attach", {})
        want = []
        for i in idxs:
            if not (0 <= i < len(self.items)):
                continue
            if i not in want:
                want.append(i)
            for a in attach.get(i, []):          # 自动带上附件
                if a["trip"] not in want:
                    want.append(a["trip"])
        want = [i for i in want if self.items[i].ok]
        if not want:
            return {"ok": False, "msg": "选中的都是未识别文件，没有可打包的内容"}

        out_dir = out_dir or self.folder or \
            RC.common_base_dir([self.items[i].path for i in want])
        if not out_dir or not os.path.isdir(out_dir):
            return {"ok": False, "msg": "保存位置不存在"}

        name = RC.safe_name(os.path.basename(
            (name or "").strip() or
            "报销单_%s" % datetime.now().strftime("%Y%m%d")), "报销单")
        finals = self._final_names()

        def work():
            dest = self._unique_dir(os.path.join(out_dir, name))
            os.makedirs(dest, exist_ok=True)
            total = len(want)

            # ---- 第一步：先把每个文件的目标名定下来 ----
            # 必须串行算：要处理同名加序号（(2)(3)…），并行算会撞车。
            jobs, taken, listRows = [], set(), []
            for i in want:
                it = self.items[i]
                ext = os.path.splitext(it.file_name)[1].lower()
                if it.kind == "trip":
                    t = it.trip or {}
                    base = "%s_行程单" % (t.get("platform") or "行程单")
                else:
                    base = os.path.splitext(finals.get(i, it.file_name))[0]
                base = RC.safe_name(base, "凭证")
                dst = os.path.join(dest, base + ext)
                k = 2
                while os.path.exists(dst) or dst.lower() in taken:   # 绝不覆盖
                    dst = os.path.join(dest, "%s(%d)%s" % (base, k, ext))
                    k += 1
                taken.add(dst.lower())
                jobs.append((it, dst))
                if it.kind == "trip":
                    listRows.append({"name": os.path.basename(dst),
                                     "buyer": "（附件）",
                                     "amount": (it.trip or {}).get("total"),
                                     "no": ""})
                else:
                    listRows.append({"name": os.path.basename(dst),
                                     "buyer": it.buyer or "",
                                     "amount": it.amount,
                                     "no": it.invoice_no or ""})

            # ---- 第二步：并行复制 ----
            done, failed = [], []
            lock = threading.Lock()

            def one(job):
                it, dst = job
                try:
                    shutil.copy2(it.path, dst)
                    with lock:
                        done.append(os.path.basename(dst))
                        n = len(done) + len(failed)
                        self._progress(n, total, os.path.basename(it.path))
                except Exception as exc:
                    with lock:
                        failed.append({"file": it.file_name, "error": str(exc)})

            if total <= 2:
                for j in jobs:
                    one(j)
            else:
                with ThreadPoolExecutor(max_workers=min(8, max(2, total))) as ex:
                    list(ex.map(one, jobs))

            # ---- 第三步：清单（可选） ----
            made_list = ""
            if with_list and listRows:
                try:
                    from export_core import write_pack_list
                    lp = os.path.join(dest, "报销清单.xlsx")
                    write_pack_list(lp, listRows, {
                        "title": name,
                        "note": "本清单由「全电发票批量重命名工具」于 %s 自动生成"
                                % datetime.now().strftime("%Y-%m-%d %H:%M"),
                    })
                    made_list = os.path.basename(lp)
                except Exception as exc:
                    # 清单失败不能把打包结果废掉 —— 文件已经复制好了
                    failed.append({"file": "报销清单.xlsx",
                                   "error": "清单生成失败：%s" % exc})

            self._progress(total, total, "完成")
            return {"ok": True, "dir": dest, "count": len(done),
                    "failed": failed, "files": done, "list": made_list}

        return self._start("pack", work)


    def export_csv(self):
        if not self.items:
            return {"ok": False, "msg": "列表为空"}
        import webview
        default = "发票台账_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M")
        try:
            res = self._window.create_file_dialog(
                dlg_kind("SAVE"), save_filename=default,
                file_types=("CSV 表格 (*.csv)", "所有文件 (*.*)"))
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
        if not res:
            return {"ok": False, "msg": "已取消"}
        path = res if isinstance(res, str) else res[0]

        total = 0.0
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["序号", "原文件名", "购买方", "销售方", "价税合计",
                            "发票号码", "开票日期", "新文件名", "识别状态", "重复"])
                for i in range(len(self.items)):
                    r = self._row(i)
                    if r["amount"] is not None:
                        total += r["amount"]
                    w.writerow([i + 1, r["file"], r["buyer"], r["seller"],
                                r["amount"] if r["amount"] is not None else "",
                                r["no"], r["date"], r["newName"],
                                r["status"], "是" if r["dup"] else ""])
                w.writerow([])
                w.writerow(["", "合计", "", "", round(total, 2), "", "", "", "", ""])
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
        return {"ok": True, "path": path}

    # ---------------- 对外：杂项 ----------------
    def open_external(self, url):
        try:
            os.startfile(url)
            return {"ok": True}
        except Exception:
            return {"ok": False}
