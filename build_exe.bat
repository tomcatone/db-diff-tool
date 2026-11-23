@echo off
chcp 65001 >nul
cd /d %~dp0
echo ===== 数据迁移对比工具 - 一键打包 exe =====
where python >nul 2>nul || (echo 未找到 Python，请先安装 Python 3.9+ 并勾选 "Add to PATH" & pause & exit /b 1)
if not exist .venv_build (python -m venv .venv_build)
call .venv_build\Scripts\activate
python -m pip install -U pip
pip install sqlalchemy pymysql psycopg2-binary oracledb pyodbc pyinstaller || (echo 依赖安装失败 & pause & exit /b 1)
python build_exe.py || (echo 打包失败 & pause & exit /b 1)
echo.
echo 打包完成：dist\DBDiff.exe （图形界面）  dist\DBDiff-cli.exe （命令行）
pause
