# -*- coding: utf-8 -*-
"""
发票离线风控校验
==================================================
为什么是「离线」的：
    国家税务总局全国增值税发票查验平台是**纯 JS 单页应用 + 图形验证码**。
    全自动批量查验意味着要打包一个 Chromium（体积 +100MB）并破解验证码，
    对现在这个 26MB 的本地工具既不现实也不稳定，而且平台有频率限制。
    所以这里做**离线就能查的部分** —— 而这些恰恰是财务实操中最常见的差错来源：

    1. 发票号码位数（数电票是 20 位，读错了当天就能发现）
    2. 纳税人识别号的**校验位**（GB 32100-2015，一位错就能算出来）
    3. 价税合计的**大小写一致性**（篡改或识别错误都会露馅）
    4. 开票日期合理性（不能是未来日期）

    真伪核验另走「一键查验」通道：程序帮你把票面信息备好、打开查验平台，
    你只需输一次验证码 —— 不假装能做到全自动。
"""
import re
from datetime import date

# ------------------------------------------------------------------ 税号
# 统一社会信用代码：31 个字符（去掉了容易混淆的 I O S V Z）
USCC_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
USCC_WEIGHTS = [1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28]


def uscc_ok(code):
    """
    校验 18 位统一社会信用代码 / 纳税人识别号。
    返回 True / False / None（None = 没法判断，比如老式 15 位税号）。
    """
    code = (code or "").strip().upper()
    if len(code) != 18:
        return None                      # 老税号 15 位、或其他格式，不妄下结论
    try:
        total = sum(USCC_WEIGHTS[i] * USCC_CHARS.index(code[i])
                    for i in range(17))
    except ValueError:
        return None                      # 含非法字符 → 交给号码格式校验去报
    return code[17] == USCC_CHARS[(31 - total % 31) % 31]


# ------------------------------------------------------------------ 号码
def invoice_no_ok(no):
    """
    发票号码格式。
    数电票（全电发票）是 **20 位纯数字**；老式增值税发票是 8 位号码（+12 位代码）。
    返回 True / False / None（空号码不判）
    """
    d = re.sub(r"\D", "", no or "")
    if not d:
        return None
    if len(d) == 20:
        return True
    if len(d) in (8, 12):
        return True                      # 老式发票，正常
    return False


# ------------------------------------------------------------------ 大写金额
CN_DIGITS = "零壹贰叁肆伍陆柒捌玖"
CN_UNITS = ["", "拾", "佰", "仟"]
CN_GROUP = ["", "万", "亿", "万亿"]


def _int_to_cn(n):
    """
    整数 → 中文大写（票面写法，注意「零」的位置）。

    ⚠️ 历史 bug（2026-10-17 用户报「大小写金额不一致」**误报**，两张真实票）：
       老实现按「每 4 位一组」分别拼，只在**组内**补「零」，**跨组的零完全没管** ——
           200687 → 贰拾万陆佰捌拾柒   ❌ 正确是「贰拾万零陆佰捌拾柒」
           290021 → 贰拾玖万贰拾壹     ❌ 正确是「贰拾玖万零贰拾壹」
       校验时拿这个算错的结果去比票面，就报「大写与小写金额不一致」——
       而票面其实是对的，用户白核对一场。

       修法：**逐位扫全串**。遇到 0 先记着（zero），后面一旦出现非零数字，
       先补一个「零」再写这个数字；每 4 位到组末（个/万/亿位）时补组单位。
       这样跨组、组内、连续多个零（只补一个）都自然对了。
    """
    if n == 0:
        return "零"
    s = str(n)
    L = len(s)
    out = ""
    zero = False            # 前面遇到过 0，还没补「零」
    group_has_digit = False  # 当前这 4 位组里有没有出现过非零数字
    for i, ch in enumerate(s):
        d = int(ch)
        pos = L - 1 - i              # 这一位的权（0 = 个位）
        gi, pi = divmod(pos, 4)      # 第几组、组内第几位（0=个 1=拾 2=佰 3=仟）
        if d == 0:
            zero = True
        else:
            if zero and out:
                out += "零"
            zero = False
            out += CN_DIGITS[d] + CN_UNITS[pi]
            group_has_digit = True
        if pi == 0:                  # 到组末了
            if group_has_digit:      # 整组全零就不加「万/亿」（如 100000000 不应变成「壹亿零万」）
                out += CN_GROUP[gi]
                group_has_digit = False
    return out


