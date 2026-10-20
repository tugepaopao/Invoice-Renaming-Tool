# -*- coding: utf-8 -*-
"""
全电发票批量重命名工具 —— 主程序

界面：pywebview（HTML/CSS/JS）  ·  后端：bridge.py + invoice_parser.py + rename_core.py
"""
import os
import sys

# 确保能 import 同目录模块（打包后资源目录不同）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctypes
import logging

import webview

import bridge

# 窗口标题也带版本号：任务栏悬停、多版本并存时能一眼分清
WINDOW_TITLE = "%s v%s" % (bridge.APP_NAME, bridge.VERSION)
BG_COLOR = "#F3F5F9"

# ============================================================
# 自定义标题栏（v2.8.0）
# ------------------------------------------------------------
# 业主 2026-10-18 定稿：去掉系统标题栏，把 最小化/最大化/关闭 画进软件顶栏，
# 窗口看起来是一整块（方案 A）。
#
# 为什么不能只用 frameless=True 就完事：
#   pywebview 的 winforms 后端把 frameless 实现成 FormBorderStyle=None，
#   这会连**窗口边框**一起删掉 —— 用户就再也拖不动窗口大小，也没法贴边分屏。
#   所以这里在 frameless 之后，用 Win32 把 WS_THICKFRAME 加回去：
#   只去掉"标题栏的绘制"，保留缩放边框与系统阴影。
#
# 拖动窗口则走经典做法：ReleaseCapture + WM_NCLBUTTONDOWN(HTCAPTION)，
# 由 Windows 接管拖动，跟手、不抖，贴边分屏也还在。
#
# 兜底开关：设 INVOICE_NATIVE_FRAME=1 就完全回到系统标题栏（万一某台机器异常）。
# ============================================================
CUSTOM_FRAME = os.environ.get("INVOICE_NATIVE_FRAME", "0") != "1"

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2


def _tell_js(window, flag):
    """把「已最大化 / 已还原」告诉前端，让它换 □ / ❐ 图标"""
    try:
        window.evaluate_js("window.__setWinMax && window.__setWinMax(%s)" % flag)
    except Exception:
        pass


# ---- 自定义标题栏的窗口过程 ----
# 为什么要子类化：去掉 WS_CAPTION 后，窗口四周仍留 8px 的非客户区（WinForms 刷成白色）。
# 标准做法是 WM_NCCALCSIZE 时把客户区扩到整窗，再由 WM_NCHITTEST 自己回答"哪儿能拖缩放"。
# ⚠️ WinForms 在窗口句柄重建时会把 WndProc 装回去，所以要在 HandleCreated 里重挂，
#    并在启动后 3 秒内做几次看门狗检查（实测：只在 shown 时挂，会被覆盖掉）。
_WNDPROC_REF = {}
_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p,
                              ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)


def _install_wndproc(hwnd):
    u32 = ctypes.windll.user32
    u32.GetWindowLongPtrW.restype = ctypes.c_void_p
    u32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u32.SetWindowLongPtrW.restype = ctypes.c_void_p
    u32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    u32.CallWindowProcW.restype = ctypes.c_ssize_t
    u32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_size_t, ctypes.c_ssize_t]

    prev = u32.GetWindowLongPtrW(hwnd, -4)

    @_WNDPROC
    def proc(h, msg, wp, lp):
        if msg == 0x0083 and wp:                     # WM_NCCALCSIZE（wParam=1）
            return 0                                 # 客户区占满整个窗口
        if msg == 0x0084:                            # WM_NCHITTEST
            x = lp & 0xFFFF
            x = x - 65536 if x >= 32768 else x
            y = (lp >> 16) & 0xFFFF
            y = y - 65536 if y >= 32768 else y
            if u32.IsZoomed(h):
                return 1
            from ctypes import wintypes as wt
            r = wt.RECT()
            u32.GetWindowRect(h, ctypes.byref(r))
            try:
                dpi = u32.GetDpiForWindow(h) or 96
                bw = max(8, int(8 * dpi / 96))
            except Exception:
                bw = 8
            L = x - r.left < bw
            R = r.right - x < bw
            T = y - r.top < bw
            B = r.bottom - y < bw
            if L and T:
                return 13
            if R and T:
                return 14
            if L and B:
                return 16
            if R and B:
                return 17
            if L:
                return 10
            if R:
                return 11
            if T:
                return 12
            if B:
                return 15
            return 1
        return u32.CallWindowProcW(prev, h, msg, wp, lp)

    _WNDPROC_REF["proc"] = proc        # 防 GC
    _WNDPROC_REF["prev"] = prev
    want = ctypes.cast(proc, ctypes.c_void_p).value
    u32.SetWindowLongPtrW(hwnd, -4, ctypes.c_void_p(want))
    now = u32.GetWindowLongPtrW(hwnd, -4)
    _WNDPROC_REF["installed"] = (now == want)
    return _WNDPROC_REF["installed"]


