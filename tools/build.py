# -*- coding: utf-8 -*-
"""
一键打包：PyInstaller(onedir) → NSIS 安装包

用法（在工具目录下）：
    .venv\\Scripts\\python.exe tools\\build.py            # 完整打包
    .venv\\Scripts\\python.exe tools\\build.py --exe-only # 只出免安装版
    .venv\\Scripts\\python.exe tools\\build.py --setup-only # 只重新生成安装包

依赖：PyInstaller（.venv 里已装）；NSIS 若缺失会自动下载便携版到 tools/_nsis/
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from version import BUILD_NAME, VERSION      # noqa: E402  版本号/名字唯一来源

# PyInstaller 的 --name 与产物目录用 BUILD_NAME（发票批量重命名工具），
# 不是给人看的 APP_NAME（全电发票批量重命名工具）—— 见 version.py 的注释。
APP_NAME = BUILD_NAME


def check_version():
    """
    校验 installer.nsi 里的版本号与名字跟 version.py 一致。

    NSIS 脚本没法 import Python，只能各写一份；不一致的后果都很隐蔽：
      · 版本号不一致 → 「装出来是 2.2.0、界面显示 2.2.1」
      · 名字不一致   → NSIS 去 dist\\发票批量重命名工具 找不到文件，
                      报一句 "no files found"，看起来完全不像名字问题
                      （这个坑真踩到了：把 build.py 的 APP_NAME 换成显示名之后，
                       产物目录变成了 dist\\全电发票批量重命名工具，安装包直接失败）
    所以打包前一次性全查，不一致就停下。
    """
    nsi = io.open(os.path.join(ROOT, "installer.nsi"), encoding="utf-8").read()
    checks = [
        ("APP_VERSION", VERSION, r'!define\s+APP_VERSION\s+"([^"]+)"'),
        ("APP_NAME", "全电发票批量重命名工具",
         r'!define\s+APP_NAME\s+"([^"]+)"'),
        ("APP_EXE", APP_NAME + ".exe", r'!define\s+APP_EXE\s+"([^"]+)"'),
        ("SOURCE_DIR", "dist\\" + APP_NAME, r'!define\s+SOURCE_DIR\s+"([^"]+)"'),
    ]
    bad = []
    for label, want, pattern in checks:
        m = __import__("re").search(pattern, nsi)
        if not m:
            bad.append("%s：installer.nsi 里找不到该定义" % label)
        elif m.group(1) != want:
            bad.append("%s：installer.nsi = %r，version.py 期望 %r"
                       % (label, m.group(1), want))
    for msg in bad:
        print("  " + msg)
    return not bad
NSIS_DIR = os.path.join(ROOT, "tools", "_nsis")
NSIS_URLS = [
    "https://downloads.sourceforge.net/project/nsis/NSIS%203/3.10/nsis-3.10.zip",
    "https://sourceforge.net/projects/nsis/files/NSIS%203/3.10/nsis-3.10.zip/download",
]


def log(msg):
    print("  " + msg, flush=True)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, **kw)
    return r.returncode, r.stdout, r.stderr


# ---------------------------------------------------------------- exe
def build_exe():
    print("\n【1/3】用 PyInstaller 打包免安装版（onedir）")
    py = sys.executable
    rc, out, err = run([
        py, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir", "--windowed",
        "--name", APP_NAME,
        "--icon", os.path.join(ROOT, "app.ico"),
        "--add-data", "web;web",
        "--collect-all", "pymupdf",
        "--collect-submodules", "webview",
        "--hidden-import", "clr",
        # ⚠️ Pillow 只是 tools/_bench/grab.py 截图诊断用的，绝不能进安装包
        #    （装了 pillow 之后包会从 26.3MB 涨到 29.5MB）
        "--exclude-module", "PIL",
        "--exclude-module", "PIL.Image",
        "--exclude-module", "PIL.ImageGrab",
        os.path.join(ROOT, "app.py"),
    ], cwd=ROOT)
    if rc != 0:
        print(err.decode("utf-8", "replace")[-3000:])
        return False
    log("完成：dist\\%s\\%s.exe" % (APP_NAME, APP_NAME))
    return True


# ---------------------------------------------------------------- NSIS
def find_makensis():
    for p in (os.path.join(NSIS_DIR, "makensis.exe"),
              r"C:\Program Files (x86)\NSIS\makensis.exe",
              r"C:\Program Files\NSIS\makensis.exe"):
        if os.path.exists(p):
            return p
    return None


def download_nsis():
    print("\n【2/3】未找到 NSIS，正在下载便携版…")
    os.makedirs(NSIS_DIR, exist_ok=True)
    zpath = os.path.join(NSIS_DIR, "nsis.zip")
    ok = False
    for url in NSIS_URLS:
        log("下载 " + url)
        rc, _, _ = run(["curl", "-L", "--max-time", "300", "-o", zpath, url])
        if rc == 0 and os.path.exists(zpath) and os.path.getsize(zpath) > 500000:
            ok = True
            break
    if not ok:
        print("  下载失败。请手动安装 NSIS（https://nsis.sourceforge.io/）后重试。")
        return None
    with zipfile.ZipFile(zpath) as z:
        z.extractall(NSIS_DIR)
    for root, _dirs, files in os.walk(NSIS_DIR):
        if "makensis.exe" in files:
            exe = os.path.join(root, "makensis.exe")
            if root != NSIS_DIR:
                for n in os.listdir(root):
                    src = os.path.join(root, n)
                    dst = os.path.join(NSIS_DIR, n)
                    if not os.path.exists(dst):
                        shutil.move(src, dst)
                shutil.rmtree(root, ignore_errors=True)
                exe = os.path.join(NSIS_DIR, "makensis.exe")
            log("就绪：" + exe)
            return exe
    print("  解压后没找到 makensis.exe")
    return None


def build_setup():
    print("\n【3/3】编译安装包")
    makensis = find_makensis() or download_nsis()
    if not makensis:
        return False

    # 安装包是把整个 dist 目录 File /r 进去的，所以必须先清掉「运行期才产生」的文件。
    # 否则本机测试时留下的 config.json（里面记着 lastFolder 等个人数据）会被一起
    # 打包发给用户 —— 用户首次打开就会去扫一个不存在的路径。
    src = os.path.join(ROOT, "dist", APP_NAME)
    for stray in ("config.json", "启动错误.log", "自检报告.json"):
        p = os.path.join(src, stray)
        if os.path.exists(p):
            try:
                os.remove(p)
                log("已清理运行期文件：%s" % stray)
            except OSError as exc:
                log("清理 %s 失败（请手动删除）：%s" % (stray, exc))

    os.makedirs(os.path.join(ROOT, "安装包"), exist_ok=True)
    nsi = os.path.join(ROOT, "installer.nsi")

    # NSIS 3 靠 UTF-8 BOM 识别中文，这里确保带上
    with open(nsi, "rb") as f:
        raw = f.read()
    if not raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
        with io.open(nsi, "w", encoding="utf-8-sig", newline="") as f:
            f.write(text)
        log("已为 installer.nsi 补上 UTF-8 BOM")

    rc, out, err = run([makensis, nsi], cwd=ROOT)
    text = (out + err).decode("gbk", "replace")
    for line in text.splitlines():
        if any(k in line for k in ("Total size", "Error", "warning",
                                   "Can't open")):
            log(line.strip())
    if rc != 0:
        print(text[-2000:])
        return False

    out_file = os.path.join(ROOT, "安装包",
                            "发票批量重命名工具_安装程序_v%s.exe" % VERSION)
    if os.path.exists(out_file):
        log("完成：%s（%.1f MB）"
            % (os.path.basename(out_file), os.path.getsize(out_file) / 1048576))
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe-only", action="store_true")
    ap.add_argument("--setup-only", action="store_true")
    a = ap.parse_args()

    print("=" * 62)
    print("  全电发票批量重命名工具 · 打包脚本")
    print("  版本 %s" % VERSION)
    print("=" * 62)

    if not check_version():
        print("\n版本号不一致，已中止 ❌（宁可停下，也不要装出一个版本号对不上的包）")
        return 1

    ok = True
    if not a.setup_only:
        ok = build_exe() and ok
    if not a.exe_only:
        ok = build_setup() and ok

    print("\n" + ("全部完成 ✅" if ok else "有步骤失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
