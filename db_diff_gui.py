#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_diff_gui.py —— 数据迁移对比工具（图形界面版）

使用方法：
    pip install sqlalchemy pymysql psycopg2-binary     # 按需安装驱动
    python db_diff_gui.py

需要与 db_diff.py 放在同一目录。界面仅依赖 Python 自带的 tkinter，无需额外 GUI 库。
（Linux 若提示没有 tkinter：sudo apt install python3-tk）

功能：
  - 源库 / 目标库连接（MySQL、PostgreSQL、Oracle、SQL Server、SQLite、自定义 URL），可“测试连接”
  - 对比范围与规则：包含/排除表、忽略列、业务键、对比模式、大小写、精度等
  - 后台线程执行，实时进度，可随时取消，界面不卡死
  - 结果分三块展示：仅源库的表 / 仅目标库的表 / 相同表（结构 + 数据差异明细）
  - 导出 HTML / JSON / 修复 SQL，复制文本报告
  - 配置可保存/加载（不保存密码），并自动记住上次的设置
"""
import json
import os
import queue
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import db_diff as core
    from sqlalchemy.engine import URL, make_url
except ImportError as _e:  # 友好提示而不是直接崩溃
    _r = tk.Tk(); _r.withdraw()
    messagebox.showerror("缺少依赖", f"{_e}\n\n请确认 db_diff.py 与本文件在同一目录，并已执行：pip install sqlalchemy")
    sys.exit(1)

STATE_FILE = os.path.join(os.path.expanduser("~"), ".db_diff_gui.json")

DB_TYPES = {
    "MySQL / MariaDB": {"driver": "mysql+pymysql", "port": "3306", "query": {"charset": "utf8mb4"}},
    "PostgreSQL": {"driver": "postgresql+psycopg2", "port": "5432"},
    "Oracle": {"driver": "oracle+oracledb", "port": "1521", "service": True},
    "SQL Server": {"driver": "mssql+pyodbc", "port": "1433", "query": {"driver": "ODBC Driver 17 for SQL Server"}},
    "SQLite": {"driver": "sqlite", "file": True},
    "自定义 URL": {"custom": True},
}
MODES = [("完整对比（结构 + 数据）", "full"), ("仅对比表和结构", "schema"), ("数据只比行数（最快）", "count")]


# ----------------------------------------------------------------------------
# 连接面板
# ----------------------------------------------------------------------------
class ConnPanel(ttk.LabelFrame):
    FIELDS = ("type", "host", "port", "user", "database", "schema", "url")

    def __init__(self, master, title, app):
        super().__init__(master, text=title, padding=10)
        self.app = app
        self.v = {k: tk.StringVar() for k in self.FIELDS + ("password",)}
        self.v["type"].set("MySQL / MariaDB")
        self.v["port"].set("3306")
        self.columnconfigure(0, weight=1)

        r = ttk.Frame(self); r.grid(row=0, column=0, sticky="ew", pady=2)
        ttk.Label(r, text="数据库类型", width=11).pack(side="left")
        self.cb = ttk.Combobox(r, textvariable=self.v["type"], values=list(DB_TYPES), state="readonly")
        self.cb.pack(side="left", fill="x", expand=True)
        self.cb.bind("<<ComboboxSelected>>", lambda e: self._on_type())

        # 网络型数据库
        self.f_net = ttk.Frame(self); self.f_net.columnconfigure(1, weight=1); self.f_net.columnconfigure(3, weight=1)
        ttk.Label(self.f_net, text="主机", width=11).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(self.f_net, textvariable=self.v["host"]).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(self.f_net, text="端口").grid(row=0, column=2, padx=(10, 4))
        ttk.Entry(self.f_net, textvariable=self.v["port"], width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(self.f_net, text="用户名", width=11).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(self.f_net, textvariable=self.v["user"]).grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(self.f_net, text="密码").grid(row=1, column=2, padx=(10, 4))
        ttk.Entry(self.f_net, textvariable=self.v["password"], show="*").grid(row=1, column=3, sticky="ew")
        # 数据库名 / 文件
        self.f_db = ttk.Frame(self); self.f_db.columnconfigure(1, weight=1)
        self.lb_db = ttk.Label(self.f_db, text="数据库名", width=11); self.lb_db.grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(self.f_db, textvariable=self.v["database"]).grid(row=0, column=1, sticky="ew", pady=2)
        self.btn_browse = ttk.Button(self.f_db, text="浏览…", width=7, command=self._browse)
        self.btn_browse.grid(row=0, column=2, padx=(6, 0))
        # Schema
        self.f_schema = ttk.Frame(self); self.f_schema.columnconfigure(1, weight=1)
        ttk.Label(self.f_schema, text="Schema", width=11).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(self.f_schema, textvariable=self.v["schema"]).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(self.f_schema, text="可选；PG 默认 public，MySQL 留空即可", foreground="#777").grid(
            row=1, column=1, sticky="w")
        # 自定义 URL
        self.f_url = ttk.Frame(self); self.f_url.columnconfigure(1, weight=1)
        ttk.Label(self.f_url, text="连接 URL", width=11).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(self.f_url, textvariable=self.v["url"]).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(self.f_url, text="如 mysql+pymysql://user:pwd@host:3306/db", foreground="#777").grid(
            row=1, column=1, sticky="w")
        # 测试连接
        self.f_test = ttk.Frame(self)
        self.btn_test = ttk.Button(self.f_test, text="测试连接", command=self.test)
        self.btn_test.pack(side="left")
        self.lb_status = ttk.Label(self.f_test, text="", wraplength=360, justify="left")
        self.lb_status.pack(side="left", padx=8, fill="x", expand=True)
        self._on_type(reset_port=False)

    # ---- 显示/隐藏 ----
    def _on_type(self, reset_port=True):
        t = DB_TYPES[self.v["type"].get()]
        for f in (self.f_net, self.f_db, self.f_schema, self.f_url, self.f_test):
            f.grid_remove()
        row = 1
        if t.get("custom"):
            self.f_url.grid(row=row, column=0, sticky="ew"); row += 1
        else:
            if not t.get("file"):
                self.f_net.grid(row=row, column=0, sticky="ew"); row += 1
                if reset_port and t.get("port"):
                    self.v["port"].set(t["port"])
            self.lb_db.config(text="SQLite 文件" if t.get("file") else ("服务名" if t.get("service") else "数据库名"))
            self.btn_browse.grid() if t.get("file") else self.btn_browse.grid_remove()
            self.f_db.grid(row=row, column=0, sticky="ew"); row += 1
            if not t.get("file"):
                self.f_schema.grid(row=row, column=0, sticky="ew"); row += 1
        if t.get("custom"):
            self.f_schema.grid(row=row, column=0, sticky="ew"); row += 1
        self.f_test.grid(row=row, column=0, sticky="ew", pady=(6, 0))

    def _browse(self):
        p = filedialog.askopenfilename(title="选择 SQLite 数据库文件",
                                       filetypes=[("SQLite", "*.db *.sqlite *.sqlite3 *.db3"), ("所有文件", "*.*")])
        if p:
            self.v["database"].set(p)

    # ---- 取值 ----
    def url(self) -> str:
        v = {k: x.get().strip() for k, x in self.v.items() if k != "password"}
        t = DB_TYPES[v["type"]]
        if t.get("custom"):
            if not v["url"]:
                raise ValueError("请填写连接 URL")
            make_url(v["url"])  # 格式校验
            return v["url"]
        if t.get("file"):
            if not v["database"]:
                raise ValueError("请选择 SQLite 文件")
            if not os.path.isfile(v["database"]):
                raise ValueError(f"SQLite 文件不存在：{v['database']}")
            return URL.create("sqlite", database=v["database"]).render_as_string(hide_password=False)
        if not v["host"]:
            raise ValueError("请填写主机")
        try:
            port = int(v["port"]) if v["port"] else None
        except ValueError:
            raise ValueError("端口必须是数字")
        query, db = dict(t.get("query", {})), v["database"] or None
        if t.get("service") and db:
            query["service_name"], db = db, None
        return URL.create(t["driver"], username=v["user"] or None, password=self.v["password"].get() or None,
                          host=v["host"], port=port, database=db, query=query).render_as_string(hide_password=False)

    def schema(self):
        return self.v["schema"].get().strip() or None

    def to_dict(self) -> dict:  # 不保存密码
        d = {k: self.v[k].get() for k in self.FIELDS}
        if d["url"]:
            try:
                d["url"] = make_url(d["url"]).set(password=None).render_as_string(hide_password=False)
            except Exception:
                d["url"] = ""
        return d

    def from_dict(self, d: dict):
        for k in self.FIELDS:
            if k in d:
                self.v[k].set(d[k])
        if self.v["type"].get() not in DB_TYPES:
            self.v["type"].set("MySQL / MariaDB")
        self._on_type(reset_port=False)

    # ---- 测试连接 ----
    def test(self):
        try:
            url = self.url()
        except ValueError as e:
            self.show_status(str(e), ok=False)
            return
        schema = self.schema()
        self.btn_test.config(state="disabled")
        self.show_status("正在连接…", ok=None)

        def work():
            ok, msg = core.test_connection(url, schema)
            self.app.q.put(("conn", {"panel": self, "ok": ok, "msg": msg}))
        threading.Thread(target=work, daemon=True).start()

    def show_status(self, msg, ok):
        self.lb_status.config(text=msg, foreground={True: "#1a7f37", False: "#cf222e", None: "#555"}[ok])
        if ok is not None:
            self.btn_test.config(state="normal")


# ----------------------------------------------------------------------------
# 主窗口
# ----------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("数据迁移对比工具  —  源库 vs 目标库")
        self.geometry("1160x800")
        self.minsize(1000, 680)
        self.q = queue.Queue()
        self.rep = None
        self.running = False
        self.worker = None
        self.last_html = None
        self._setup_style()
        self._build()
        self._load_state()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = self._on_tk_error

    # ---------------- 样式 ----------------
    def _setup_style(self):
        st = ttk.Style(self)
        for name in ("vista", "aqua", "clam"):
            if name in st.theme_names():
                st.theme_use(name)
                break
        base = tkfont.nametofont("TkDefaultFont")
        fam, size = base.actual("family"), base.actual("size")
        st.configure("Treeview", rowheight=max(22, int(base.metrics("linespace") * 1.5)))
        st.configure("Big.TLabel", font=(fam, abs(size) + 3, "bold"))
        st.configure("Go.TButton", font=(fam, abs(size) + 1, "bold"), padding=(18, 6))
        self.mono = tkfont.nametofont("TkFixedFont").copy()
        self.mono.configure(size=abs(size))

    # ---------------- 界面 ----------------
    def _build(self):
        self._build_actionbar()  # 必须先 pack（side=bottom），否则窗口较小时会被 Notebook 挤出可视区
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self.tab_cfg, self.tab_res, self.tab_log = (ttk.Frame(self.nb, padding=8) for _ in range(3))
        self.nb.add(self.tab_cfg, text="  ① 连接与选项  ")
        self.nb.add(self.tab_res, text="  ② 对比结果  ")
        self.nb.add(self.tab_log, text="  ③ 运行日志  ")
        self._build_cfg(self.tab_cfg)
        self._build_result(self.tab_res)
        self._build_log(self.tab_log)

    def _build_cfg(self, tab):
        tab.columnconfigure(0, weight=1, uniform="p"); tab.columnconfigure(1, weight=1, uniform="p")
        self.src = ConnPanel(tab, "源库（迁移前 / 基准）", self)
        self.tgt = ConnPanel(tab, "目标库（迁移后 / 待校验）", self)
        self.src.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.tgt.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        o = {k: tk.StringVar() for k in ("include", "exclude", "ignore", "key", "html", "json", "fix",
                                         "sample", "chunk", "floatp")}
        o.update(mode=tk.StringVar(value="full"), case=tk.BooleanVar(), notrim=tk.BooleanVar(),
                 withdel=tk.BooleanVar(), autoopen=tk.BooleanVar(value=True))
        o["sample"].set("20"); o["chunk"].set("5000"); o["floatp"].set("6")
        self.o = o

        sc = ttk.LabelFrame(tab, text="对比范围与规则", padding=10)
        sc.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        for c in (1, 3):
            sc.columnconfigure(c, weight=1)
        rows = [("包含的表", "include", "通配符，逗号分隔；留空=全部", "排除的表", "exclude", "如 tmp_*,*_bak"),
                ("忽略的列", "ignore", "如 *.update_time,user.last_login", "业务键", "key",
                 "无主键表用：orders=order_no; items=a,b")]
        for i, (l1, k1, h1, l2, k2, h2) in enumerate(rows):
            ttk.Label(sc, text=l1).grid(row=i * 2, column=0, sticky="w", pady=(4, 0))
            ttk.Entry(sc, textvariable=o[k1]).grid(row=i * 2, column=1, sticky="ew", padx=(6, 14), pady=(4, 0))
            ttk.Label(sc, text=l2).grid(row=i * 2, column=2, sticky="w", pady=(4, 0))
            ttk.Entry(sc, textvariable=o[k2]).grid(row=i * 2, column=3, sticky="ew", padx=(6, 0), pady=(4, 0))
            ttk.Label(sc, text=h1, foreground="#777").grid(row=i * 2 + 1, column=1, sticky="w", padx=(6, 0))
            ttk.Label(sc, text=h2, foreground="#777").grid(row=i * 2 + 1, column=3, sticky="w", padx=(6, 0))
        m = ttk.Frame(sc); m.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(m, text="对比模式").pack(side="left", padx=(0, 8))
        for text, val in MODES:
            ttk.Radiobutton(m, text=text, value=val, variable=o["mode"]).pack(side="left", padx=(0, 12))
        a = ttk.Frame(sc); a.grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Checkbutton(a, text="区分表名/列名大小写", variable=o["case"]).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(a, text="不去除字符串尾部空格", variable=o["notrim"]).pack(side="left", padx=(0, 14))
        for text, key, lo, hi in (("样本行数", "sample", 0, 1000), ("读取批大小", "chunk", 100, 200000),
                                  ("浮点保留位数", "floatp", 0, 15)):
            ttk.Label(a, text=text).pack(side="left", padx=(10, 4))
            ttk.Spinbox(a, from_=lo, to=hi, width=7, textvariable=o[key]).pack(side="left")

        out = ttk.LabelFrame(tab, text="输出（留空表示不生成）", padding=10)
        out.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        out.columnconfigure(1, weight=1)
        for i, (text, key, ext, types) in enumerate((
                ("HTML 报告", "html", ".html", [("HTML", "*.html")]),
                ("JSON 报告", "json", ".json", [("JSON", "*.json")]),
                ("修复 SQL", "fix", ".sql", [("SQL", "*.sql")]))):
            ttk.Label(out, text=text, width=10).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(out, textvariable=o[key]).grid(row=i, column=1, sticky="ew", padx=6, pady=2)
            ttk.Button(out, text="浏览…", width=7,
                       command=lambda k=key, e=ext, t=types: self._pick_save(k, e, t)).grid(row=i, column=2, pady=2)
        ttk.Checkbutton(out, text="修复 SQL 启用 DELETE（默认注释）", variable=o["withdel"]).grid(
            row=2, column=3, padx=(14, 0), sticky="w")
        ttk.Checkbutton(out, text="完成后自动打开 HTML 报告", variable=o["autoopen"]).grid(
            row=0, column=3, padx=(14, 0), sticky="w")

    def _pick_save(self, key, ext, types):
        p = filedialog.asksaveasfilename(defaultextension=ext, filetypes=types + [("所有文件", "*.*")])
        if p:
            self.o[key].set(p)

    def _make_tree(self, parent, cols, heads, widths, height=6, anchors=None):
        fr = ttk.Frame(parent)
        tree = ttk.Treeview(fr, columns=cols, show="headings", height=height, selectmode="browse")
        sb = ttk.Scrollbar(fr, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        for i, (c, h, w) in enumerate(zip(cols, heads, widths)):
            tree.heading(c, text=h, command=lambda c=c, t=tree: self._sort(t, c))
            tree.column(c, width=w, minwidth=40, anchor=(anchors or {}).get(c, "w"), stretch=(i == 0))
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return fr, tree

    def _build_result(self, tab):
        top = ttk.Frame(tab); top.pack(fill="x")
        self.verdict = ttk.Label(top, text="尚未运行对比", style="Big.TLabel")
        self.verdict.pack(side="left")
        for text, cmd in (("复制文本报告", self._copy_text), ("导出 JSON…", self._export_json),
                          ("导出 HTML…", self._export_html), ("在浏览器打开 HTML", self._open_html)):
            ttk.Button(top, text=text, command=cmd).pack(side="right", padx=(6, 0))
        self.summary = ttk.Label(tab, text="", foreground="#444")
        self.summary.pack(fill="x", pady=(2, 6))

        pw = ttk.PanedWindow(tab, orient="vertical")
        pw.pack(fill="both", expand=True)

        f1 = ttk.Frame(pw); f1.columnconfigure(0, weight=1, uniform="o"); f1.columnconfigure(1, weight=1, uniform="o")
        f1.rowconfigure(0, weight=1)
        a = ttk.LabelFrame(f1, text="仅在源库的表（迁移遗漏）", padding=4); a.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        b = ttk.LabelFrame(f1, text="仅在目标库的表（多余/新增）", padding=4); b.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        fr, self.t_only_s = self._make_tree(a, ("t", "n"), ("表名", "行数"), (260, 80), 4, {"n": "e"}); fr.pack(fill="both", expand=True)
        fr, self.t_only_t = self._make_tree(b, ("t", "n"), ("表名", "行数"), (260, 80), 4, {"n": "e"}); fr.pack(fill="both", expand=True)
        pw.add(f1, weight=1)

        f2 = ttk.LabelFrame(pw, text="两库共有的表", padding=4)
        bar = ttk.Frame(f2); bar.pack(fill="x")
        self.only_diff = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="只显示有差异的表", variable=self.only_diff, command=self._fill_common).pack(side="left")
        ttk.Label(bar, text="（点击表头排序；选中一行在下方查看明细）", foreground="#777").pack(side="left", padx=10)
        cols = ("t", "st", "rows", "miss", "extra", "mod", "sch")
        fr, self.t_common = self._make_tree(
            f2, cols, ("表名", "结论", "行数(源/目标)", "缺失", "多余", "不一致", "结构差异项"),
            (240, 110, 120, 70, 70, 70, 90), 8,
            {"rows": "e", "miss": "e", "extra": "e", "mod": "e", "sch": "e"})
        fr.pack(fill="both", expand=True)
        self.t_common.tag_configure("ok", foreground="#1a7f37")
        self.t_common.tag_configure("bad", foreground="#cf222e")
        self.t_common.tag_configure("err", foreground="#b45309")
        self.t_common.bind("<<TreeviewSelect>>", self._show_detail)
        pw.add(f2, weight=3)

        f3 = ttk.LabelFrame(pw, text="差异明细", padding=4)
        self.detail = tk.Text(f3, height=8, wrap="none", font=self.mono, state="disabled")
        ys = ttk.Scrollbar(f3, orient="vertical", command=self.detail.yview)
        xs = ttk.Scrollbar(f3, orient="horizontal", command=self.detail.xview)
        self.detail.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y"); xs.pack(side="bottom", fill="x"); self.detail.pack(fill="both", expand=True)
        for tag, color in (("title", "#000"), ("schema", "#0b5cad"), ("data", "#6f42c1"), ("warn", "#b45309"),
                           ("err", "#cf222e"), ("okc", "#1a7f37")):
            self.detail.tag_configure(tag, foreground=color)
        self.detail.tag_configure("title", font=(self.mono.actual("family"), self.mono.actual("size"), "bold"))
        pw.add(f3, weight=3)

    def _build_log(self, tab):
        self.log_text = tk.Text(tab, wrap="word", font=self.mono, state="disabled")
        sb = ttk.Scrollbar(tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.log_text.pack(fill="both", expand=True)

    def _build_actionbar(self):
        bar = ttk.Frame(self, padding=(10, 8)); bar.pack(side="bottom", fill="x")
        self.btn_go = ttk.Button(bar, text="▶ 开始对比", style="Go.TButton", command=self.start)
        self.btn_go.pack(side="left")
        self.btn_cancel = ttk.Button(bar, text="取消", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(8, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Button(bar, text="加载演示库", command=self.load_demo).pack(side="left")
        ttk.Button(bar, text="保存配置…", command=self.save_cfg).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="加载配置…", command=self.load_cfg).pack(side="left", padx=(8, 0))
        right = ttk.Frame(bar); right.pack(side="left", fill="x", expand=True, padx=(16, 0))
        self.status = tk.StringVar(value="就绪")
        ttk.Label(right, textvariable=self.status).pack(anchor="w")
        self.pb = ttk.Progressbar(right, mode="determinate")
        self.pb.pack(fill="x", pady=(2, 0))

    # ---------------- 工具 ----------------
    def log(self, text):
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _sort(self, tree, col, reverse=False):
        def key(k):
            v = tree.set(k, col)
            try:
                return (0, float(v.split("/")[0].replace(",", "")))
            except ValueError:
                return (1, v.lower())
        items = sorted(tree.get_children(""), key=key, reverse=reverse)
        for i, k in enumerate(items):
            tree.move(k, "", i)
        tree.heading(col, command=lambda: self._sort(tree, col, not reverse))

    def _on_tk_error(self, exc, val, tb):
        msg = "".join(traceback.format_exception(exc, val, tb))
        self.log("界面异常：\n" + msg)
        messagebox.showerror("程序异常", f"{val}\n\n详细信息已写入“运行日志”页。")

    # ---------------- 运行 ----------------
    def collect_args(self):
        o = self.o

        def num(key, name, lo, hi):
            try:
                v = int(o[key].get())
            except ValueError:
                raise ValueError(f"“{name}”必须是整数")
            if not lo <= v <= hi:
                raise ValueError(f"“{name}”范围应为 {lo} ~ {hi}")
            return v

        src, tgt = self.src.url(), self.tgt.url()
        if src == tgt and self.src.schema() == self.tgt.schema():
            raise ValueError("源库和目标库完全相同，没有对比意义")
        paths = {k: o[k].get().strip() or None for k in ("html", "json", "fix")}
        for k, p in paths.items():
            if p and not os.path.isdir(os.path.dirname(os.path.abspath(p))):
                raise ValueError(f"输出目录不存在：{os.path.dirname(os.path.abspath(p))}")
        mode = o["mode"].get()
        f = lambda k: [o[k].get()] if o[k].get().strip() else None
        return core.default_args(
            source=src, target=tgt, src_schema=self.src.schema(), tgt_schema=self.tgt.schema(),
            include=f("include"), exclude=f("exclude"), ignore_columns=f("ignore"), key=f("key"),
            schema_only=(mode == "schema"), count_only=(mode == "count"),
            case_sensitive=o["case"].get(), no_trim=o["notrim"].get(),
            sample=num("sample", "样本行数", 0, 1000), chunk=num("chunk", "读取批大小", 100, 200000),
            float_places=num("floatp", "浮点保留位数", 0, 15),
            json=paths["json"], html=paths["html"], fix_sql=paths["fix"], with_delete=o["withdel"].get())

    def start(self):
        if self.running:
            return
        try:
            args = self.collect_args()
        except ValueError as e:
            messagebox.showwarning("参数有误", str(e))
            return
        self.running = True
        self.btn_go.config(state="disabled"); self.btn_cancel.config(state="normal")
        self.status.set("正在连接数据库 …")
        self.pb.config(mode="indeterminate"); self.pb.start(12)
        self.log("=" * 50)
        self.log(f"开始对比  模式={self.o['mode'].get()}")
        self.worker = threading.Thread(target=self._work, args=(args,), daemon=True)
        self.worker.start()

    def _work(self, args):
        put = self.q.put
        try:
            rep = core.run(args, progress=lambda kind, **k: put((kind, k)))
            try:
                core.save_reports(rep, args.json, args.html)
            except Exception as e:
                put(("log", {"text": f"报告文件保存失败：{core.friendly_error(e)}"}))
            put(("done", {"rep": rep, "args": args}))
        except core.Cancelled:
            put(("cancelled", {}))
        except Exception as e:
            put(("error", {"msg": core.friendly_error(e), "tb": traceback.format_exc()}))

    def cancel(self):
        if self.running:
            core.CANCEL.set()
            self.status.set("正在取消 …（当前批次处理完即停止）")
            self.btn_cancel.config(state="disabled")

    def _finish_run(self):
        self.running = False
        self.pb.stop(); self.pb.config(mode="determinate", value=0)
        self.btn_go.config(state="normal"); self.btn_cancel.config(state="disabled")

    def _poll(self):
        try:
            while True:
                kind, k = self.q.get_nowait()
                getattr(self, "_on_" + kind)(**k)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---- 队列事件 ----
    def _on_stage(self, text):
        self.status.set(text); self.log(text)

    def _on_table(self, index, total, name):
        if str(self.pb.cget("mode")) == "indeterminate":
            self.pb.stop(); self.pb.config(mode="determinate")
        self.pb.config(maximum=total, value=index - 1)
        self.status.set(f"正在对比 {name}   ({index}/{total})")

    def _on_table_done(self, index, total, name, status, seconds):
        self.pb.config(value=index)
        self.log(f"[{index}/{total}] {name}  {status}  ({seconds:.1f}s)")

    def _on_log(self, text):
        self.log(text)

    def _on_conn(self, panel, ok, msg):
        panel.show_status(("✔ " if ok else "✘ ") + msg, ok)

    def _on_cancelled(self):
        self._finish_run()
        self.status.set("已取消")
        self.log("已取消。")

    def _on_error(self, msg, tb):
        self._finish_run()
        self.status.set("对比失败")
        self.log("对比失败：\n" + tb)
        messagebox.showerror("对比失败", msg)

    def _on_done(self, rep, args):
        self._finish_run()
        self.show_results(rep)
        s = rep["summary"]
        self.status.set(f"完成，用时 {rep['meta']['elapsed']}s")
        self.log(f"完成：共有 {s['common_tables']} 张表，{s['common_different']} 张有差异；"
                 f"仅源库 {s['only_in_source']}，仅目标库 {s['only_in_target']}；用时 {rep['meta']['elapsed']}s")
        for label, p in (("HTML 报告", args.html), ("JSON 报告", args.json), ("修复 SQL", args.fix_sql)):
            if p:
                self.log(f"{label}: {p}")
        self.last_html = args.html
        self.nb.select(self.tab_res)
        if args.html and self.o["autoopen"].get():
            self._open_html()

    # ---------------- 结果展示 ----------------
    def show_results(self, rep):
        self.rep = rep
        s = rep["summary"]
        same = not (s["only_in_source"] or s["only_in_target"] or s["common_different"])
        self.verdict.config(text="【完全一致】两库的表、结构、数据均无差异" if same else
                            f"【存在差异】{s['common_different']} 张共有表有差异 · "
                            f"仅源库 {s['only_in_source']} 张 · 仅目标库 {s['only_in_target']} 张",
                            foreground="#1a7f37" if same else "#cf222e")
        self.summary.config(text=f"源库 {s['source_tables']} 张表 | 目标库 {s['target_tables']} 张表 | "
                                 f"共有 {s['common_tables']} 张（一致 {s['common_identical']}，差异 {s['common_different']}，"
                                 f"出错 {s['errors']}） | 模式 {rep['meta']['mode']} | 耗时 {rep['meta']['elapsed']}s")
        for tree, key in ((self.t_only_s, "only_in_source"), (self.t_only_t, "only_in_target")):
            tree.delete(*tree.get_children())
            for t in rep[key]:
                tree.insert("", "end", values=(t["table"], "未知" if t["rows"] is None else f"{t['rows']:,}"))
        self._fill_common()
        self._set_detail([])

    def _fill_common(self):
        tree = self.t_common
        tree.delete(*tree.get_children())
        if not self.rep:
            return
        for i, e in enumerate(self.rep["common"]):
            if self.only_diff.get() and e["status"] == "一致":
                continue
            d = e["data"]
            tag = "ok" if e["status"] == "一致" else ("err" if e["error"] else "bad")
            tree.insert("", "end", iid=str(i), tags=(tag,), values=(
                e["table"], e["status"], f"{d['rows_source']:,}/{d['rows_target']:,}" if d else "-",
                d["missing_in_target"] if d else "-", d["extra_in_target"] if d else "-",
                d["modified"] if d else "-", core.schema_diff_count(e["schema"])))

    def _show_detail(self, _=None):
        sel = self.t_common.selection()
        if sel and self.rep:
            self._set_detail(core.entry_detail_lines(self.rep["common"][int(sel[0])]))

    def _set_detail(self, lines):
        t = self.detail
        t.config(state="normal"); t.delete("1.0", "end")
        for i, line in enumerate(lines):
            s = line.strip()
            tag = ("title" if i == 0 else "schema" if s.startswith("[结构]") else "data" if s.startswith("[数据]")
                   else "warn" if s.startswith("[提示]") else "err" if s.startswith("错误") else
                   "okc" if "无差异" in s else None)
            t.insert("end", line + "\n", tag or ())
        t.config(state="disabled")

    # ---------------- 导出 ----------------
    def _need_rep(self):
        if not self.rep:
            messagebox.showinfo("提示", "请先运行一次对比。")
            return False
        return True

    def _export_html(self):
        if self._need_rep():
            p = filedialog.asksaveasfilename(defaultextension=".html", filetypes=[("HTML", "*.html")])
            if p:
                core.save_reports(self.rep, None, p); self.last_html = p; self.log(f"已导出 HTML: {p}")

    def _export_json(self):
        if self._need_rep():
            p = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
            if p:
                core.save_reports(self.rep, p, None); self.log(f"已导出 JSON: {p}")

    def _copy_text(self):
        if self._need_rep():
            self.clipboard_clear(); self.clipboard_append(core.console_text(self.rep))
            self.status.set("文本报告已复制到剪贴板")

    def _open_html(self):
        if not self._need_rep():
            return
        p = self.last_html
        if not p or not os.path.isfile(p):  # 没有现成文件就生成临时文件
            p = os.path.join(tempfile.gettempdir(), "db_diff_report.html")
            core.save_reports(self.rep, None, p)
        webbrowser.open("file:///" + os.path.abspath(p).replace("\\", "/").lstrip("/"))

    # ---------------- 配置 ----------------
    def get_state(self):
        return {"src": self.src.to_dict(), "tgt": self.tgt.to_dict(), "opts": {k: v.get() for k, v in self.o.items()}}

    def set_state(self, d):
        self.src.from_dict(d.get("src", {})); self.tgt.from_dict(d.get("tgt", {}))
        for k, v in d.get("opts", {}).items():
            if k in self.o:
                try:
                    self.o[k].set(v)
                except tk.TclError:
                    pass

    def save_cfg(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("配置文件", "*.json")],
                                         initialfile="db_diff_config.json")
        if p:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self.get_state(), f, ensure_ascii=False, indent=2)
            messagebox.showinfo("已保存", "配置已保存（出于安全，不包含密码）。")

    def load_cfg(self):
        p = filedialog.askopenfilename(filetypes=[("配置文件", "*.json"), ("所有文件", "*.*")])
        if p:
            try:
                with open(p, encoding="utf-8") as f:
                    self.set_state(json.load(f))
            except Exception as e:
                messagebox.showerror("加载失败", str(e))

    def _load_state(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                self.set_state(json.load(f))
        except Exception:
            pass

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.get_state(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def load_demo(self):
        s_url, t_url, d = core.build_demo()
        for panel, url in ((self.src, s_url), (self.tgt, t_url)):
            panel.v["type"].set("SQLite"); panel._on_type(reset_port=False)
            panel.v["database"].set(make_url(url).database)
            panel.v["schema"].set("")
        self.o["ignore"].set("users.created_at")
        self.o["html"].set(os.path.join(d, "report.html"))
        self.o["fix"].set(os.path.join(d, "fix.sql"))
        self.o["autoopen"].set(False)
        self.log(f"已加载演示库：{d}")
        self.status.set("已加载演示库，点击“开始对比”")

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno("确认退出", "对比仍在进行，确定要取消并退出吗？"):
                return
            core.CANCEL.set()
        self._save_state()
        self.destroy()


def main():
    if sys.platform.startswith("win"):  # 高 DPI 下避免界面发虚
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