def _ensure_wndproc(window, tries=0):
    """句柄重建后 WndProc 会被换掉 —— 这里补挂（最多重试几次）"""
    try:
        hwnd = int(window.native.Handle.ToInt64())
        if _install_wndproc(hwnd):
            return
    except Exception:
        pass
    if tries < 6:
        import threading as _th
        _th.Timer(0.4, lambda: _ensure_wndproc(window, tries + 1)).start()


def _apply_custom_frame(window):
    """
    frameless 之后把「可缩放边框」加回来，只留边框、不留标题栏。

    这一步必须在窗口句柄已经创建之后执行（shown 事件里最稳）。
    失败了也不能影响使用 —— 最坏情况就是回到 frameless（没有缩放边）。
    """
    try:
        hwnd = int(window.native.Handle.ToInt64())
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        style &= ~WS_CAPTION                     # 去掉标题栏
        style |= (WS_THICKFRAME | WS_MINIMIZEBOX
                  | WS_MAXIMIZEBOX | WS_SYSMENU)  # 保留缩放边 / 三个系统按钮
        user32.SetWindowLongW(hwnd, GWL_STYLE, style)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)

        # ① 8px 的边框带是窗口自己画的，默认白色 —— 业主截图里"顶上一条白边"就是它。
        #    把窗体底色设成页面底色，白边立刻消失（内容看起来顶到窗口边缘）。
        try:
            from System.Drawing import ColorTranslator
            window.native.BackColor = ColorTranslator.FromHtml(BG_COLOR)
        except Exception as exc:
            logging.info("设置窗口底色失败（不影响使用）：%s", exc)

        # ② Win11 会给"活动窗口"画一圈强调色描边（截图顶上那条蓝线）。
        #    DWMWA_BORDER_COLOR = 34，传 DWMWA_COLOR_NONE 表示不要描边。
        try:
            no_color = ctypes.c_uint(0xFFFFFFFE)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 34, ctypes.byref(no_color), ctypes.sizeof(no_color))
        except Exception:
            pass

        # ③ 客户区扩到整窗，消掉 8px 白边
        _ensure_wndproc(window)
        # 光把客户区算大还不够：WinForms 的子控件（WebView2）还停在旧尺寸上，
        # 顶上会留 8px 的白底。这里抖动 1px 触发一次 WM_SIZE，让它重新铺满。
        def _nudge():
            try:
                from ctypes import wintypes as _wt
                u = ctypes.windll.user32
                hwnd2 = int(window.native.Handle.ToInt64())
                rr = _wt.RECT()
                u.GetWindowRect(hwnd2, ctypes.byref(rr))
                w, hh = rr.right - rr.left, rr.bottom - rr.top
                u.SetWindowPos(hwnd2, 0, 0, 0, w, hh + 1,
                               0x0002 | 0x0004 | 0x0010)   # NOMOVE|NOZORDER|NOACTIVATE
                u.SetWindowPos(hwnd2, 0, 0, 0, w, hh,
                               0x0002 | 0x0004 | 0x0010)
            except Exception:
                pass
        _nudge()
        try:
            win = window.native

            def _on_recreate(sender, args):
                _ensure_wndproc(window)

            win.HandleCreated += _on_recreate
            _WNDPROC_REF["handler"] = _on_recreate
        except Exception:
            pass

        # ③ 顶部那条 8px 白边是 DWM 的"玻璃边框"（pywebview 的 shadow=True 会调
        #    DwmExtendFrameIntoClientArea，把这段刷成白色）。
        #    关掉 shadow + 把窗体底色设成页面底色，白边自然消失（实测整条都是 #F3F5F9）。
        logging.info("自定义标题栏已启用（style=0x%08X）", style)
    except Exception as exc:                      # 绝不能因为装饰性功能起不来
        logging.warning("自定义标题栏启用失败，回退无边框模式：%s", exc)



