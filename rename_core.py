# -*- coding: utf-8 -*-
"""
重命名核心逻辑（GUI / 桥接层 / 命令行共用）
--------------------------------------------------
职责：
  · 生成重命名计划（含重名处理、客户分发、长路径预警）
  · 发票查重（按发票号码）
  · 执行前自动备份
  · 执行与撤销
"""
import json
import os
import re
import shutil
import uuid
from datetime import datetime

OUT_SUBDIR = "重命名输出"
BACKUP_SUBDIR = "_备份"
LOG_NAME = "_发票重命名记录.json"


def long_path(p):
    """
    Windows 长路径支持（v2.6）。

    Win32 默认路径上限 260 字符（MAX_PATH）。客户公司名很长、又放在
    「D:\\客户资料\\2026年10月\\某某某公司\\」这种深目录里时，os.rename 直接抛
    FileNotFoundError —— 用户只看到「改名失败」，完全不知道是路径太长。

    给绝对路径加 `\\\\?\\` 前缀可以把上限抬到 32767。
    · 只在**系统调用那一刻**加前缀，日志里仍然存正常路径（界面要能看懂）
    · 短路径不加（避免 UNC、相对路径那些边角情况）
    """
    if os.name != "nt" or not p:
        return p
    try:
        ap = os.path.abspath(p)
    except Exception:
        return p
    if ap.startswith("\\\\?\\"):
        return ap
    if len(ap) < 248:          # 留出文件名余量；短路径没必要加
        return ap
    if ap.startswith("\\\\"):
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def path_len_warn(p):
    """路径太长且**加不了**前缀时（理论上的极端情况）给个预警；正常返回空串"""
    try:
        n = len(os.path.abspath(p))
    except Exception:
        return ""
    if n >= 260:
        return "路径 %d 字符，超出 Windows 默认上限（已自动用长路径方式处理）" % n
    return ""

# Windows 文件名非法字符
_ILLEGAL = r'<>:"/\|?*'
# Windows 保留设备名
_RESERVED = {"CON", "PRN", "AUX", "NUL"} \
    | {f"COM{i}" for i in range(1, 10)} \
    | {f"LPT{i}" for i in range(1, 10)}

MAX_NAME_LEN = 200          # 单段文件名保守上限
MAX_FULL_PATH = 255         # 完整路径保守上限


# ---------------------------------------------------------------- 基础工具
def safe_name(name, fallback="未命名"):
    """把任意字符串转成合法文件名（去掉非法字符、保留名、首尾点）"""
    if not name:
        return fallback
    out = "".join("_" if c in _ILLEGAL else c for c in str(name))
    out = out.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    out = re.sub(r"\s{2,}", " ", out).strip().strip(".").strip()
    if not out:
        return fallback
    if out.upper().split(".")[0] in _RESERVED:
        out = "_" + out
    return out[:MAX_NAME_LEN]


class Entry:
    """一条待处理记录"""

    __slots__ = ("src", "base", "invoice_no", "buyer", "amount", "ok")

    def __init__(self, src, base, invoice_no="", buyer="", amount=None, ok=True):
        self.src = src
        self.base = base
        self.invoice_no = invoice_no
        self.buyer = buyer
        self.amount = amount
        self.ok = ok


class PlanItem:
    """计划中的一项"""

    __slots__ = ("index", "src", "dst", "action", "warn")

    def __init__(self, index, src, dst, action="rename", warn=""):
        self.index = index
        self.src = src
        self.dst = dst
        self.action = action          # rename | copy | same | skip
        self.warn = warn              # 预警信息（长路径等）

    @property
    def src_name(self):
        return os.path.basename(self.src)

    @property
    def dst_name(self):
        return os.path.basename(self.dst)


# ---------------------------------------------------------------- 统计 / 查重
def find_duplicates(invoice_nos):
    """
    按发票号码查重。
    invoice_nos: [(index, invoice_no), ...]
    返回 {发票号码: [index, ...]}，只含出现 2 次及以上的。
    """
    seen = {}
    for idx, no in invoice_nos:
        if not no:
            continue
        seen.setdefault(no, []).append(idx)
    return {no: idxs for no, idxs in seen.items() if len(idxs) > 1}


def summarize(amounts):
    """汇总：[金额...] -> (有效张数, 合计, 正数合计, 负数合计)"""
    vals = [a for a in amounts if a is not None]
    return (len(vals),
            round(sum(vals), 2),
            round(sum(v for v in vals if v >= 0), 2),
            round(sum(v for v in vals if v < 0), 2))


