# -*- coding: utf-8 -*-
"""
票面预览（v2.6 新增）。

两个能力：
  1. 把 PDF 的某一页渲染成 PNG（接口返回 data URL，前端直接 <img>）
  2. 算出「购买方 / 价税合计 / 发票号码」在**票面上的矩形位置**，
     前端据此在图上画框 —— 让用户一眼看到"这个值是从哪块读出来的"。
     读错了能立刻发现，而不是盲目信任解析结果。

为什么用 search_for 反查位置：
    解析时是按行/坐标取值的，但没有保存"这个值来自哪个矩形"。与其改解析器
    （风险大），不如拿解析出来的文本回到页面上**搜索**一次 —— 文本层里的
    字符串和取到的值一致时就能定位，简单可靠。

⚠️ 只支持 PDF。OFD / XML 没有可直接渲染的页面，返回 ok=False 由界面降级显示。
"""

import base64
import os

from invoice_parser import PDF_PASSWORDS, _fitz

# 渲染结果的内存缓存：(path, mtime_ns, page, 目标宽) -> data URL
# 一页 PNG 大约 100~200KB，留 8 页够翻着看，再多就占内存了
_RENDER_CACHE = {}
_RENDER_MAX = 8


def _open(path):
    doc = _fitz().open(path)          # 惰性加载，见 invoice_parser._fitz 的注释
    if doc.needs_pass:
        pwd = (PDF_PASSWORDS.get(os.path.abspath(path))
               or PDF_PASSWORDS.get(path) or "")
        if not pwd or not doc.authenticate(pwd):
            doc.close()
            raise RuntimeError("PDF 已加密，请先输入密码")
    return doc


def _stamp(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def render(path, page=0, target_width=760):
    """
    把第 page 页渲染成 PNG data URL。

    目标宽度而不是缩放倍数：预览面板宽度是固定的，按目标宽度反算 zoom，
    不同尺寸的票（A4 / A5 / 小票）渲染出来宽度一致，界面不会忽大忽小。
    """
    if not os.path.exists(path):
        return {"ok": False, "msg": "文件不存在"}
    if not path.lower().endswith(".pdf"):
        return {"ok": False, "msg": "只有 PDF 能预览票面（OFD / XML 没有页面）",
                "no_render": True}

    key = (os.path.abspath(path).lower(), _stamp(path), int(page), int(target_width))
    hit = _RENDER_CACHE.get(key)
    if hit:
        return hit["payload"]

    doc = None
    try:
        doc = _open(path)
        if doc.page_count == 0:
            return {"ok": False, "msg": "这个 PDF 没有页面"}
        page = max(0, min(int(page), doc.page_count - 1))
        p = doc[page]
        w = p.rect.width or 595
        zoom = max(0.6, min(3.0, float(target_width) / float(w)))
        pix = p.get_pixmap(matrix=_fitz().Matrix(zoom, zoom), alpha=False)
        png = pix.tobytes("png")
        payload = {
            "ok": True,
            "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            "page": page,
            "pages": doc.page_count,
            "width": pix.width,
            "height": pix.height,
        }
    except Exception as exc:
        return {"ok": False, "msg": "预览失败：%s" % exc}
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass

    if len(_RENDER_CACHE) >= _RENDER_MAX:
        _RENDER_CACHE.pop(next(iter(_RENDER_CACHE)), None)
    _RENDER_CACHE[key] = {"payload": payload}
    return payload


def _find(page, text, tries=(1.0, 0.6, 0.35)):
    """
    在页面上找一段文本，返回它外接的矩形（页宽高归一化到 0~1）。

    为什么分几档长度去试：购买方名称太长时会换行，整串搜不到；
    退而求其次搜前半段，至少能把大概位置框出来。
    """
    t = (text or "").strip()
    if not t:
        return None
    rect_page = page.rect
    W = rect_page.width or 1
    H = rect_page.height or 1
    for ratio in tries:
        probe = t if ratio >= 1 else t[: max(2, int(len(t) * ratio))]
        if len(probe) < 2:
            continue
        try:
            hits = page.search_for(probe)
        except Exception:
            hits = []
        if hits:
            r = hits[0]
            for other in hits[1:4]:          # 多段命中就取并集
                r = r | other
            return [max(0.0, r.x0 / W), max(0.0, r.y0 / H),
                    min(1.0, r.x1 / W), min(1.0, r.y1 / H)]
    return None


def field_rects(path, page, info):
    """
    算出关键字段在票面上的位置。

    info: invoice_parser.InvoiceInfo
    返回 {"buyer": [x0,y0,x1,y1], "amount": [...], "no": [...], "date": [...]}
    （全部是 0~1 的相对坐标，前端按百分比定位，窗口缩放也不会错位）
    """
    out = {}
    if not os.path.exists(path) or not path.lower().endswith(".pdf"):
        return out
    doc = None
    try:
        doc = _open(path)
        idx = max(0, min(int(page or 0), doc.page_count - 1))
        p = doc[idx]
        out["buyer"] = _find(p, getattr(info, "buyer", ""))
        out["amount"] = _find(p, getattr(info, "amount_text", ""))
        out["no"] = _find(p, getattr(info, "invoice_no", ""))
    except Exception:
        return {}
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    return {k: v for k, v in out.items() if v}
