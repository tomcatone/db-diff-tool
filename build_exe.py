#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_exe.py —— 一键打包：生成 DBDiff.exe（图形界面）和 DBDiff-cli.exe（命令行）

用法（在 Windows 上，已安装 Python 3.9+）：
    双击 build_exe.bat          # 推荐，自动建虚拟环境并安装依赖
或  python build_exe.py         # 已手动 pip install pyinstaller sqlalchemy 及驱动后

产物在 dist/ 目录。PyInstaller 不能跨平台打包：要 exe 必须在 Windows 上运行本脚本（或用 GitHub Actions）。
可选：把 icon.ico 放在同目录，会自动作为程序图标。
"""
import importlib.util
import os
import sys

try:
    import PyInstaller.__main__ as pyi
except ImportError:
    sys.exit("未安装 PyInstaller，请先执行：pip install pyinstaller")

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

# 已安装的数据库驱动才会被打进去（缺哪个就跳过哪个，不会让打包失败）
DRIVERS = ["pymysql", "psycopg2", "oracledb", "pyodbc"]
present = [d for d in DRIVERS if importlib.util.find_spec(d)]
missing = [d for d in DRIVERS if d not in present]
print("打入的驱动:", present or "无")
if missing:
    print("未安装（对应数据库在 exe 里将不可用）:", missing)

# ---- 定位源文件：容忍下载后改名（"db diff gui.py" / "db_diff_gui (1).py" 等），统一复制为标准名再打包 ----
import re
import shutil


def _norm(fn):
    b = os.path.splitext(fn)[0].lower()
    b = re.sub(r"\s*\(\d+\)$", "", b)          # 去掉浏览器加的 " (1)"
    return re.sub(r"[\s\-]+", "_", b)             # 空格/连字符 -> 下划线


def find_source(canon):
    files = [f for f in os.listdir(HERE) if f.lower().endswith(".py")]
    if canon + ".py" in files:
        return canon + ".py"
    return next((f for f in files if _norm(f) == canon), None)


STAGE = os.path.join(HERE, "_build_src")
shutil.rmtree(STAGE, ignore_errors=True)
os.makedirs(STAGE)
for canon in ("db_diff", "db_diff_gui"):
    src = find_source(canon)
    if not src:
        print(f"\n错误：在 {HERE} 里找不到 {canon}.py")
        print("该文件夹中的 .py 文件有：", [f for f in os.listdir(HERE) if f.lower().endswith(".py")] or "无")
        sys.exit("请把 db_diff.py 和 db_diff_gui.py 与 build_exe.py 放在【同一个文件夹】后重试。")
    shutil.copy(os.path.join(HERE, src), os.path.join(STAGE, canon + ".py"))
    print(f"源文件: {src}" + ("" if src == canon + ".py" else f"  (已识别并按 {canon}.py 处理)"))

common = ["--noconfirm", "--clean", "--onefile", "--collect-submodules", "sqlalchemy.dialects"]
for d in present:
    common += ["--hidden-import", d]
if os.path.isfile("icon.ico"):
    common += ["--icon", "icon.ico"]


def build(script, name, windowed):
    args = common + ["--name", name, "--windowed" if windowed else "--console", script]
    print("\n>>> PyInstaller", " ".join(args))
    pyi.run(args)


build(os.path.join(STAGE, "db_diff_gui.py"), "DBDiff", windowed=True)      # 双击运行，无黑色控制台窗口
build(os.path.join(STAGE, "db_diff.py"), "DBDiff-cli", windowed=False)     # 命令行版，可用于脚本/CI
ext = ".exe" if sys.platform.startswith("win") else ""
shutil.rmtree(STAGE, ignore_errors=True)
print(f"\n完成：dist/DBDiff{ext}（GUI）  dist/DBDiff-cli{ext}（命令行）")