# ---------------------------------------------------------------- 计划生成
def build_plan(entries, copy_mode=False, dedup="index", distribute=False):
    """
    entries    : [Entry, ...]
    copy_mode  : True  复制到「重命名输出」而不是原地改名
    dedup      : index | noshort | skip   重名处理
    distribute : True  按购买方名分到子文件夹（客户归档）

    返回 [PlanItem, ...]
    """
    plan, used = [], set()
    all_src = {os.path.abspath(e.src).lower() for e in entries}

    for idx, e in enumerate(entries):
        base = (e.base or "").strip()
        if not base:
            plan.append(PlanItem(idx, e.src, "", "skip"))
            continue

        src_dir = os.path.dirname(e.src)

        if distribute and e.buyer:
            dst_dir = os.path.join(src_dir, safe_name(e.buyer, "未识别购买方"))
        elif copy_mode:
            dst_dir = os.path.join(src_dir, OUT_SUBDIR)
        else:
            dst_dir = src_dir

        src_abs = os.path.abspath(e.src).lower()
        # ⚠️ 扩展名必须跟着源文件走（.pdf / .ofd / .xml）。
        #    这里原来写死 ".pdf"，加了 OFD/XML 支持后会把 .ofd 改成 .pdf，
        #    等于把文件改坏 —— 名字对了、打不开了。
        ext = os.path.splitext(e.src)[1].lower()

        def conflict(name):
            key = os.path.join(dst_dir, name + ext).lower()
            if key in used:
                return True
            p = os.path.join(dst_dir, name + ext)
            if not os.path.exists(p):
                return False
            p_abs = os.path.abspath(p).lower()
            if p_abs == src_abs:
                return False           # 改成自己（重复执行）不算冲突
            if p_abs in all_src:
                return False           # 占位者本批次也会挪走，不算冲突
            return True

        cand = base
        if conflict(cand):
            if dedup == "skip":
                plan.append(PlanItem(idx, e.src, "", "skip"))
                continue
            if dedup == "noshort" and e.invoice_no:
                cand = "%s(%s)" % (base, e.invoice_no[-6:])
            n = 2
            while conflict(cand) and n < 500:
                cand = "%s(%d)" % (base, n)
                n += 1

        dst = os.path.join(dst_dir, cand + ext)
        used.add(dst.lower())

        warn = ""
        if len(os.path.basename(dst)) > MAX_NAME_LEN:
            warn = "文件名过长（%d 字符）" % len(os.path.basename(dst))
        elif len(dst) > MAX_FULL_PATH:
            warn = "完整路径过长（%d 字符），可能失败" % len(dst)

        dst_abs = os.path.abspath(dst).lower()
        same_dir = os.path.abspath(dst_dir).lower() == \
            os.path.abspath(src_dir).lower()

        if dst_abs == src_abs:
            action = "same"
        elif same_dir:
            action = "rename"                    # 原地改名
        elif copy_mode:
            action = "copy"                      # 保留原文件
        else:
            action = "move"                      # 归档：移动到子文件夹

        plan.append(PlanItem(idx, e.src, dst, action, warn))

    return _order_plan(plan)


def _order_plan(plan):
    """
    调整执行顺序：若我的源文件名正是别人的目标名（A→B、B→C），
    我必须先被挪走，否则会互相挡道。
    """
    dst_set = {os.path.abspath(p.dst).lower() for p in plan if p.dst}
    first, rest = [], []
    for p in plan:
        if os.path.abspath(p.src).lower() in dst_set:
            first.append(p)
        else:
            rest.append(p)
    return first + rest


