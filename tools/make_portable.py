# -*- coding: utf-8 -*-
"""
把 dist 里的免安装目录打成便携版 zip（解压即用，不用装）。

为什么单独一个包：
    · 公司电脑没有管理员权限，装不了软件；
    · 装在 U 盘里换电脑用。
注意：便携版和安装版用的是**同一份**程序文件，只是打包方式不同。

用法：
    .venv\\Scripts\\python.exe tools\\make_portable.py
输出：
    安装包\\Invoice-Renaming-Tool-Portable-v{版本}.zip
"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from version import VERSION  # noqa: E402

SRC = os.path.join(ROOT, "dist", "发票批量重命名工具")
OUT_DIR = os.path.join(ROOT, "安装包")

README = """全电发票批量重命名工具 v%s（便携版）
=====================================

这是免安装版：解压到任意文件夹（建议 D:\\Tools\\发票工具），
双击里面的「发票批量重命名工具.exe」即可使用。

⚠️ 重要
1. 必须**先完整解压**再运行，不要用压缩软件打开 zip 直接双击 exe ——
   那样程序只解到临时目录，会提示「解析组件不完整」。
2. 整个文件夹要一起保留，不要只拷 exe —— 旁边的 _internal 是程序本体
   （PyMuPDF、WebView2 等都在里面）。
3. 没有做代码签名，杀毒软件可能提示「未知程序」。
   请把本文件夹加入信任区，或右键 →「仍要运行」。

想装到开始菜单 / 桌面快捷方式，请用安装包版本。

项目主页：https://github.com/tugepaopao/Invoice-Renaming-Tool
""" % VERSION


def main():
    if not os.path.isdir(SRC):
        print("找不到 %s，先跑 tools/build.py" % SRC)
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "Invoice-Renaming-Tool-Portable-v%s.zip" % VERSION)

    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("使用说明（先看这个）.txt", README)
        for base, _dirs, files in os.walk(SRC):
            for f in files:
                p = os.path.join(base, f)
                rel = os.path.relpath(p, SRC)
                z.write(p, os.path.join("发票批量重命名工具", rel))
                n += 1
    size = os.path.getsize(out) / 1024 / 1024
    print("便携版已生成：%s（%d 个文件，%.1f MB）" % (out, n, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
