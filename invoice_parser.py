# -*- coding: utf-8 -*-
"""
发票信息提取模块（数电票 PDF / OFD / XML 三种交付格式）
--------------------------------------------------
从三种格式里提取同一组字段：
    · 购买方名称   · 价税合计
    · 发票号码 / 开票日期 / 销售方名称
    · 发票类型 / 购销方税号 / 不含税金额 / 税额 / 大写金额 / 备注 / 开票人

三种格式的解析难度天差地别：
    1) **XML**（电子税务局下载的 .xml，或 .zip 里那个 xml）——最容易。
       字段是结构化的，直接按标签取值，不用猜版式。
    2) **OFD**（zip + XML）——同样容易。OFD 里有一份 `CustomTag.xml`
       存放「语义字段名 → 对象 ID」的映射（如 BuyerName → 6924），
       拿着 ID 去 Content.xml 里取文本即可，完全不用猜版式。
    3) **PDF** —— 最难。只有文本层，得靠坐标重建视觉行再按标签取值，
       详见 _collect_lines / _extract_extras 里的注释。

设计原则（三种格式一致）：
    · 能用结构化位置定位的，就不靠模糊正则猜
    · 提不到就留空 —— 猜错的税号/金额比空着危险得多
"""

import os
import posixpath
import re
import threading
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from cache_core import CACHE

# PyMuPDF 改成**按需加载**（v2.7）。
# 为什么要改：实测 `import pymupdf` 要 ~580ms，而它只在真正解析 PDF 时才用得上。
# 以前在模块顶层导入，等于开软件先干等半秒；现在界面先出来，第一张 PDF
# 需要时再加载，用户感知的启动时间直接少一截。
# ⚠️ 全文件统一走 _fitz()，不要直接写 pymupdf.open —— 那样会拿到 None。
# ⚠️ 这段必须在 `import pair_core` **之前**：pair_core 反过来要借用 _fitz，
#    放到后面会形成循环导入（v2.7 踩过一次，报 cannot import name '_fitz'）。
pymupdf = None


def _fitz():
    """取 PyMuPDF 模块（第一次调用时才真正导入）"""
    global pymupdf
    if pymupdf is None:
        try:
            import pymupdf as _m          # PyMuPDF >= 1.24
        except ImportError:               # pragma: no cover
            import fitz as _m
        pymupdf = _m
    return pymupdf


# pair_core 也要用 _fitz，所以只能等上面定义完再导入（见上面的循环导入说明）
import pair_core as PC                                    # noqa: E402

# 支持导入的发票文件扩展名。改这里就能全站生效 ——
# 历史上扩展名判断散落在 parser / bridge / rename_core 三处，加格式时漏改过。
INVOICE_EXTS = (".pdf", ".ofd", ".xml")
ARCHIVE_EXTS = (".zip",)

# 列表/详情里展示的格式标签
EXT_LABEL = {".pdf": "PDF", ".ofd": "OFD", ".xml": "XML"}


# ---------------------------------------------------------------- 数据结构
class InvoiceInfo:
    """单张发票的解析结果"""

    __slots__ = ("path", "file_name", "ok", "buyer", "seller", "amount",
                 "amount_text", "invoice_no", "date", "engine", "message",
                 "raw_text", "is_red", "fmt",
                 # ---- 扩展字段：供「命名规则」勾选使用 ----
                 "inv_type", "inv_code", "buyer_tax", "seller_tax",
                 "net", "tax", "amount_cn", "remark",
                 "payee", "reviewer", "drawer",
                 # ---- 凭证类型：发票 / 行程单（v2.5）----
                 "kind", "trip",
                 # ---- v2.6：一个 PDF 里有多张票 / 加密需要密码 ----
                 "multi", "need_password")

    def __init__(self, path: str):
        self.path = path
        self.file_name = os.path.basename(path)
        self.ok = False
        self.buyer = ""
        self.seller = ""
        self.amount = None          # float，可为负数（红字发票）
        self.amount_text = ""
        self.invoice_no = ""
        self.date = ""
        self.engine = ""            # 命中的解析策略
        self.message = ""           # 失败原因 / 提示
        self.raw_text = ""
        self.is_red = False         # 是否为红字（负数）发票
        self.fmt = EXT_LABEL.get(os.path.splitext(path)[1].lower(), "")
        # ---- v2.6 ----
        # 一个 PDF 里有多张发票时：{"count": n, "groups": [{"pages": [0,1], ...}]}
        self.multi = None
        # 加密 PDF，还没拿到密码（界面据此弹输入框）
        self.need_password = False
        # ---- 扩展字段（提不到就留空，不做模糊猜测）----
        self.inv_type = ""          # 发票类型：增值税专用发票 / 普通发票 …
        self.inv_code = ""          # 发票代码（数电票已取消，仅老版票有）
        self.buyer_tax = ""         # 购买方纳税人识别号
        self.seller_tax = ""        # 销售方纳税人识别号
        self.net = None             # 不含税金额（合计金额）
        self.tax = None             # 税额（合计税额）
        self.amount_cn = ""         # 价税合计（大写）
        self.remark = ""            # 备注
        self.payee = ""             # 收款人
        self.reviewer = ""          # 复核人
        self.drawer = ""            # 开票人
        # ---- 凭证类型（2026-10 起）----
        # "invoice" = 发票（主凭证）；"trip" = 行程单/水单（配套附件）。
        # 行程单不是坏文件，老版本把它标成「未识别」是错的 ——
        # 它是网约车/酒店报销的标配附件，要跟发票配对而不是报错。
        self.kind = "invoice"
        self.trip = None            # 行程单的结构化信息（见 pair_core.parse_trip）

    def __repr__(self):
        return (f"<InvoiceInfo {self.file_name} ok={self.ok} "
                f"buyer={self.buyer!r} amount={self.amount}>")


# ---------------------------------------------------------------- 常量
# 竖排的「购买方信息 / 销售方信息」在文本流中会散成单个汉字，
# 出现在名称首尾处，需要裁掉。
LABEL_CHARS = "购买方信息销售"
_LEAD_LABEL = re.compile(r"^(?:[" + LABEL_CHARS + r"]+(?=[\s：:])|[\s：:]+)+")
_TAIL_LABEL = re.compile(r"(?:[\s：:]+|[\s]+[" + LABEL_CHARS + r"]+)+$")

# 在同一行里，遇到这些词说明已经越界到别的栏目了
STOP_WORDS = (
    "统一社会信用代码", "纳税人识别号", "识别号", "地址", "电话",
    "开户行", "账号", "备注", "项目名称", "规格型号", "密码区",
)