# ---------------------------------------------------------------- 备份
def backup_files(paths, root_dir):
    """
    复制到 <root_dir>/_备份/<时间戳>/。
    返回 (备份目录, 成功数, 失败列表)
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = os.path.join(root_dir, BACKUP_SUBDIR, stamp)
    try:
        os.makedirs(long_path(target), exist_ok=True)
    except Exception as exc:
        return "", 0, [(p, str(exc)) for p in paths]

    ok, failed = 0, []
    for p in paths:
        try:
            shutil.copy2(long_path(p), long_path(os.path.join(target, os.path.basename(p))))
            ok += 1
        except Exception as exc:
            failed.append((p, str(exc)))
    return target, ok, failed


# ---------------------------------------------------------------- 执行
class Runner:
    """执行计划，收集日志。on_progress(done, total, name)"""

    def __init__(self, plan, do_backup=False, backup_root=None,
                 on_progress=None, batch_id=""):
        self.plan = plan
        self.do_backup = do_backup
        self.backup_root = backup_root
        # 批次号：一次「开始重命名」= 一批。撤销时可以只回滚某一批（v2.6）
        self.batch_id = batch_id or ("B%s-%s" % (
            datetime.now().strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:4]))
        self.on_progress = on_progress
        self.log = []
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.backup_dir = ""
        self.backup_failed = []

    def run(self):
        todo = [p for p in self.plan if p.action in ("rename", "copy", "move")]

        if self.do_backup and todo and self.backup_root:
            self.backup_dir, _n, self.backup_failed = backup_files(
                [p.src for p in todo], self.backup_root)

        total = len(todo)
        for i, p in enumerate(todo, 1):
            rec = {"from": p.src, "to": p.dst,
                   "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "mode": p.action,
                   "batch": self.batch_id,
                   "backup": self.backup_dir or ""}
            try:
                d = os.path.dirname(p.dst)
                if d and not os.path.isdir(d):
                    os.makedirs(long_path(d), exist_ok=True)
                if p.action == "copy":
                    shutil.copy2(long_path(p.src), long_path(p.dst))
                else:                             # rename / move
                    try:
                        os.rename(long_path(p.src), long_path(p.dst))   # 目标已存在会报错，是道保险
                    except OSError:
                        shutil.move(long_path(p.src), long_path(p.dst))
                self.done += 1
                self.log.append(rec)
            except Exception as exc:
                self.failed += 1
                rec["mode"] = "error"
                rec["error"] = str(exc)
                self.log.append(rec)
            if self.on_progress:
                self.on_progress(i, total, os.path.basename(p.src))

        self.skipped = len(self.plan) - total
        return self.log


# ---------------------------------------------------------------- 日志 / 撤销
def log_path_for(folder):
    return os.path.join(folder, LOG_NAME)


def append_log(folder, records):
    """追加写入操作记录（供撤销使用）"""
    if not folder or not records:
        return
    path = log_path_for(folder)
    try:
        old = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(old + records, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def read_log(folder):
    path = log_path_for(folder)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def pending_undo(folder):
    """可撤销的记录：原地改名或归档移动、目标还在、原位置空闲"""
    return [r for r in read_log(folder)
            if r.get("mode") in ("rename", "move")
            and os.path.exists(r.get("to", ""))
            and not os.path.exists(r.get("from", ""))]


def _undoable(r):
    """这条记录现在还能不能原地撤回"""
    return (r.get("mode") in ("rename", "move")
            and os.path.exists(r.get("to") or "")
            and not os.path.exists(r.get("from") or ""))


def undo_folder(folder):
    """撤销**全部**可撤销记录（LIFO 顺序）。返回 (本次处理的记录, 成功还原数)"""
    log = read_log(folder)
    ops = [r for r in log if _undoable(r)]

    done = 0
    for r in reversed(ops):
        try:
            d = os.path.dirname(r["from"])
            if d and not os.path.isdir(d):
                os.makedirs(long_path(d), exist_ok=True)
            os.rename(long_path(r["to"]), long_path(r["from"]))
            r["mode"] = "undone"
            done += 1
        except Exception:
            pass

    if done:
        try:
            with open(log_path_for(folder), "w", encoding="utf-8") as f:
                json.dump(log, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return ops, done


def undo_batch(folder, batch):
    """
    只回滚**某一批**（v2.6）。返回 (这批里的记录, 成功还原数)。

    为什么需要：老版本只有一个「撤销上次」，一点就把**所有**能撤的记录全撤了 ——
    用户连做了三次改名，只想退最近那次，结果三次全退回去了，很吓人。
    现在每条记录都带 batch，界面上按批次列出来，撤哪批由用户点。
    """
    log = read_log(folder)
    ops = [r for r in log if (r.get("batch") or "") == (batch or "") and _undoable(r)]

    done = 0
    for r in reversed(ops):
        try:
            d = os.path.dirname(r["from"])
            if d and not os.path.isdir(d):
                os.makedirs(long_path(d), exist_ok=True)
            os.rename(long_path(r["to"]), long_path(r["from"]))
            r["mode"] = "undone"
            done += 1
        except Exception:
            pass

    if done:
        try:
            with open(log_path_for(folder), "w", encoding="utf-8") as f:
                json.dump(log, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return ops, done


def history(folder, limit=40):
    """
    操作历史（按批次倒序）。

    每条：{"batch": id, "at": 最早一条的时间, "count": 条数,
           "undoable": 还能撤几条, "renamed": 改名几条, "moved": 归档几条,
           "label": "改名 19 个"}
    "batch" 为空的是 v2.6 之前的记录，统一归到 legacy 一组。
    """
    log = read_log(folder)
    groups = {}
    order = []
    for r in log:
        b = r.get("batch") or "legacy"
        if b not in groups:
            groups[b] = []
            order.append(b)
        groups[b].append(r)

    out = []
    for b in order:
        recs = groups[b]
        renamed = sum(1 for r in recs if r.get("mode") == "rename")
        moved = sum(1 for r in recs if r.get("mode") == "move")
        failed = sum(1 for r in recs if r.get("mode") == "error")
        undone = sum(1 for r in recs if r.get("mode") == "undone")
        undoable = sum(1 for r in recs if _undoable(r))
        at = ""
        for r in recs:
            t = r.get("time") or ""
            if t and (not at or t < at):
                at = t
        bits = []
        if renamed:
            bits.append("改名 %d 个" % renamed)
        if moved:
            bits.append("归档 %d 个" % moved)
        if undone:
            bits.append("已撤销 %d 个" % undone)
        if failed:
            bits.append("失败 %d 个" % failed)
        out.append({
            "batch": b,
            "at": at,
            "count": len(recs),
            "undoable": undoable,
            "renamed": renamed,
            "moved": moved,
            "failed": failed,
            "undone": undone,
            "label": "　".join(bits) or "%d 条记录" % len(recs),
        })
    out.reverse()                       # 新的在前
    return out[:limit]


def common_base_dir(paths):
    """取一组文件的共同父目录"""
    dirs = list({os.path.dirname(p) for p in paths if p})
    if not dirs:
        return ""
    if len(dirs) == 1:
        return dirs[0]
    try:
        return os.path.commonpath(dirs)
    except ValueError:
        return dirs[0]