def amount_to_cn(amount):
    """金额 → 票面标准大写（如 1410.3 → 壹仟肆佰壹拾圆叁角）"""
    neg = amount < 0
    cents = int(round(abs(amount) * 100))
    yuan, rest = divmod(cents, 100)
    jiao, fen = divmod(rest, 10)

    s = (_int_to_cn(yuan) if yuan else "零") + "圆"
    if jiao == 0 and fen == 0:
        s += "整"
    else:
        if jiao:
            s += CN_DIGITS[jiao] + "角"
        elif fen:
            s += "零"                     # 有分无角，必须补「零」
        if fen:
            s += CN_DIGITS[fen] + "分"
    return ("负" if neg else "") + s


def _norm_cn(s):
    """大写金额归一化后再比对：各家开票系统的空格/结尾写法不一，先抹平"""
    s = re.sub(r"\s+", "", s or "")
    s = s.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    s = re.sub(r"^(人民币|RMB|CNY)", "", s, flags=re.I)
    for tail in ("整", "正", "元整"):
        if s.endswith(tail):
            s = s[:-len(tail)]
    return s.replace("元", "圆")          # 「元/圆」两种写法都见过


def amount_cn_ok(amount, cn_text):
    """
    校验「价税合计大写」和金额是否一致。
    返回 True / False / None（信息不足时不判）
    """
    if amount is None or not (cn_text or "").strip():
        return None
    if amount < 0:
        return None                       # 红字票的大写写法各家不统一，不判
    try:
        forms = _cn_forms(amount)
    except Exception:
        return None
    got = _norm_cn(cn_text)
    if not forms or not got:
        return None
    return got in forms


def _cn_forms(amount):
    """
    同一个金额，票面上可能出现的几种**合法**写法（用于宽松比对）。

    ⚠️ 不足 1 元时：国标写法是「玖角伍分」，不写「零圆」；
       但程序拼串时会带上「零圆」→「零圆玖角伍分」，直接比会判成不一致。
       业主 2026-10-18 报的「0.95 元通行费被标待核对」就是这个原因。
    另外「整/正」有无、圆/元混用也一并容忍（_norm_cn 里统一）。
    """
    base = amount_to_cn(amount)
    forms = {base}
    if base.startswith("零圆"):
        cut = base[2:]                    # 零圆玖角伍分 → 玖角伍分
        forms.add(cut)
        forms.add(cut.lstrip("零"))       # 零圆零伍分 → 伍分
    return {_norm_cn(f) for f in forms}


# ------------------------------------------------------------------ 日期
def date_ok(ymd, slack_days=1):
    """
    开票日期不能是未来（容忍 1 天，避开时区/当天开票的边界）。
    返回 True / False / None
    """
    d = re.sub(r"\D", "", ymd or "")
    if len(d) != 8:
        return None
    try:
        dt = date(int(d[:4]), int(d[4:6]), int(d[6:8]))
    except ValueError:
        return False                      # 20261332 这种非法日期
    from datetime import timedelta
    return dt <= date.today() + timedelta(days=slack_days)


# ------------------------------------------------------------------ 总入口
def validate(item, today=None):
    """
    对一张发票做全套离线校验。

    `item` 需要具备：invoice_no / buyer_tax / seller_tax / amount /
                     amount_cn / date / is_red（可选）
    返回 [{"level":"error"|"warn", "field":..., "msg":...}, ...]
    空列表 = 没发现问题
    """
    out = []

    v = invoice_no_ok(getattr(item, "invoice_no", ""))
    if v is False:
        out.append({"level": "error", "field": "发票号码",
                    "msg": "号码位数异常（数电票应为 20 位），可能是读取有误"})

    for fld, label in (("buyer_tax", "购买方税号"), ("seller_tax", "销售方税号")):
        code = getattr(item, fld, "") or ""
        if not code.strip():
            continue
        v = uscc_ok(code)
        if v is False:
            out.append({"level": "warn", "field": label,
                        "msg": "校验位对不上，税号可能有误（%s）" % code})

    v = amount_cn_ok(getattr(item, "amount", None),
                     getattr(item, "amount_cn", ""))
    if v is False:
        out.append({"level": "warn", "field": "价税合计",
                    "msg": "大写与小写金额不一致，请核对票面"})

    v = date_ok(getattr(item, "date", ""))
    if v is False:
        out.append({"level": "warn", "field": "开票日期",
                    "msg": "开票日期不合法（可能是未来日期或读取有误）"})

    return out


def worst_level(results):
    """把校验结果压成一个等级，供列表标色：error > warn > ''"""
    lv = ""
    for r in results or []:
        if r["level"] == "error":
            return "error"
        lv = "warn"
    return lv