class _QuietNoise(logging.Filter):
    """
    pywebview 会在读取 .NET 无障碍树 / 跨线程访问 WebView2 时刷大量
    「Error while processing window.native.xxx」日志。这些是无害的，
    但它们会淹没调试窗口里真正有用的报错，这里过滤掉。
    """

    NOISE = ("AccessibilityObject", "only be accessed from the UI thread",
             "ImeMode", "E_NOINTERFACE")

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(k in msg for k in self.NOISE)


def quiet_logs():
    for name in ("pywebview", "webview", "__main__"):
        lg = logging.getLogger(name)
        lg.addFilter(_QuietNoise())


def storage_path():
    """WebView2 用户数据目录（放用户目录里，不污染工具文件夹）"""
    base = os.environ.get("LOCALAPPDATA") or bridge.data_dir()
    p = os.path.join(base, "InvoiceRenamer", "webview")
    try:
        os.makedirs(p, exist_ok=True)
    except Exception:
        return None
    return p


def pick_free_port():
    """
    给 pywebview 的内部 HTTP 服务挑一个空闲端口。

    为什么必须自己做：
        pywebview 在 private_mode=False 时会把端口写死成 42001
        （webview/__init__.py 里的 DEFAULT_HTTP_PORT）。
        如果这个端口被上一次运行残留的连接占着，本地页面就加载不出来，
        窗口会卡住十几秒表现为「未响应」——而且时有时无，很难查。
        这里改成每次启动都找一个真正空闲的端口，彻底避开冲突。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    except Exception:
        return 0
    finally:
        s.close()


def reset_webview_profile():
    """
    一次性清理旧版本可能留下的损坏 WebView2 配置目录。

    背景：旧版本用 pywebview 默认的 private_mode=True，退出时会删掉整个
    用户数据目录，删除常因文件被占用而失败，留下半损坏的目录，下次启动要
    花十几秒做恢复。
    现在改成 private_mode=False 保留配置，正常情况下不会再损坏。
    但用户机器上可能已经有那个坏目录，所以这里做一次带标记的清理：
    清一次就写下标记文件，以后永不再清。
    """
    p = storage_path()
    if not p:
        return
    marker = os.path.join(p, ".profile_ok_v2")
    if os.path.exists(marker):
        return
    try:
        import shutil
        shutil.rmtree(p, ignore_errors=True)
        os.makedirs(p, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("ok")
    except Exception:
        pass


def fatal(msg, detail=""):
    """弹系统对话框报错（比静默退出友好）"""
    try:
        import ctypes
        text = msg + ("\n\n" + detail if detail else "")
        ctypes.windll.user32.MessageBoxW(0, text, WINDOW_TITLE, 0x10)
    except Exception:
        print(msg, detail)


def selftest():
    """
    自检模式：把运行环境信息写一份报告到 exe 同目录，用于排查问题。
    用法：发票批量重命名工具.exe --selftest
    """
    import json
    index = os.path.join(bridge.resource_dir(), "web", "index.html")
    report = {
        "时间": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "打包运行": bool(getattr(sys, "frozen", False)),
        "exe路径": sys.executable,
        "资源目录": bridge.resource_dir(),
        "数据目录": bridge.data_dir(),
        "配置文件": bridge.CONFIG_PATH,
        "界面文件存在": os.path.exists(index),
        "界面文件": index,
        "web目录内容": sorted(os.listdir(os.path.join(bridge.resource_dir(), "web")))
        if os.path.isdir(os.path.join(bridge.resource_dir(), "web")) else [],
    }
    try:
        import webview as _w
        report["pywebview"] = getattr(_w, "__version__", "已安装")
    except Exception as exc:
        report["pywebview"] = "导入失败：%s" % exc
    try:
        import pymupdf
        report["pymupdf"] = pymupdf.version[0]
    except Exception as exc:
        report["pymupdf"] = "导入失败：%s" % exc

    # 诊断配置读取链路
    try:
        report["数据目录可写"] = os.access(bridge.data_dir(), os.W_OK)
        report["配置文件是否存在"] = os.path.exists(bridge.CONFIG_PATH)
        loaded = bridge.load_config()
        report["加载到的配置"] = loaded
        report["上次文件夹"] = loaded.get("lastFolder", "")
        a = bridge.Api()
        report["Api.folder"] = a.folder
        report["Api.看到的面板数"] = len(a.items)
    except Exception as exc:
        report["配置诊断"] = "失败：%s" % exc

    out = os.path.join(bridge.data_dir(), "自检报告.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return 0


def _wire_file_drop(window, api):
    """
    让「把发票文件 / 文件夹 / 压缩包拖进窗口」真的能用。

    为什么不用前端 HTML5 的 drop：
        浏览器出于安全**不会**把文件的绝对路径交给 JS，而我们恰恰要靠绝对路径
        去原地改名。WebView2 里真实路径只存在于宿主侧 —— pywebview 为此提供了
        DOM 事件回调，会把 `CoreWebView2File.Path` 塞进
        `event.dataTransfer.files[i].pywebviewFullPath` 交给 Python。
        所以这条链路必须从 Python 侧注册。

    三个必须注意的点（踩过才知）：
      · pywebview 只有在**存在 DOM 监听器**时才会记录拖入的路径
        （内部 _dnd_state['num_listeners'] 为 0 就直接 return），
        所以下面这个监听器是功能的一部分，不是可选装饰。
      · `prevent_default=True` 要开着，否则 WebView2 会直接导航到被拖进来的
        文件（界面被替换成那个 PDF）。
      · 拖拽时的**高亮反馈**由前端 JS 自己画，两边互不干扰：
        JS 负责视觉，Python 负责拿路径。
    """
    try:
        # ⚠️ 符号在 webview.dom 下，**不在** webview 顶层。
        #    写成 webview.DOMEventHandler 会 AttributeError；
        #    而下面又兜住了异常，于是拖放会「静默失效」——
        #    界面照常打开、拖文件没反应，极难排查。
        #    （这个 bug 就是打包后的端到端探针抓出来的。）
        try:
            from webview.dom import DOMEventHandler as _DnDHandler
        except ImportError:                      # 兼容旧版把符号放在顶层的情况
            _DnDHandler = webview.DOMEventHandler

        # 挂在 body 上：拖到窗口任意位置都能接住（drop 会冒泡到 body）
        target = window.dom.body

        def on_drop(e):
            """
            拖放导入的**主通道**。

            为什么它可以当主通道：pywebview 在派发本回调前，会把 WebView2 给的
            真实文件路径回配进 `pywebviewFullPath`（本机实测能成功）。
            我们要做的只是别浪费它。

            但它**不能是唯一通道**：pywebview 那段回配逻辑一旦失败
            （文件名对不上、序列化丢了 name 导致 KeyError）就完全静默，
            严重时本回调根本不会被调用。所以：
              · 回配成功 → 这里直接导入
              · 回配失败 → 从宿主记录表里捞剩下的（bridge.take_drop_paths 也做同样的事，
                前端会在 drop 后兜底调一次；accept_drop 有防重复，两边都触发也不会重复导入）
            """
            try:
                files = (e.get("dataTransfer") or {}).get("files") or []
                paths = [f.get("pywebviewFullPath") for f in files
                         if isinstance(f, dict) and f.get("pywebviewFullPath")]
                where = "pywebviewFullPath"
                if not paths:
                    # 回配失败：表里可能还留着没被消费的记录
                    try:
                        from webview.dom import _dnd_state
                        paths = [p for (_n, p) in _dnd_state.get("paths", []) if p]
                        _dnd_state["paths"].clear()
                        where = "宿主记录表（回配失败兜底）"
                    except Exception:
                        paths = []
                api._log_drop("event", ["drop 到达，从 %s 取到 %d 条路径"
                                        % (where, len(paths))])
                if paths:
                    api.accept_drop(list(dict.fromkeys(paths)))
            except Exception as exc:
                api._log_drop("error", ["drop 回调异常：%s" % exc])

        target.events.drop += _DnDHandler(on_drop, prevent_default=True)
        # 可验证的探针：pywebview 只在 num_listeners > 0 时才会记录拖入文件的
        # 真实路径（见 platforms/edgechromium.py 的 FilesDropped 分支）。
        # 光「没抛异常」说明不了问题，所以把开关状态如实报出来，
        # 让自动化能断言「拖放真的能拿到路径」。
        try:
            from webview.dom import _dnd_state
            api._dropReady = _dnd_state.get("num_listeners", 0) > 0
        except Exception:
            api._dropReady = False
    except Exception as exc:
        # 拖放只是锦上添花，挂了也不能影响正常启动；
        # 但必须把原因留下来 —— 前端会据此提示用户改用「选择文件夹」。
        api._dropReady = False
        api._dropErr = "%s: %s" % (type(exc).__name__, exc)


def main():
    if "--selftest" in sys.argv:
        return selftest()
    quiet_logs()
    api = bridge.Api()

    index = os.path.join(bridge.resource_dir(), "web", "index.html")
    if not os.path.exists(index):
        fatal("找不到界面文件：\n%s\n\n请确认 web 文件夹与本程序放在一起。" % index)
        return 1

    # 打包版启动自检：pymupdf 的两个 .pyd 缺一个，所有 PDF 都会报
    # 「cannot import name '_extra' from partially initialized module 'pymupdf'」。
    # 最常见的原因是**直接在压缩包里双击 exe**（只解压到临时目录），
    # 或者杀软把 _extra.pyd 当可疑文件删了。这里提前说清楚，别让用户看一堆英文。
    if getattr(sys, "frozen", False):
        _base = getattr(sys, "_MEIPASS", "")
        _pdir = os.path.join(_base, "pymupdf")
        _need = ("_extra.pyd", "_mupdf.pyd")
        _miss = [n for n in _need if not os.path.exists(os.path.join(_pdir, n))]
        if _miss:
            fatal("解析组件不完整，读不了 PDF。",
                  "缺少文件：pymupdf\\" + "、".join(_miss) + "\n\n"
                  "常见原因：\n"
                  "  1) 直接在压缩包里双击运行（文件只解到临时目录）；\n"
                  "  2) 杀毒软件把安装目录里的 .pyd 删了。\n\n"
                  "处理办法：\n"
                  "  · 用安装包重新安装（推荐），或把整个文件夹完整解压后再运行；\n"
                  "  · 并把安装目录加入杀毒软件的白名单。")
            return 1

    window = webview.create_window(
        WINDOW_TITLE,
        url=index,
        js_api=api,
        width=1340,
        height=880,
        min_size=(1100, 700),
        background_color=BG_COLOR,
        text_select=False,
        confirm_close=False,
        shadow=False,
        frameless=CUSTOM_FRAME,        # 去掉系统标题栏（见上方 CUSTOM_FRAME 说明）
    )
    api._window = window
    api._custom_frame = CUSTOM_FRAME      # 前端据此决定显不显示窗口按钮

    if CUSTOM_FRAME:
        # shown 时才拿到稳定的窗口句柄；这里补回缩放边框
        window.events.shown += lambda: _apply_custom_frame(window)
        # 最大化 / 还原时通知前端换图标（□ ↔ ❐）
        window.events.maximized += lambda: _tell_js(window, "true")
        window.events.restored += lambda: _tell_js(window, "false")

    # 拖放导入：必须在页面加载完之后再挂，那时 #dropZone 才存在
    window.events.loaded += lambda: _wire_file_drop(window, api)

    reset_webview_profile()

    debug = "--debug" in sys.argv
    port = pick_free_port()
    # 诊断开关：设了 INVOICE_NO_HTTP_PORT=1 就不指定端口，用于排查问题
    if os.environ.get("INVOICE_NO_HTTP_PORT") == "1":
        port = None

    # 诊断开关：设了 INVOICE_DEBUG_PORT=端口号（如 9333），WebView2 会开放调试端口，
    # 可以用浏览器/自动化脚本直接查看界面真实的 DOM 与截图（排查界面问题用）。
    # 不设置时完全不影响启动。
    _dbg_port = os.environ.get("INVOICE_DEBUG_PORT")
    if _dbg_port:
        try:
            webview.settings["REMOTE_DEBUGGING_PORT"] = int(_dbg_port)
        except Exception:
            pass
    try:
        # 两个关键参数，都是为了启动不卡：
        #   private_mode=False —— 默认 True 时 pywebview 每次退出都会删掉整个
        #     WebView2 用户数据目录，下次启动得冷初始化（十几秒白屏/未响应），
        #     而且删除常因文件被占用失败。改 False 后配置被保留，热启动秒开。
        #   http_port —— 自己指定空闲端口，避开 pywebview 写死的 42001 被占用
        #     导致页面加载不出来、窗口卡死的问题。
        webview.start(debug=debug,
                      private_mode=False,
                      storage_path=storage_path(),
                      http_port=port)
    except Exception as exc:
        text = str(exc)
        if "WebView2" in text or "webview2" in text.lower():
            fatal("本机缺少 Microsoft Edge WebView2 运行环境，界面无法显示。",
                  "解决办法（任选其一）：\n"
                  "1. 安装 WebView2 Runtime（免费，约 2 分钟）：\n"
                  "   https://developer.microsoft.com/microsoft-edge/webview2/\n"
                  "2. 或者更新一次 Windows 自带的 Microsoft Edge 浏览器。\n\n"
                  "错误详情：\n" + text)
        else:
            fatal("程序启动失败。", text)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