RE_AMOUNT_LOWER = re.compile(
    r"[（(]\s*小\s*写\s*[）)]\s*[¥￥]?\s*(-?\d[\d,，]*(?:\.\d+)?)"
)
RE_CURRENCY = re.compile(r"[¥￥]\s*(-?\d[\d,，]*(?:\.\d+)?)")
RE_AMOUNT_PLAIN = re.compile(r"(?<![\d.])(-?\d[\d,，]*\.\d{2})(?![\d])")
RE_INVOICE_NO = re.compile(r"发票号码\s*[：:]?\s*(\d{15,25})")
RE_INVOICE_NO_FALLBACK = re.compile(r"(?<!\d)(\d{20})(?!\d)")
RE_DATE = re.compile(r"开票日期\s*[：:]?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
RE_DATE_ALT = re.compile(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?!\d)")

# 文件名里常见的发票号码（20 位）
RE_FILE_NO = re.compile(r"(?<!\d)(\d{20})(?!\d)")
RE_FILE_TS = re.compile(r"(?<!\d)(\d{14})(?!\d)")

# ---------------------------------------------------------------- 扩展字段
# 「命名规则」勾选功能用到的更多票面字段。统一「宽松匹配 + 找不到就留空」：
# 税号、金额这类信息猜错了比空着更危险，所以绝不做模糊补全。
RE_INVOICE_TYPE = re.compile(
    r"(?:电子发票|数电票|全电发票)\s*[（(]([^）)]{2,24})[）)]")
RE_INVOICE_CODE = re.compile(r"发票代码\s*[：:]?\s*(\d{10,12})")
# 仅匹配标签本身，有两个用途：
#   · 在视觉行里把左右两栏切开，逐栏解析税号（主路径）
#   · 整段文本兜底（老版式 / 罕见排版）
RE_TAX_LABEL = re.compile(
    r"(?:统一社会信用代码(?:/纳税人识别号)?|纳税人识别号)\s*[：:]")
# 票面「价税合计（大写）」后的中文大写金额。
# 红字发票的大写金额有的版式会带「负」前缀，所以字符集里也放一个「负」——
# 带上它反而更有信息量（一眼看出是红冲票），丢了就分辨不出。
#
# ⚠️ 下限只能是 3，不能是 4（2026-10-16 用户真实票踩到）：
#    小额票的大写是「玖元整」「拾元整」——**只有 3 个字**，
#    写 {4,40} 时整段匹配不上，PDF 和 OFD 两条路都会把大写字段丢掉，
#    而 XML 报文里明明有。同一张票三种格式只有两种能取到大写，就说明是正则的问题。
RE_AMOUNT_CN = re.compile(
    r"价税合计\s*[（(]\s*大写\s*[）)]\s*[：:]?\s*"
    r"([零壹贰叁肆伍陆柒捌玖拾佰仟万亿元圆角分整正〇○负]{3,40})")
RE_CN_AMOUNT = re.compile(
    r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿元圆角分整正〇○负]{3,40}")
# 单个汉字（OFD 竖排表头过滤用）
RE_CJK_CHAR = re.compile(r"[\u4e00-\u9fa5]")
RE_MONEY = re.compile(r"-?\d[\d,，]*\.\d{2}")

# ⚠️ 以下三个字段（开票人 / 收款人 / 复核人）**只能**在「视觉行」里取同行右侧的值，
#    绝对不能用「整段文本 + 标签\s*[：:] 值」的正则。
#    原因：真实数电票的 PDF 文本层是**先输出全部标签、再输出全部值**的排列
#    （表单型 PDF 的典型特征），`开票人：` 后面紧邻的那一行往往是**发票号码**。
#    老写法 RE_DRAWER = 开票人\s*[：:]\s*([^\s…]{1,20}) 在真实票上会把
#    `开票人：` + 下一行 20 位号码抓成人名（正好 ≤20 字符，且 \s* 会跨行）。
RE_PERSON_VALUE = re.compile(r"[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z·．. ]{0,19}")
PERSON_LABEL_OF = {"drawer": "开票人", "payee": "收款人", "reviewer": "复核人"}
RE_ANY_PERSON_LABEL = re.compile(r"(开票人|收款人|复核人)\s*[：:]?")
# 人名里不该出现的词（防止把标签本身或整行说明当成人名）
PERSON_STOP = ("备注", "名称", "税号", "识别号", "开票", "收款", "复核",
               "日期", "号码", "合计", "金额", "税额", "价税", "小写",
               "大写", "项目", "规格", "单位", "数量", "单价", "税率")

# 税号：法定长度只有 15 / 17 / 18 / 20 位。
# 不要放宽到 12 位 —— 一段被截断的税号比空着更危险（会生成「看着对、其实错」的文件名）。
RE_TAX_VALUE = re.compile(r"[0-9A-Z]{15,20}")
# 仅匹配标签本身，用于在视觉行里把左右两栏切开
RE_ROW_TAX_LABEL = re.compile(r"统一社会信用代码(?:/纳税人识别号)?|纳税人识别号")
RE_ROW_REMARK = re.compile(r"备注\s*[：:]?")
RE_ROW_LOWER = re.compile(r"[（(]小写[）)]")
RE_ROW_TOTAL_LABEL = re.compile(r"价税合计")


# ---------------------------------------------------------------- 工具函数
def _normalize(s: str) -> str:
    """统一全角空格、冒号等，便于正则匹配"""
    if not s:
        return ""
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = s.replace("（", "（").replace("）", "）")
    return s


def _clean_name(s: str) -> str:
    """清理名称首尾的竖排标签字符与标点"""
    if not s:
        return ""
    s = s.replace("\u3000", " ").strip()
    prev = None
    while prev != s:                      # 反复裁剪，直到稳定
        prev = s
        s = _LEAD_LABEL.sub("", s)
        s = _TAIL_LABEL.sub("", s)
        s = s.strip(" ：:，,、")
    return s.strip()


def _to_float(text: str):
    """'1,060.00' -> 1060.0"""
    if text is None:
        return None
    t = text.strip().replace(",", "").replace("，", "").replace(" ", "")
    if not t or t in "-":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _date_from_text(s: str) -> str:
    """
    各种写法的开票日期 → 'YYYYMMDD'。

    三种格式各写各的：
        PDF   '2026年09月15日'
        OFD   '2026年10月08日'
        XML   '2026-10-08'（IssueTime）/ '2026-10-08 14:49:06'（RequestTime）
    取不到返回空串（绝不用「今天」之类去凑）。
    """
    if not s:
        return ""
    s = s.strip()
    m = re.search(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return "%04d%02d%02d" % (y, mo, d)
        return ""
    m = re.match(r"(\d{4})(\d{2})(\d{2})", s)
    return m.group(0) if m else ""


def _cut_at_stop(s: str) -> str:
    """
    截断到第一个「越界停用词」之前。

    注意：停用词必须出现在词首或前面是空白/标点，才算越界。
    否则公司名里自带这些字（如「上海地址科技有限公司」）会被误截断。
    """
    idx = len(s)
    for w in STOP_WORDS:
        for m in re.finditer(re.escape(w), s):
            p = m.start()
            if p == 0 or s[p - 1].isspace() or s[p - 1] in "，,、：:；;|":
                if p < idx:
                    idx = p
                break
    return s[:idx]


# ------------------------------------------------- 「视觉行」工具（字段提取的地基）
def _compact(text: str) -> str:
    """去掉所有空白，便于在「字符被空格拆开」的票面上做匹配。

    数电票的号码、税号、大写金额经常被渲染成
        `26 3 1 2 0 0 0 0 …` / `9 1 3 1 0 1 …`
    这种一个字一个空格的样子，先压掉空白再匹配才稳。
    """
    return re.sub(r"\s+", "", _normalize(text))


def _row_segments(text: str, rx):
    """
    把一行文本按 rx 的每个匹配切开，返回「每个匹配之后 → 下一个匹配之前」的片段。

    为什么不能只用 find：
        购销方信息是左右两栏，同一视觉行里会出现**两次相同标签**，
        只取第一个会把销售方的值漏掉。
    为什么必须保留空片段：
        购买方是「个人」时票面仍有两个税号标签，但第一栏后面是空的 ——
        切片会得到 ['', '销售方税号']，正好对上「购买方无税号」的真实情况。
    """
    ms = list(rx.finditer(text))
    if not ms:
        return []
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append(text[m.end():end])
    return out


def _looks_like_person(s: str) -> bool:
    """判断一段文本像不像人名：2~20 字、含中文或字母、没有 4 位以上连续数字。"""
    s = (s or "").strip()
    if not (2 <= len(s) <= 20):
        return False
    if not re.search(r"[\u4e00-\u9fa5A-Za-z]", s):
        return False
    if re.search(r"\d{4,}", s):            # 人名里不该出现长数字串
        return False
    return not any(w in s for w in PERSON_STOP)


def _person_from_segment(seg: str) -> str:
    """从切片里取出人名：只吃开头的「连续合法人名串」，后面粘的标签单字一律丢掉。"""
    seg = _normalize(seg)
    m = RE_PERSON_VALUE.match(seg.lstrip())
    if not m:
        return ""
    val = m.group().strip()
    # 尾部可能粘着竖排标签单字（如「销」「信」「息」）
    val = re.sub(r"[\s]*[" + LABEL_CHARS + r"]+$", "", val).strip()
    return val if _looks_like_person(val) else ""


def _extract_person(lines, label_key: str) -> str:
    """
    开票人 / 收款人 / 复核人：值必须与标签出现在**同一视觉行**。

    这是本次修复的核心：真实数电票文本层「先标签后值」，
    跨行取值的正则会把人名写成发票号码（用户截图里的
    「开票人 = 26312000006376520821」就是这么来的）。

    老版式会把「收款人：X 复核人：Y 开票人：Z」挤在一行里，
    所以要按**所有人名标签**把行切开，再取目标标签紧跟的那一段，
    否则会把「复核人」三个字一起吞进人名。
    """
    want = PERSON_LABEL_OF[label_key]
    for _, _, text in lines:
        if want not in text:
            continue
        ms = list(RE_ANY_PERSON_LABEL.finditer(text))
        for i, m in enumerate(ms):
            if m.group(1) != want:
                continue
            end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
            val = _person_from_segment(text[m.end():end])
            if val:
                return val
    return ""


def _pick_net_tax(nums, amount):
    """
    从「合计」行的一串数字里挑出 (不含税金额, 税额)。

    优先挑「两数之和 == 价税合计」的那一组：合计行里可能混着
    数量/单价等数字，用价税合计做勾稽能排除掉。
    勾稽不上时退回最前面的两个（老票/免税票可能对不上）。
    """
    if len(nums) >= 2:
        if amount is not None:
            for i in range(len(nums)):
                for j in range(i + 1, len(nums)):
                    if abs(nums[i] + nums[j] - amount) <= 0.02:
                        return nums[i], nums[j]
        return nums[0], nums[1]
    if len(nums) == 1:
        return nums[0], None
    return None, None


def _extract_remark(lines) -> str:
    """
    备注：两种版式都覆盖。

    ① 「备注：内容」写在同一行的（老版电子发票常见）→ 直接取同行右侧；
    ② 数电票的备注是个**方框**，而且「备」「注」两个字是竖排渲染的，
       与内容不在同一行 —— 这时用「价税合计行」与「开票人行」之间的
       纵向区间当方框范围，取区间内的有效文本。
    """
    for _, _, text in lines:
        c = _compact(text)
        m = RE_ROW_REMARK.match(c)
        if not m:
            continue
        tail = c[m.end():]
        if tail:
            return tail
        break                      # 标签同行没内容 → 走 ② 的方框解析

    y1 = y2 = None
    for _, y0, text in lines:
        c = _compact(text)
        if y1 is None and RE_ROW_TOTAL_LABEL.search(c):
            y1 = y0
        if y2 is None and "开票人" in c:
            y2 = y0
    if y1 is not None and y2 is not None and y2 - y1 > 2:
        buf = []
        for _, y0, text in lines:
            if not (y1 + 1.0 < y0 < y2 - 1.0):
                continue
            c = _compact(text)
            # 方框里那一行可能就是「备注：」标签本身（内容为空），要去掉标签再判断
            if c.startswith("备注"):
                c = RE_ROW_REMARK.sub("", c, count=1)
            if len(c) < 2:         # 竖排的「备」「注」单字 / 空内容
                continue
            buf.append(c)
        if buf:
            return " ".join(buf)
    return ""


# ---------------------------------------------------------------- 核心解析
def _collect_lines(page, y_tol: float = 3.5):
    """
    把页面文本按「视觉行」聚合，返回 [(x0, y0, row_text), ...]

    做法：以 PyMuPDF 的 **line** 为最小单位（它已经把同一单元格里的
    「名称：」和它右边的值正确分在一起），再把**同一水平位置**的多个 line
    合并成一行 —— 典型情况就是「购买方 / 销售方」这种左右两栏。

    ⚠️ 为什么不拿 span 从头自己聚类（老做法，踩过一个真 bug）：
       数电票左侧的「购 买 方 信 息」是**竖排**的，每个字独立成行、基线各不相同。
       用 span 的 bbox 中心聚类时，竖排首字「购」的中心和右邻「名称：」的中心
       差了 3.8pt，刚好越过 3.5 的容差 →「名称：」被孤立成单独一行，
       和它的值「上海白泽…」被硬拆开。
       后果链：购买方取不到 → 退化到兜底策略 → 错取明细表头里的「项目名称」→
       文件名变成「单 价 数 量 金 额 税率/征收率 税 额—23.90.pdf」。
       （T3 出行的票就是这个版式；享道出行把标签和值放在同一个 span，所以没事。）
       PyMuPDF 的 line 分组本身是对的，没理由再自己造一遍轮子。
    """
    entries = []
    try:
        data = page.get_text("dict")
    except Exception:
        return []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [sp for sp in line.get("spans", [])
                     if sp.get("text", "").strip()]
            if not spans:
                continue
            x0 = min(sp["bbox"][0] for sp in spans)
            x1 = max(sp["bbox"][2] for sp in spans)
            y0 = min(sp["bbox"][1] for sp in spans)
            y1 = max(sp["bbox"][3] for sp in spans)
            text = "".join(sp["text"] for sp in spans)
            entries.append((x0, y0, x1, y1, (y0 + y1) / 2.0, text))
    if not entries:
        return []

    entries.sort(key=lambda e: (e[4], e[0]))

    # 按「行中心」聚类：同一水平位置的左右两栏会落进同一组
    groups, cur = [], [entries[0]]
    cur_y = entries[0][4]
    for e in entries[1:]:
        if abs(e[4] - cur_y) <= y_tol:
            cur.append(e)
        else:
            groups.append(cur)
            cur, cur_y = [e], e[4]
    groups.append(cur)

    out = []
    for row in groups:
        row.sort(key=lambda e: e[0])          # 组内按 x 从左到右
        parts, prev_x1 = [], None
        for x0, _y0, x1, _y1, _yc, text in row:
            if prev_x1 is not None and x0 - prev_x1 > 1.2:
                parts.append(" ")             # 单元格之间的空隙补一个空格
            parts.append(text)
            prev_x1 = x1 if prev_x1 is None else max(prev_x1, x1)
        text = "".join(parts)
        if not text.strip():
            continue
        out.append((min(e[0] for e in row), min(e[1] for e in row), text))
    out.sort(key=lambda t: (round(t[1], 1), t[0]))
    return out


def _needs_continue(name: str) -> bool:
    """判断名称是否被换行截断了。

    依据：企业名里的括号是成对的，「（…）」不会只出现一半。
    实测票面 `上海市浦东新区高行镇科凡维修技术服务中心（个体工商` 就是
    被换成两行的，只看当前视觉行会丢掉尾巴。
    """
    return (name.count("（") > name.count("）")
            or name.count("(") > name.count(")"))


def _continue_name(name: str, lines, y_after: float) -> str:
    """把紧随其后的视觉行里续上的名称补齐（长企业名换行）"""
    for _x, y1, t in lines:
        if y1 <= y_after + 0.5:
            continue
        if y1 > y_after + 30:          # 隔太远就不是同一段名称了
            break
        tail = _clean_name(_normalize(t))
        if tail:
            name += tail
        if not _needs_continue(name):
            break
    return name


def _names_from_lines(lines):
    """从文本行中抽取 (购买方, 销售方, 命中策略)"""
    candidates = []            # [(x0, y0, name)]
    for x0, y0, text in lines:
        t = _normalize(text)
        # ⚠️ 必须匹配「名称：」带冒号的**字段标签**，不能只找「名称」二字：
        #    明细表头里有一列叫「项目名称」，也含「名称」。
        #    踩过：T3 出行那张票的购买方没取到时，兜底策略命中了表头，
        #    于是购买方变成了「单  价 数  量 金  额 税率/征收率 税  额」，
        #    最终文件名荒唐到没法用。加个冒号就精准了 ——
        #    票面上的字段标签一律写作「名称：」，表头不会带冒号。
        positions = [m.start() for m in re.finditer(r"名称\s*[：:]", t)]
        if not positions:
            continue
        for i, pos in enumerate(positions):
            start = pos + 2
            end = positions[i + 1] if i + 1 < len(positions) else len(t)
            seg = _cut_at_stop(t[start:end])
            name = _clean_name(seg)
            if name and len(name) >= 2 and re.search(r"[\u4e00-\u9fa5A-Za-z]", name):
                if _needs_continue(name):
                    name = _continue_name(name, lines, y0)
                candidates.append((x0, y0, name))

    if not candidates:
        return "", "", ""

    # 去重（同一名称在同一行重复出现时只保留一次）
    seen, uniq = set(), []
    for x0, y0, name in candidates:
        key = (round(y0, 0), name)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((x0, y0, name))

    # 按「行」分组：同一行（y 相近）内的名称，最左为购买方，其次为销售方
    uniq.sort(key=lambda c: (round(c[1] / 6.0), c[0]))
    rows = {}
    for x0, y0, name in uniq:
        rows.setdefault(round(y0 / 6.0), []).append((x0, name))

    buyer = seller = ""
    for _, row in sorted(rows.items()):
        row.sort(key=lambda c: c[0])
        if len(row) >= 2:
            buyer, seller = row[0][1], row[1][1]
            return buyer, seller, "同行双名称"
    # 只有单个名称：取最靠左的一个作为购买方
    uniq.sort(key=lambda c: c[0])
    buyer = uniq[0][2]
    return buyer, "", "单名称"


def _names_from_words(page):
    """坐标兜底：找所有含「名称」的词，取其中最左侧的名称值"""
    try:
        words = page.get_text("words")
    except Exception:
        return "", ""
    cands = []
    for w in words:
        x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
        t = _normalize(txt)
        if "名称" not in t:
            continue
        tail = t.split("名称")[-1]
        tail = _clean_name(tail.lstrip("：:"))
        if tail and len(tail) >= 2:
            cands.append((x0, tail))
            continue
        # 名称与值分离：取同一行右侧的词
        same = [q for q in words
                if abs((q[1] + q[3]) / 2 - (y0 + y1) / 2) < 4 and q[0] > x1 - 1]
        same.sort(key=lambda q: q[0])
        buf = []
        for q in same:
            qt = _normalize(q[4])
            if any(s in qt for s in STOP_WORDS) or qt in ("名称", "购", "销"):
                break
            buf.append(qt)
            if len("".join(buf)) >= 4 and (buf[-1].endswith(("公司", "厂", "店", "部", "心", "人", "司", "户"))):
                break
        val = _clean_name("".join(buf))
        if val and len(val) >= 2:
            cands.append((x0, val))
    if not cands:
        return "", ""
    cands.sort(key=lambda c: c[0])
    return cands[0][1], (cands[1][1] if len(cands) > 1 else "")


def _extract_amount(full_text: str, lines):
    """金额提取：小写优先 → 价税合计行 → 全局货币符号"""
    m = RE_AMOUNT_LOWER.search(full_text)
    if m:
        v = _to_float(m.group(1))
        if v is not None:
            return v, m.group(1).strip(), "小写"

    # 「价税合计」所在行（可能跨行，取其后 2 行内）
    for i, (_, _, text) in enumerate(lines):
        t = _normalize(text)
        if "价税合计" in t or "小写" in t:
            window = " ".join(_normalize(lines[j][2])
                              for j in range(i, min(i + 3, len(lines))))
            m = RE_CURRENCY.search(window) or RE_AMOUNT_PLAIN.search(window)
            if m:
                v = _to_float(m.group(1))
                if v is not None:
                    return v, m.group(1).strip(), "价税合计行"

    # 全局兜底：所有 ¥ 金额里取绝对值最大的（通常是价税合计）
    vals = [(_to_float(x), x) for x in RE_CURRENCY.findall(full_text)]
    vals = [(v, x) for v, x in vals if v is not None]
    if vals:
        v, x = max(vals, key=lambda p: abs(p[0]))
        return v, x.strip(), "货币符号兜底"
    return None, "", ""


def _extract_misc(lines, full_text: str, file_name: str):
    """
    发票号码 / 开票日期。

    优先在**视觉行**里取（标签与值同一行，最可靠）；
    取不到再退回整段文本，最后退回文件名里的号码 / 时间戳。
    数电票版式里号码常被渲染成「26 3 1 2 0 0 0 0 …」这种带空格的样子，
    所以匹配到之后要先去掉空白再取连续数字。
    """
    no = ""
    for _, _, text in lines:
        c = _normalize(text)
        if "发票号码" not in c:
            continue
        m = re.search(r"发票号码[^\d]{0,10}([\d\s]{15,80})", c)
        if not m:
            continue
        digits = re.sub(r"\s+", "", m.group(1))
        m2 = re.search(r"\d{15,25}", digits)
        if m2:
            no = m2.group()
            break
    if not no:
        m = RE_INVOICE_NO.search(full_text)
        if m:
            no = m.group(1)
    if not no:
        m2 = re.search(r"发票号码[^\d]{0,10}([\d\s]{15,80})", full_text)
        if m2:
            digits = re.sub(r"\s+", "", m2.group(1))
            m3 = re.search(r"\d{15,25}", digits)
            if m3:
                no = m3.group()
    if not no:
        m = RE_FILE_NO.search(file_name) or RE_INVOICE_NO_FALLBACK.search(full_text)
        if m:
            no = m.group(1)

    date = ""
    for _, _, text in lines:
        c = _normalize(text)
        if "开票日期" not in c:
            continue
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", c)
        if m:
            date = "%04d%02d%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            break
    if not date:
        m = RE_DATE.search(full_text)
        if m:
            date = "%04d%02d%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if not date:
        m = RE_FILE_TS.search(file_name)
        if m:
            date = m.group(1)[:8]
    return no, date


# ---------------------------------------------------------------- 扩展字段
def _extract_extras(lines, full_text: str, buyer: str = "", amount=None):
    """
    扩展字段提取：发票类型/代码、购销方税号、不含税金额、税额、
    价税合计大写、备注、收款人、复核人、开票人。

    这些字段只服务于「命名规则」的勾选组合，原则是：
        · **能在视觉行里按位置定位的，就不靠整段文本的模糊正则猜**
          （真实数电票文本层是「先全部标签、再全部值」，跨行正则会抓错）
        · 提不到就留空 —— 猜错的税号/金额比空着危险得多
    """
    ex = {
        "inv_type": "", "inv_code": "", "buyer_tax": "", "seller_tax": "",
        "net": None, "tax": None, "amount_cn": "",
        "remark": "", "payee": "", "reviewer": "", "drawer": "",
    }

    # ---- 发票类型：电子发票（增值税专用发票）→「增值税专用发票」----
    m = RE_INVOICE_TYPE.search(full_text)
    if m:
        ex["inv_type"] = m.group(1).strip()

    # ---- 发票代码（数电票已取消，仅老版电子发票/纸质票面存在）----
    m = RE_INVOICE_CODE.search(full_text)
    if m:
        ex["inv_code"] = m.group(1)

    # ---- 购销方税号：在视觉行里按标签切左右两栏 ----
    # 关键：不能按「全文出现顺序取前两个税号」。
    # 购买方是「个人」时票面仍有两个标签，但第一栏后面是空的，
    # 按出现顺序会把销售方税号错配给购买方。切片保留空片段就能自然对上。
    tax_vals = []
    for _, _, text in lines:
        segs = _row_segments(text, RE_ROW_TAX_LABEL)
        if not segs:
            continue
        got = []
        for seg in segs:
            m = RE_TAX_VALUE.search(_compact(seg))
            got.append(m.group() if m else "")
        if any(got):
            tax_vals.extend(got)
            if len(tax_vals) >= 2:
                break
    if len(tax_vals) >= 2:
        ex["buyer_tax"], ex["seller_tax"] = tax_vals[0], tax_vals[1]
    elif tax_vals and tax_vals[0]:
        # 票面只出现一个带值的税号：购买方是个人的话，它属于销售方
        if "个人" in (buyer or ""):
            ex["seller_tax"] = tax_vals[0]
        else:
            ex["buyer_tax"] = tax_vals[0]
    if not ex["buyer_tax"] and not ex["seller_tax"]:
        # 视觉行里没找到 → 退回整段文本（老版式/罕见排版）
        segs = RE_TAX_LABEL.split(full_text)
        vals = []
        for seg in segs[1:3]:
            m = RE_TAX_VALUE.match(_compact(seg)[:24])
            vals.append(m.group() if m else "")
        if len(vals) >= 2:
            ex["buyer_tax"], ex["seller_tax"] = vals[0], vals[1]
        elif vals and vals[0]:
            if "个人" in (buyer or ""):
                ex["seller_tax"] = vals[0]
            else:
                ex["buyer_tax"] = vals[0]

    # ---- 不含税金额 / 税额：定位「合计」行（排除「价税合计」）----
    # 合计行里可能混着数量/单价等数字，用「两数之和 == 价税合计」做勾稽来挑。
    #
    # ⚠️ 要**上下都看**（2026-10-16 用户真实通行费票踩到）：
    #    有的版式把值放在标签**上一行** ——
    #        y=229.53 | ¥8.74 0.26        ← 值
    #        y=233.03 | 合  计             ← 标签
    #    只往下扫的话，捞到的第一个数是下面「价税合计」行的 9.00，
    #    于是 net 被填成 9.00、tax 空着 —— 同一张票 XML/OFD 是 8.74 + 0.26，
    #    PDF 却是 9.00，三种格式对不上，用户一眼就看出来了。
    for i, (_, _, text) in enumerate(lines):
        c = _compact(text)
        if not c.startswith("合计") or "价税" in c:
            continue

        cands = []
        for offset in (0, -1):
            j = i + offset
            if not (0 <= j < len(lines)):
                continue
            nums = []
            for k in range(j, min(j + 3, len(lines))):
                for v in RE_MONEY.findall(_normalize(lines[k][2])):
                    f = _to_float(v)
                    if f is not None:
                        nums.append(f)
            net, tax = _pick_net_tax(nums, amount)
            if net is not None:
                cands.append((net, tax))

        if cands:
            best = cands[0]
            for net, tax in cands:          # 优先挑勾稽得上的那一组
                if (tax is not None and amount is not None
                        and abs(net + tax - amount) <= 0.02):
                    best = (net, tax)
                    break
            ex["net"], ex["tax"] = best
        break

    # ---- 价税合计（大写）：同行取，且在「（小写）」之前截断 ----
    for _, _, text in lines:
        c = _compact(text)
        if not (RE_ROW_TOTAL_LABEL.search(c) and "大写" in c):
            continue
        head = RE_ROW_LOWER.split(c)[0]
        m = RE_CN_AMOUNT.search(head)
        if m:
            ex["amount_cn"] = m.group()
            break
    if not ex["amount_cn"]:
        # 退回整段文本：标签与值可能被拆到相邻两行
        m = RE_AMOUNT_CN.search(full_text)
        if m:
            ex["amount_cn"] = m.group(1)
        else:
            idx = full_text.find("大写")
            if idx >= 0:
                tail = full_text[idx: idx + 100]
                m = RE_CN_AMOUNT.search(tail)
                if m and "小写" not in tail[:m.start()]:
                    ex["amount_cn"] = m.group()

    # ---- 备注（同行 / 方框两种版式）----
    ex["remark"] = _extract_remark(lines)

    # ---- 人名类：开票人（数电票必有）、收款人 / 复核人（老版票才有）----
    # ⚠️ 必须同行取，否则会把发票号码当成人名（见 RE_ANY_PERSON_LABEL 处的注释）
    for key in ("drawer", "payee", "reviewer"):
        ex[key] = _extract_person(lines, key)

    return ex


# ---------------------------------------------------------------- 对外接口
def _try_trip(path):
    """
    试着把文件当行程单解析。

    单独包一层 try：行程单解析出任何岔子都不能影响发票解析这条主链路 ——
    宁可把它当「未识别」，也不能让一个附件把整批扫描搞挂。
    """
    try:
        return PC.parse_trip(path)
    except Exception:
        return None


def _missing_msg(miss) -> str:
    """
    「这张票读不到哪些字段」的统一文案（PDF / OFD / XML 三条路共用一份）。

    ⚠️ 措辞必须短（2026-10-16 用户报「状态显示不全」）：
       列表的「状态」列只有 126px，一行最多放 8 个汉字。
       原来写「未识别到：购买方名称、价税合计」——17 个字，要折 4 行，很难看；
       压成「未识别：购买方、价税合计」——12 个字，正好两行折得下。
       顺带把字段名也写成票面上的叫法（「购买方名称」→「购买方」）。
    """
    return "未识别：" + "、".join(miss)


# 加密 PDF 的密码：按绝对路径记在内存里。
# ⚠️ 只放内存、不落盘 —— 密码不属于发票数据，不该被写进缓存或配置文件。
PDF_PASSWORDS = {}


def _parse_pdf_pages(doc, path, page_idxs):
    """
    在**已经打开**的 doc 上，只用指定页解析出一张票。

    抽出来是为了两个用途共用同一套字段提取：
      · 正常解析：取前 3 页（数电票一般 1~2 页）
      · 多票检测：一页一页单独解析，看这一页是不是一张独立的票
    两边必须完全一致，否则会出现「整份读得出、拆开读不出」的怪事。
    """
    info = InvoiceInfo(path)
    pages_text, lines = [], []
    for i in page_idxs:
        page = doc[i]
        pages_text.append(page.get_text("text"))
        lines.extend(_collect_lines(page))
    lines.sort(key=lambda t: (round(t[1], 1), t[0]))
    full_text = _normalize("\n".join(pages_text))

    if len(full_text.strip()) < 20:
        info.message = "未读取到文本层（可能是图片/扫描件），需 OCR"
        return info

    info.raw_text = full_text

    buyer, seller, engine = _names_from_lines(lines)
    if not buyer:
        buyer, seller = _names_from_words(doc[page_idxs[0]])
        engine = engine or "坐标兜底"

    amount, amount_text, aengine = _extract_amount(full_text, lines)
    info.invoice_no, info.date = _extract_misc(lines, full_text, info.file_name)

    info.buyer = buyer
    info.seller = seller
    info.amount = amount
    info.amount_text = amount_text
    info.is_red = amount is not None and amount < 0
    info.engine = f"{engine}|{aengine}" if (engine or aengine) else ""

    # 扩展字段（发票类型 / 税号 / 不含税金额 / 税额 / 大写 / 开票人 …）
    # amount 传进去是为了给「合计」行的数字做勾稽，挑出真正的金额与税额
    for k, v in _extract_extras(lines, full_text, buyer, amount).items():
        setattr(info, k, v)

    if buyer and amount is not None:
        info.ok = True
    return info


def detect_multi(doc, path, max_pages=24):
    """
    判断这个 PDF 里是不是塞了**多张发票**。

    客户把几张票合并成一个 PDF 发过来是常事，老版本只把前 3 页的文字拼在一起
    当成一张票读 —— 第 2 张以后的信息全是错的，而且不报错。

    做法：每一页单独解析，按票号把**连续的页**归成一组。
      · 某页没有票号（清单页 / 附件页）→ 归到上一组，不另算一张
      · 只有一组，或者所有组的票号都相同 → 当成一张票，返回 None
    返回 {"count": n, "groups": [{"pages": [...], "no":…, "buyer":…, "amount":…}]}

    ⚠️ 只有 page_count >= 2 才跑；单页票（绝大多数）零额外开销。
    """
    try:
        n = min(doc.page_count, max_pages)
        if n < 2:
            return None
        groups = []
        for i in range(n):
            pi = _parse_pdf_pages(doc, path, [i])
            key = (pi.invoice_no or "").strip()
            if not key:
                if groups:
                    groups[-1]["pages"].append(i)
                else:
                    groups.append({"pages": [i], "no": "", "buyer": pi.buyer,
                                   "amount": pi.amount})
                continue
            if groups and groups[-1]["no"] == key:
                groups[-1]["pages"].append(i)
            else:
                groups.append({"pages": [i], "no": key, "buyer": pi.buyer,
                               "amount": pi.amount})
        if len(groups) <= 1:
            return None
        nos = {g["no"] for g in groups if g["no"]}
        if len(nos) <= 1:
            return None
        return {"count": len(groups), "groups": groups}
    except Exception:
        return None


def split_pdf_by_groups(path, groups, out_dir=None):
    """
    按页组把一个大 PDF 拆成多个文件。

    命名用「原名_第N张.pdf」——**故意不套命名规则**：
    拆出来的文件还要重新走一遍解析+改名流程，这里套规则会算两遍、容易对不上。
    返回 (新文件列表, 错误信息)。
    """
    made = []
    try:
        doc = _fitz().open(path)
    except Exception as exc:
        return [], "无法打开 PDF：%s" % exc
    try:
        if doc.needs_pass:
            pwd = (PDF_PASSWORDS.get(os.path.abspath(path))
                   or PDF_PASSWORDS.get(path) or "")
            if not pwd or not doc.authenticate(pwd):
                return [], "PDF 已加密，请先输入密码"
        folder = out_dir or os.path.dirname(os.path.abspath(path))
        base = os.path.splitext(os.path.basename(path))[0]
        for k, g in enumerate(groups, 1):
            pages = [p for p in g.get("pages", []) if 0 <= p < doc.page_count]
            if not pages:
                continue
            new = _fitz().open()
            try:
                for p in pages:
                    new.insert_pdf(doc, from_page=p, to_page=p)
                dst = os.path.join(folder, "%s_第%d张.pdf" % (base, k))
                n = 1
                while os.path.exists(dst):
                    n += 1
                    dst = os.path.join(folder, "%s_第%d张(%d).pdf" % (base, k, n))
                new.save(dst)
                made.append(dst)
            finally:
                try:
                    new.close()
                except Exception:
                    pass
    except Exception as exc:
        return made, "拆分失败：%s" % exc
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return made, ""


def parse_pdf(path: str) -> InvoiceInfo:
    """解析单个 PDF，返回 InvoiceInfo"""
    try:
        doc = _fitz().open(path)
    except Exception as exc:
        info = InvoiceInfo(path)
        info.message = "无法打开 PDF：%s" % exc
        return info

    try:
        if doc.needs_pass:
            pwd = (PDF_PASSWORDS.get(os.path.abspath(path))
                   or PDF_PASSWORDS.get(path) or "")
            if not pwd or not doc.authenticate(pwd):
                info = InvoiceInfo(path)
                info.message = "已加密，需要密码"
                info.need_password = True
                return info

        info = _parse_pdf_pages(doc, path, list(range(min(3, doc.page_count))))

        if info.ok:
            # 一张票读出来了 —— 再看这个文件里是不是还塞着别的票
            info.multi = detect_multi(doc, path)
            return info

        # 不是发票 —— 先看看是不是「行程单」。
        # 行程单是网约车/酒店报销的**配套附件**，不是坏文件。
        # 老版本一律标成「未识别」，用户看到一片红会以为文件坏了。
        trip = _try_trip(path)
        if trip:
            info.kind = "trip"
            info.trip = trip
            info.ok = True                  # 识别成功了，只不过它本身不是发票
            info.message = ""
            info.buyer = trip.get("platform") or "行程单"
            info.seller = ""
            info.amount = trip.get("total")
            info.amount_text = ("%.2f" % trip["total"]
                                if trip.get("total") is not None else "")
            info.invoice_no = ""
            info.date = (trip.get("date_to") or "").replace("-", "")
            info.engine = "行程单"
            return info

        miss = []
        if not info.buyer:
            miss.append("购买方")
        if info.amount is None:
            miss.append("价税合计")
        info.message = _missing_msg(miss)
        return info
    except Exception as exc:
        info = InvoiceInfo(path)
        info.message = "解析异常：%s" % exc
        return info
    finally:
        try:
            doc.close()
        except Exception:
            pass


# ---------------------------------------------------------------- OFD（zip + XML）
# OFD 本身就是个 zip 包。数电票 OFD 里有一份 `Tags/CustomTag.xml`，
# 存放「语义字段名 → 对象 ID」的映射，例如：
#     <ofd:Buyer><ofd:BuyerName><ofd:ObjectRef>6924</ofd:ObjectRef></ofd:BuyerName>
# 拿着 6924 去 Pages/*/Content.xml 里找 <ofd:TextObject ID="6924"> 的文本即可。
# —— 不用猜版式、不会认错，比 PDF 稳得多。
OFD_NS = "http://www.ofdspec.org/2016"
_O = "{%s}" % OFD_NS


def _zip_read(zf, name):
    """按路径从包里读文本；路径大小写不敏感（各生成器写法不一致）"""
    if not name:
        return None
    want = name.replace("\\", "/").lstrip("/").lower()
    for n in zf.namelist():
        if n.replace("\\", "/").lstrip("/").lower() == want:
            try:
                return zf.read(n).decode("utf-8", "replace")
            except Exception:
                return None
    return None


def _ofd_text_objects(root):
    """
    收集 Content.xml 里的所有文本对象 → [(对象ID, 文本, x, y)]（保持文档顺序）。

    Boundary="x y w h" 是对象在图页上的位置，用它来定位「备注框」这种
    没有语义标签的区域。坐标解析失败就记 0，不影响主流程。
    """
    out = []
    for to in root.iter(_O + "TextObject"):
        tc = to.find(_O + "TextCode")
        txt = (tc.text or "") if tc is not None else ""
        b = (to.get("Boundary") or "").split()
        try:
            x, y = float(b[0]), float(b[1])
        except (ValueError, IndexError):
            x = y = 0.0
        out.append((to.get("ID") or "", txt, x, y))
    return out


def _ofd_amount(page_texts, cmap, names, used):
    """
    金额取值：一个语义名可能对应多个对象（「¥」符号一个、数字一个），
    所以按名字逐个试探，取第一个**能解析成数字**的，顺手把这些对象标记为已用。
    """
    for name in names:
        for oid in cmap.get(name, []):
            v = (page_texts.get(oid) or "").strip()
            f = _to_float(v)
            if f is not None:
                used.add(oid)
                return f
    return None


# ------------------------------------------------------- OFD 坐标回退（没有语义标签时）
# 背景：标准数电票 OFD 里有一份 Tags/CustomTag.xml，写着「语义字段名 → 对象 ID」，
# 按名字取就行、又准又省事。但**由 PDF 硬转出来的 OFD**（实测：
# Suwell PDF2OFD Creator 生成的「附件打包」里的 .ofd）**没有这份映射** ——
# 包里只有排版数据，于是语义路一个字段都取不到，整张票被判
# 「未识别到：购买方名称、价税合计」，而同一张票的 PDF / XML 都能正常识别。
#
# 这种 OFD 得按**坐标**把版式还原出来，再套用 PDF 那套字段提取逻辑。
# 坐标从哪来（两处，实测是同一个坐标系）：
#   · 标签（发票号码：/ 名称：/ 价税合计…）在 PageBlock **外面**，用 Boundary 的 x/y；
#   · 字段值（公司名 / 税号 / 金额…）在 <ofd:PageBlock> **里面**，它们的 Boundary
#     是**整页框**（0 0 210 297），位置毫无意义 —— 真正的坐标写在
#     <ofd:TextCode X="…" Y="…"> 上。
#   验证：标签左缘 + 标签宽 ≈ 值的 X（税号那两行误差 0，公司名 < 1.3）。
def _ofd_text_objects_px(root, pw=None, ph=None):
    """
    取文本对象并给出**可用的**位置 → [(x, y, 文本)]（x 为左缘、y 约为文字底线）。

    Boundary 是整页框时改用 TextCode 的 X/Y —— 但**只在那种情况下**改：
    标准 OFD 的 TextCode X/Y 是相对对象自身的，无条件拿来当页面坐标会算错。
    """
    items = []
    for to in root.iter(_O + "TextObject"):
        txt, tx, ty = "", None, None
        for tc in to.findall(_O + "TextCode"):
            t = tc.text or ""
            if t.strip():
                txt = t
                try:
                    tx, ty = float(tc.get("X")), float(tc.get("Y"))
                except (TypeError, ValueError):
                    tx = ty = None
                break
        if not txt.strip():
            continue

        b = (to.get("Boundary") or "").split()
        try:
            bx, by, bw, bh = (float(v) for v in b[:4])
        except (ValueError, IndexError):
            bx = by = bw = bh = None

        box_is_page = (
            bw is not None and bh is not None and bw >= 100 and bh >= 100
            and (pw is None or bw >= pw * 0.9)
            and (ph is None or bh >= ph * 0.9))

        if box_is_page and tx is not None:
            items.append((tx, ty if ty is not None else 0.0, txt))
        elif bx is not None:
            # 标签：Boundary 是文本框**顶端**，加个高度当底线，好和值对齐到同一行
            items.append((bx, by + (bh or 0.0), txt))
    return items


def _ofd_drop_vertical(items):
    """
    丢掉**竖排表头**的单字对象，避免污染行文本。

    数电票左侧「购 买 方 信 息」「销 售 方 信 息」和备注框的「备 注」，在 OFD 里是
    **一个字一个 TextObject**，同一个 x、y 逐字递增。它们横向拼进行里会粘在标签上：
      实测购买方名称会变成「灏芮库（上海）干冰设备有限公司**售**」、
      备注会变成「**备**沪AB93659 一型货车…」。
    判据：同一个 x（±0.1）上有 ≥2 个**单字汉字**、且相邻 y 间距 ≤ 6
    （一个字高约 2.8，竖排字距约 3.3）。间距上限顺便把明细表里上下相邻的
    同列值排除掉 —— 那些行距在 10 以上，不会误伤。
    """
    by_x = {}
    for i, (_x, _y, text) in enumerate(items):
        if len(text) == 1 and RE_CJK_CHAR.match(text):
            by_x.setdefault(round(_x, 1), []).append(i)
    drop = set()
    for idxs in by_x.values():
        idxs.sort(key=lambda i: items[i][1])
        run = [idxs[0]]
        for i in idxs[1:]:
            if items[i][1] - items[run[-1]][1] <= 6.0:
                run.append(i)
            else:
                if len(run) >= 2:
                    drop.update(run)
                run = [i]
        if len(run) >= 2:
            drop.update(run)
    return [it for i, it in enumerate(items) if i not in drop]


def _ofd_geometry_lines(items, tol=3.0):
    """按 y 聚成「视觉行」→ [(x0, y0, 整行文字)]（行内按 x 排序后拼接）"""
    rows = []
    for x, y, text in sorted(items, key=lambda t: (t[1], t[0])):
        for r in rows:
            if abs(r["y"] - y) <= tol:
                r["cells"].append((x, text))
                if y < r["y"]:
                    r["y"] = y
                break
        else:
            rows.append({"y": y, "cells": [(x, text)]})
    out = []
    for r in rows:
        r["cells"].sort(key=lambda c: c[0])
        out.append((r["cells"][0][0], r["y"],
                    "".join(c[1] for c in r["cells"])))
    return out


def _ofd_recover_by_geometry(zf, page_locs, file_name):
    """
    按坐标还原版式并提取字段，返回 dict；取不到东西返回 None。

    还原出来的 lines 结构（x0, y0, 文本）与 PDF 路径**完全一致**，
    所以下游直接复用 _names_from_lines / _extract_amount / _extract_misc /
    _extract_extras —— 一套逻辑两种输入，不会两边跑偏。
    """
    items = []
    for loc in page_locs:
        s = _zip_read(zf, loc)
        if not s:
            continue
        try:
            root = ET.fromstring(s)
        except ET.ParseError:
            continue
        pw = ph = None
        for el in root.iter(_O + "PhysicalBox"):
            try:
                _a, _b, w, h = (float(v) for v in (el.text or "").split()[:4])
                pw, ph = w, h
            except (ValueError, TypeError):
                pass
            break
        items += _ofd_text_objects_px(root, pw, ph)

    if not items:
        return None
    lines = _ofd_geometry_lines(_ofd_drop_vertical(items))
    if not lines:
        return None

    full_text = _normalize("\n".join(t for _x, _y, t in lines))
    buyer, seller, _eng = _names_from_lines(lines)
    amount, amount_text, _aeng = _extract_amount(full_text, lines)
    invoice_no, date = _extract_misc(lines, full_text, file_name)

    out = {
        "buyer": buyer, "seller": seller,
        "amount": amount, "amount_text": amount_text,
        "invoice_no": invoice_no, "date": date,
        "raw_text": full_text,
    }
    out.update(_extract_extras(lines, full_text, buyer, amount))
    return out


def parse_ofd(path: str) -> InvoiceInfo:
    """解析 OFD 格式的数电票"""
    info = InvoiceInfo(path)
    info.fmt = "OFD"
    try:
        zf = zipfile.ZipFile(path)
    except Exception as exc:
        info.message = "无法打开 OFD（不是有效的 zip 包）：%s" % exc
        return info

    try:
        # ① OFD.xml → 文档根（DocRoot），它给出 Document.xml 的位置
        docroot = ""
        x = _zip_read(zf, "OFD.xml")
        if x:
            try:
                for el in ET.fromstring(x).iter(_O + "DocRoot"):
                    docroot = (el.text or "").strip()
                    if docroot:
                        break
            except ET.ParseError:
                pass
        if not docroot:
            # 兜底：包里直接翻 Document.xml
            cands = [n for n in zf.namelist()
                     if posixpath.basename(n).lower() == "document.xml"]
            docroot = cands[0] if cands else ""
        if not docroot:
            info.message = "OFD 里找不到 Document.xml"
            return info
        base = posixpath.dirname(docroot)

        # ② Document.xml → 页面、模板、自定义标签的位置
        page_locs, tpl_locs, tag_loc = [], [], ""
        x = _zip_read(zf, docroot)
        if x:
            try:
                root = ET.fromstring(x)
                for el in root.iter(_O + "Page"):
                    b = (el.get("BaseLoc") or "").strip()
                    if b:
                        page_locs.append(posixpath.join(base, b))
                for el in root.iter(_O + "TemplatePage"):
                    b = (el.get("BaseLoc") or "").strip()
                    if b:
                        tpl_locs.append(posixpath.join(base, b))
                for el in root.iter(_O + "CustomTags"):
                    if (el.text or "").strip():
                        tag_loc = posixpath.join(base, el.text.strip())
            except ET.ParseError:
                pass

        # ③ CustomTags.xml → CustomTag.xml（真正的语义映射表）
        if tag_loc:
            cx = _zip_read(zf, tag_loc)
            if cx:
                try:
                    for el in ET.fromstring(cx).iter(_O + "FileLoc"):
                        if (el.text or "").strip():
                            tag_loc = posixpath.join(
                                posixpath.dirname(tag_loc), el.text.strip())
                            break
                except ET.ParseError:
                    pass
        cmap = {}
        if tag_loc:
            tx = _zip_read(zf, tag_loc)
            if tx:
                try:
                    def walk(el, prefix=""):
                        for c in el:
                            name = c.tag.rsplit("}", 1)[-1]
                            refs = [(o.text or "").strip()
                                    for o in c.findall(_O + "ObjectRef")]
                            refs = [r for r in refs if r]
                            if refs:
                                cmap.setdefault(prefix + name, []).extend(refs)
                            else:
                                walk(c, prefix + name + "/")
                    walk(ET.fromstring(tx))
                except ET.ParseError:
                    pass

        # ④ 页面/模板里的文本对象 → {对象ID: 文本}
        page_items, tpl_items = [], []
        for loc in page_locs:
            s = _zip_read(zf, loc)
            if s:
                try:
                    page_items += _ofd_text_objects(ET.fromstring(s))
                except ET.ParseError:
                    pass
        for loc in tpl_locs:
            s = _zip_read(zf, loc)
            if s:
                try:
                    tpl_items += _ofd_text_objects(ET.fromstring(s))
                except ET.ParseError:
                    pass

        page_texts = {}
        for oid, txt, _x, _y in page_items + tpl_items:
            if oid and txt.strip():
                page_texts.setdefault(oid, txt)

        info.raw_text = "\n".join(t for _o, t, _x, _y in page_items + tpl_items
                                  if t.strip())

        used = set()

        def pick(names):
            """按语义名取文本：逐个名字试，取第一个非空的，标记为已用"""
            for name in names:
                for oid in cmap.get(name, []):
                    v = (page_texts.get(oid) or "").strip()
                    if v:
                        used.add(oid)
                        return v
            return ""

        # ---- 13 个字段 ----
        info.invoice_no = pick(["InvoiceNo", "InvoiceNumber"])
        info.date = _date_from_text(pick(["IssueDate", "BillingDate"]))
        info.buyer = pick(["Buyer/BuyerName", "BuyerName"])
        info.buyer_tax = pick(["Buyer/BuyerTaxID", "BuyerTaxID",
                               "Buyer/BuyerIdNum", "BuyerIdNum"])
        info.seller = pick(["Seller/SellerName", "SellerName"])
        info.seller_tax = pick(["Seller/SellerTaxID", "SellerTaxID",
                                "Seller/SellerIdNum", "SellerIdNum"])
        info.net = _ofd_amount(page_texts, cmap,
                               ["TaxExclusiveTotalAmount", "TotalAmWithoutTax"], used)
        info.tax = _ofd_amount(page_texts, cmap,
                               ["TaxTotalAmount", "TotalTaxAm"], used)
        info.amount = _ofd_amount(page_texts, cmap,
                                  ["TaxInclusiveTotalAmount",
                                   "TotalTax-includedAmount"], used)
        info.amount_text = ("%.2f" % info.amount) if info.amount is not None else ""
        info.drawer = pick(["InvoiceClerk", "InvoiceDrawer", "Drawer"])
        info.inv_code = pick(["InvoiceCode"])

        # 发票类型：模板里的标题「电子发票（…）」
        for _oid, t, _x, _y in tpl_items + page_items:
            m = RE_INVOICE_TYPE.search(t)
            if m:
                info.inv_type = m.group(1).strip()
                break

        # 价税合计（大写）：这份映射表里**没有**对应条目，
        # 但它在票面上是唯一的中文大写金额串，按特征识别即可。
        for oid, t, _x, _y in page_items:
            m = RE_CN_AMOUNT.search(_compact(t))
            if m:
                info.amount_cn = m.group()
                used.add(oid)
                break

        # 备注：也没有语义条目。用坐标框定 ——
        # 模板里「价税合计（大写）」标签的 y 与「开票人」标签的 y 之间就是备注框。
        info.remark = _ofd_remark(page_items, tpl_items, used)

        # ⑤ 语义标签没取全 → 坐标回退。
        # 有些 OFD 是「PDF 硬转」出来的（见 _ofd_text_objects_px 上方的注释），
        # 包里没有 Tags/CustomTag.xml，语义路一个字段都取不到。
        # 这时按坐标还原版式，缺哪个字段补哪个 —— 不覆盖语义路已经取到的值。
        if not info.buyer or info.amount is None:
            geo = _ofd_recover_by_geometry(zf, page_locs, info.file_name)
            if geo:
                if not info.buyer:
                    info.buyer = geo.get("buyer") or ""
                if not info.seller:
                    info.seller = geo.get("seller") or ""
                if info.amount is None:
                    info.amount = geo.get("amount")
                    info.amount_text = geo.get("amount_text") or ""
                if not info.invoice_no:
                    info.invoice_no = geo.get("invoice_no") or ""
                if not info.date:
                    info.date = geo.get("date") or ""
                if info.net is None:
                    info.net = geo.get("net")
                if info.tax is None:
                    info.tax = geo.get("tax")
                for key in ("buyer_tax", "seller_tax", "amount_cn", "remark",
                            "drawer", "payee", "reviewer", "inv_type",
                            "inv_code"):
                    if not getattr(info, key, "") and geo.get(key):
                        setattr(info, key, geo[key])
                if not info.raw_text:
                    info.raw_text = geo.get("raw_text") or ""
                if info.buyer and info.amount is not None:
                    info.engine = "OFD坐标回退"

        if not info.date:                       # 兜底：从文件名取时间戳
            m = RE_FILE_TS.search(info.file_name)
            if m:
                info.date = m.group(1)[:8]
        if info.amount is not None and info.amount < 0:
            info.is_red = True

        if info.buyer and info.amount is not None:
            info.ok = True
            if not info.engine:
                info.engine = "OFD语义标签"
        else:
            miss = []
            if not info.buyer:
                miss.append("购买方")
            if info.amount is None:
                miss.append("价税合计")
            info.message = _missing_msg(miss)
            if not info.engine:
                info.engine = "OFD语义标签"
        return info
    except Exception as exc:
        info.message = "解析异常：%s" % exc
        return info
    finally:
        try:
            zf.close()
        except Exception:
            pass


def _ofd_remark(page_items, tpl_items, used) -> str:
    """
    OFD 的备注没有语义标签，只能按坐标框定：
    模板里「价税合计（大写）」与「开票人」两个标签的 y 之间就是备注框。
    已经被别的字段用掉的对象（used）要排除，否则会把大写金额当成备注。
    """
    y_total = y_drawer = None
    for _oid, t, _x, y in tpl_items:
        c = _compact(t)
        if y_total is None and RE_ROW_TOTAL_LABEL.search(c) and "大写" in c:
            y_total = y
        if y_drawer is None and "开票人" in c:
            y_drawer = y
    if y_total is None or y_drawer is None or y_drawer - y_total < 2:
        return ""
    buf = []
    for oid, t, _x, y in page_items:
        if oid in used:
            continue
        if not (y_total + 1.0 < y < y_drawer - 1.0):
            continue
        c = _compact(t)
        if len(c) >= 2 and c not in buf:
            buf.append(c)
    return " ".join(buf)


# ---------------------------------------------------------------- XML（数电票报文）
# 电子税务局下载的 .xml（或 .zip 里那个 xml）是**完全结构化**的报文，
# 取字段就是按标签读，最省事也最准。目前报文没有命名空间，
# 但标准换版可能加上，所以按「本地名」匹配、忽略命名空间与大小写。
def _xall(root, name):
    """按本地名找元素（忽略命名空间与大小写）"""
    want = name.lower()
    out = []
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.rsplit("}", 1)[-1].lower() == want:
            out.append(el)
    return out


def _xtext(root, *path) -> str:
    """按路径逐层找，返回第一个非空文本；任一层找不到就返回空串"""
    cur = [root]
    for name in path:
        nxt = []
        for node in cur:
            nxt.extend(_xall(node, name))
        cur = nxt
        if not cur:
            return ""
    for el in cur:
        t = (el.text or "").strip()
        if t:
            return t
    return ""


def parse_xml(path: str) -> InvoiceInfo:
    """解析数电票 XML 报文"""
    info = InvoiceInfo(path)
    info.fmt = "XML"
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        info.message = "无法读取 XML：%s" % exc
        return info
    return _parse_xml_bytes(raw, info, path)


def _parse_xml_bytes(raw: bytes, info: InvoiceInfo, path: str) -> InvoiceInfo:
    """真正的解析逻辑（.xml 文件与 .zip 里的 xml 共用同一条路径）"""
    try:
        root = ET.fromstring(raw.decode("utf-8", "replace"))
    except ET.ParseError as exc:
        info.message = "XML 格式错误：%s" % exc
        return info

    info.raw_text = raw.decode("utf-8", "replace")[:4000]

    # ---- 发票号码：报文头与税务监管信息里各有一份，取到就停 ----
    info.invoice_no = (_xtext(root, "TaxSupervisionInfo", "InvoiceNumber")
                       or _xtext(root, "EIid"))
    # ---- 开票日期 ----
    # IssueTime 是开票日期；RequestTime 是开票请求时间，只在前者缺失时兜底
    info.date = (_date_from_text(_xtext(root, "TaxSupervisionInfo", "IssueTime"))
                 or _date_from_text(_xtext(root, "BasicInformation", "RequestTime")))
    # ---- 发票类型：票种（增值税专用发票 / 普通发票）----
    info.inv_type = (_xtext(root, "InherentLabel", "GeneralOrSpecialVAT", "LabelName")
                     or _xtext(root, "GeneralOrSpecialVAT", "LabelName"))
    # ---- 购销方 ----
    info.buyer = (_xtext(root, "BuyerInformation", "BuyerName")
                  or _xtext(root, "BuyerName"))
    info.buyer_tax = (_xtext(root, "BuyerInformation", "BuyerIdNum")
                      or _xtext(root, "BuyerIdNum"))
    info.seller = (_xtext(root, "SellerInformation", "SellerName")
                   or _xtext(root, "SellerName"))
    info.seller_tax = (_xtext(root, "SellerInformation", "SellerIdNum")
                       or _xtext(root, "SellerIdNum"))
    # ---- 金额三兄弟 ----
    info.net = _to_float(_xtext(root, "BasicInformation", "TotalAmWithoutTax"))
    info.tax = _to_float(_xtext(root, "BasicInformation", "TotalTaxAm"))
    info.amount = _to_float(
        _xtext(root, "BasicInformation", "TotalTax-includedAmount"))
    info.amount_text = ("%.2f" % info.amount) if info.amount is not None else ""
    info.amount_cn = _xtext(root, "BasicInformation",
                            "TotalTax-includedAmountInChinese")
    # ---- 开票人 ----
    info.drawer = (_xtext(root, "BasicInformation", "Drawer")
                   or _xtext(root, "Drawer"))
    # ---- 发票代码（数电票已取消，老版式报文才有）----
    info.inv_code = _xtext(root, "InvoiceCode")

    # ---- 备注：放在 AdditionalInformation 里，标签名各版本不太一致 ----
    #
    # ⚠️ 要跟另外两种格式**对齐**：XML 报文里的备注是带空格的（「沪AB93659 一型货车 …」），
    #    而 PDF / OFD 是从票面文本层拼出来的、空格早被压掉了。
    #    不对齐的后果：同一个「备注」字段，XML 出的文件名带空格、PDF 出的不带 ——
    #    用户拿三种格式各跑一遍会得到不同的文件名（2026-10-16 实测发现）。
    #    所以这里统一压掉空白，跟 _compact 的口径一致。
    for name in ("Remark", "Remarks", "RemarkInfo", "AdditionalRemarks"):
        v = _xtext(root, "AdditionalInformation", name) or _xtext(root, name)
        if v:
            info.remark = _compact(v)
            break

    # ---- 红字判定：InIssuType 的 LabelCode「Y=蓝字 / N=红字」----
    issu = _xtext(root, "InInherentLabel", "InIssuType", "LabelCode") \
        or _xtext(root, "InIssuType", "LabelCode")
    if issu.upper() == "N":
        info.is_red = True
    if info.amount is not None and info.amount < 0:
        info.is_red = True
    if info.is_red and info.inv_type and "红字" not in info.inv_type:
        info.inv_type += "·红字"

    # ---- 明细兜底：合计金额缺失时用明细行加总 ----
    if info.net is None or info.tax is None:
        amts = [_to_float(e.text) for e in _xall(root, "Amount")]
        txs = [_to_float(e.text) for e in _xall(root, "ComTaxAm")]
        amts = [a for a in amts if a is not None]
        txs = [t for t in txs if t is not None]
        if info.net is None and amts:
            info.net = round(sum(amts), 2)
        if info.tax is None and txs:
            info.tax = round(sum(txs), 2)
    if info.amount is None and info.net is not None and info.tax is not None:
        info.amount = round(info.net + info.tax, 2)
        info.amount_text = "%.2f" % info.amount
    if not info.date:
        m = RE_FILE_TS.search(info.file_name)
        if m:
            info.date = m.group(1)[:8]

    if info.buyer and info.amount is not None:
        info.ok = True
        info.engine = "XML结构化"
    else:
        miss = []
        if not info.buyer:
            miss.append("购买方")
        if info.amount is None:
            miss.append("价税合计")
        info.message = _missing_msg(miss)
        info.engine = "XML结构化"
    return info


# ---------------------------------------------------------------- 统一入口
def _walk_invoice_files(folder: str, recursive: bool = False):
    """列出文件夹里所有可解析的发票文件（按名字排序，保证每次顺序一致）"""
    files = []
    exts = INVOICE_EXTS
    for root, _dirs, names in (
            os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]):
        for n in names:
            if n.startswith("~$"):
                continue
            if n.lower().endswith(exts):
                p = os.path.join(root, n)
                if os.path.isfile(p):
                    files.append(p)
    files.sort()
    return files


_CACHE_SKIP = {"raw_text"}      # 全文太长、又没人读，不进缓存


def info_to_dict(info: InvoiceInfo):
    """把 InvoiceInfo 压成能 JSON 的字典（缓存用）"""
    d = {}
    for f in InvoiceInfo.__slots__:
        if f in _CACHE_SKIP:
            continue
        v = getattr(info, f, None)
        if v is None or isinstance(v, (str, int, float, bool, dict, list)):
            d[f] = v
        # 其它类型（理论上没有）直接丢：宁可不缓存，也不能把缓存文件写坏
    return d


def info_from_dict(path: str, d) -> InvoiceInfo:
    """从缓存字典还原 InvoiceInfo（path / 文件名一律以**当前**为准）"""
    info = InvoiceInfo(path)
    for k, v in (d or {}).items():
        if k in InvoiceInfo.__slots__ and k not in _CACHE_SKIP:
            try:
                setattr(info, k, v)
            except Exception:
                pass
    info.path = path
    info.file_name = os.path.basename(path)
    info.fmt = EXT_LABEL.get(os.path.splitext(path)[1].lower(), "")
    return info


def parse_invoice(path: str, use_cache: bool = True) -> InvoiceInfo:
    """
    解析单个文件（PDF / OFD / XML / 行程单）。

    v2.6 起带**缓存**：同样的文件（路径+大小+修改时间都没变）直接复用上次结果，
    几百张票的文件夹第二次打开基本是瞬开。
    测试里可以传 use_cache=False，避免旧结果盖住刚改的逻辑。
    """
    if use_cache:
        hit = CACHE.get(path)
        if hit:
            try:
                return info_from_dict(path, hit)
            except Exception:
                pass

    info = _parse_invoice_uncached(path)

    if use_cache:
        try:
            CACHE.put(path, info_to_dict(info))
        except Exception:
            pass
    return info


def _parse_invoice_uncached(path: str) -> InvoiceInfo:
    """
    按扩展名分发到对应的解析器。**所有调用方都应走 parse_invoice**，
    不要在别处再写 endswith(".pdf") —— 加格式时漏改过。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".ofd":
        return parse_ofd(path)
    if ext == ".xml":
        return parse_xml(path)
    if ext == ".pdf":
        return parse_pdf(path)
    info = InvoiceInfo(path)
    info.message = "不支持的文件格式：%s（只认 %s）" % (ext or "无扩展名",
                                                      " / ".join(INVOICE_EXTS))
    return info


def parse_folder(folder: str, recursive: bool = False, progress=None, workers=None,
                 on_item=None):
    """
    批量解析文件夹内的发票（PDF / OFD / XML）。

    v2.6：改成**线程池并发**。
      · 为什么是线程不是进程：耗时的部分主要是 PyMuPDF 的 C 代码（会释放 GIL）
        和磁盘 IO，线程池就够用；进程池在 PyInstaller 打包后还要额外处理
        freeze_support 和对象序列化，为了这点收益不值得冒「打包后起不来」的风险。
      · 结果顺序**仍然按文件名顺序**返回，界面不会因为哪个先解析完就跳位置。
      · 文件数很少（<=3）时走串行，省掉线程池的启动开销。

    v2.7 新增 `on_item(index, item, total)`：每读完一张就回调一次。
      bridge 用它做「流式显示」—— 几百张票时不用整批读完才刷屏。
      ⚠️ 回调在**解析线程**里执行，实现方自己加锁，别在里面做重活。
    """
    try:
        files = _walk_invoice_files(folder, recursive)
    except OSError:
        files = []

    total = len(files)
    if not total:
        return []

    if workers is None:
        workers = min(8, max(2, (os.cpu_count() or 4)))

    out = [None] * total

    # 实测（210 个 PDF）：串行 2006ms → 8 线程 605ms，约 3.3 倍。
    # 但文件少的时候线程池反而是负担（7 个文件：串行 19ms、线程池 71ms，
    # 差的是线程启动/调度的固定开销），所以 16 个以下走串行。
    if total < 16 or workers <= 1:
        for i, p in enumerate(files):
            out[i] = parse_invoice(p)
            if on_item:
                on_item(i, out[i], total)
            if progress:
                progress(i + 1, total, os.path.basename(p))
        return out

    done = 0
    lock = threading.Lock()

    def one(p):
        try:
            return parse_invoice(p)
        except Exception as exc:            # 单张炸了不能拖垮整批
            info = InvoiceInfo(p)
            info.message = "解析异常：%s" % exc
            return info

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(one, p): i for i, p in enumerate(files)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                out[i] = fut.result()
            except Exception as exc:
                info = InvoiceInfo(files[i])
                info.message = "解析异常：%s" % exc
                out[i] = info
            with lock:
                done += 1
                if on_item:
                    on_item(i, out[i], total)
                if progress:
                    progress(done, total, os.path.basename(files[i]))

    # 极端情况下（线程被强杀）可能留空，补成"未识别"，不能让界面收到 None
    for i, item in enumerate(out):
        if item is None:
            info = InvoiceInfo(files[i])
            info.message = "解析被中断"
            out[i] = info
    return out


# ---------------------------------------------------------------- 文件名生成
ILLEGAL = r'<>:"/\|?*'


def sanitize(name: str) -> str:
    out = "".join("_" if c in ILLEGAL else c for c in name)
    out = out.replace("\n", " ").replace("\r", " ").strip().rstrip(".")
    return out or "未命名"


def format_amount(amount, mode: str = "smart", prefix: str = "") -> str:
    """mode: smart(整数不带小数/小数保留2位) | fixed(固定2位) | int(取整)"""
    if amount is None:
        return "未知金额"
    if mode == "fixed":
        s = "%.2f" % amount
    elif mode == "int":
        s = "%d" % round(amount)
    else:                                  # smart
        s = ("%d" % amount) if abs(amount - round(amount)) < 1e-9 else ("%.2f" % amount)
    return prefix + s


def field_values(info: InvoiceInfo, amount_mode: str = "smart",
                 currency_prefix: str = "") -> dict:
    """
    字段 key → 显示值 的映射，供勾选式「命名规则」使用。

    与老模板的区别：这里提不到的字段一律返回**空串**（不是「未识别购买方」），
    由调用方决定是否跳过。原因是勾选组合里某个字段缺失很常见
    （例如购买方是「个人」时没有税号），留下「未知税号」比空着更难看。
    """
    return {
        "buyer": info.buyer or "",
        "seller": info.seller or "",
        "amount": (format_amount(info.amount, amount_mode, currency_prefix)
                   if info.amount is not None else ""),
        "no": info.invoice_no or "",
        "noshort": (info.invoice_no[-8:] if info.invoice_no else ""),
        "date": info.date or "",
        "type": info.inv_type or "",
        "code": info.inv_code or "",
        "buyer_tax": info.buyer_tax or "",
        "seller_tax": info.seller_tax or "",
        "net": (format_amount(info.net, amount_mode, currency_prefix)
                if info.net is not None else ""),
        "tax": (format_amount(info.tax, amount_mode, currency_prefix)
                if info.tax is not None else ""),
        "amount_cn": info.amount_cn or "",
        "remark": info.remark or "",
        "payee": info.payee or "",
        "reviewer": info.reviewer or "",
        "drawer": info.drawer or "",
    }


def build_name(info: InvoiceInfo, template: str = "{buyer}—{amount}",
               amount_mode: str = "smart", currency_prefix: str = "",
               fields=None, sep: str = "—", suffix: str = "") -> str:
    """
    生成新文件名（不含扩展名）。

    两种模式：
      · fields 给了 → 按字段列表逐个取真实值拼装，**空值自动跳过**，
        所以不会出现「购买方——1060」这种中间空一截的结果。
      · fields 为空 → 沿用老的模板字符串格式化（命令行版与历史配置仍走这条路）。
    """
    vals = field_values(info, amount_mode, currency_prefix)

    if fields:
        parts = [vals.get(k, "") for k in fields]
        parts = [p for p in parts if p]          # 跳过提不到值的字段
        if not parts:
            # 所有字段都提不到 → 退回文件名前缀，至少不产生空名字
            return sanitize(os.path.splitext(info.file_name)[0])
        raw = str(sep).join(parts) + (str(suffix) if suffix else "")
        return sanitize(raw)

    data = dict(vals)
    # 老模板里承诺过的占位文案：购买方/销售方缺失时给可读的提示词
    data["buyer"] = data["buyer"] or "未识别购买方"
    data["seller"] = data["seller"] or "未识别销售方"
    data["amount"] = data["amount"] or "未知金额"
    try:
        raw = template.format(**data)
    except (KeyError, IndexError, ValueError):
        raw = "{buyer}—{amount}".format(**data)
    return sanitize(raw)


# ---------------------------------------------------------------- 自测入口
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python invoice_parser.py <pdf文件或文件夹>")
        sys.exit(0)

    target = sys.argv[1]
    if os.path.isdir(target):
        for item in parse_folder(target):
            print(f"{item.file_name}\n  购买方={item.buyer!r} 金额={item.amount} "
                  f"号码={item.invoice_no} 日期={item.date}\n  ok={item.ok} "
                  f"策略={item.engine} 说明={item.message}\n")
    else:
        item = parse_pdf(target)
        print(item.raw_text[:2000])
        print("-" * 60)
        print("购买方 :", item.buyer)
        print("销售方 :", item.seller)
        print("金额   :", item.amount)
        print("发票号 :", item.invoice_no, " 日期:", item.date)
        print("成功   :", item.ok, " 策略:", item.engine, " 说明:", item.message)
