#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_diff.py —— 数据迁移对比工具核心（命令行 + 供 GUI 调用）

一次运行完成三层对比：
  1. 表级    ：仅在源库的表（迁移遗漏） / 仅在目标库的表（多余） / 两库共有的表
  2. 结构级  ：共有表的 列 / 类型 / 长度精度 / 可空 / 主键 / 索引 / 外键 差异
  3. 数据级  ：共有表的 行数 / 缺失行 / 多余行 / 内容不一致行（精确到列）
可选输出：控制台摘要、JSON、HTML 报告、以及让“目标库 == 源库”的修复 SQL。

图形界面请运行 db_diff_gui.py（与本文件放在同一目录）。

依赖：pip install sqlalchemy  （再按需安装驱动：pymysql / psycopg2-binary / oracledb / pyodbc ...）

命令行示例：
  python db_diff.py \
     --source "mysql+pymysql://u:p@10.0.0.1:3306/olddb?charset=utf8mb4" \
     --target "postgresql+psycopg2://u:p@10.0.0.2:5432/newdb" \
     --src-schema olddb --tgt-schema public \
     --exclude "tmp_*,*_bak" --ignore-columns "*.update_time" \
     --html report.html --json report.json --fix-sql fix.sql

  python db_diff.py --demo        # 不用连库，自动生成两个 SQLite 示例库跑一遍

数据对比原理（内存占用 = 每行约 60~100 字节，而不是整行数据）：
  - 两库各流式扫描一遍，每行先“归一化”再哈希（跨库类型差异如 Decimal/float/时间/CHAR 补空格 不会误报）
  - 有主键（或 --key 指定的业务键）：{键 -> 行哈希} 比较，得到 缺失 / 多余 / 不一致
    若发现键不唯一，自动降级为整行哈希比较（避免误导性结果）
  - 无主键：整行哈希做多重集合比较（能发现数量差异，无法区分“修改”与“删+增”）
  - 仅在存在差异时再回扫一次，取样本行与生成修复 SQL
"""
from __future__ import annotations

import argparse
import collections
import fnmatch
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import threading
import time
import unicodedata
import warnings
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import MetaData, Table, create_engine, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SAWarning

warnings.filterwarnings("ignore", category=SAWarning)  # 未识别的列类型警告对对比无影响

NULL = "\x00NULL"


class Cancelled(Exception):
    """用户取消对比。"""


CANCEL = threading.Event()  # GUI 通过 CANCEL.set() 请求取消


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled()


# ----------------------------------------------------------------------------
# 类型归一：不同数据库的同族类型视为一致（INT/BIGINT/TINYINT -> INTEGER 等）
# ----------------------------------------------------------------------------
_FAMILIES = {
    "INTEGER": "INT INTEGER SMALLINT BIGINT TINYINT MEDIUMINT SERIAL BIGSERIAL SMALLSERIAL BOOLEAN BOOL BIT",
    "DECIMAL": "NUMERIC DECIMAL NUMBER MONEY",
    "FLOAT": "FLOAT DOUBLE REAL DOUBLE_PRECISION BINARY_FLOAT BINARY_DOUBLE",
    "STRING": "VARCHAR CHAR NVARCHAR NCHAR VARCHAR2 NVARCHAR2 STRING CHARACTER TEXT TINYTEXT "
              "MEDIUMTEXT LONGTEXT CLOB NCLOB ENUM UNICODE UNICODETEXT",
    "DATETIME": "DATETIME TIMESTAMP DATETIME2 SMALLDATETIME TIMESTAMPTZ",
    "DATE": "DATE",
    "TIME": "TIME TIMETZ",
    "BINARY": "BLOB TINYBLOB MEDIUMBLOB LONGBLOB BYTEA BINARY VARBINARY LARGEBINARY RAW",
    "JSON": "JSON JSONB",
    "UUID": "UUID UNIQUEIDENTIFIER",
}
TYPE_FAMILY = {n: fam for fam, names in _FAMILIES.items() for n in names.split()}


def type_family(t) -> str:
    name = type(t).__name__.upper()
    return TYPE_FAMILY.get(name, name)


def type_str(t) -> str:
    try:
        return str(t)
    except Exception:  # 某些方言类型无法被默认方言编译
        return type(t).__name__


# ----------------------------------------------------------------------------
# 友好的错误信息
# ----------------------------------------------------------------------------
_PIP = {"pymysql": "pymysql", "MySQLdb": "mysqlclient", "psycopg2": "psycopg2-binary", "psycopg": "psycopg[binary]",
        "oracledb": "oracledb", "cx_Oracle": "cx_Oracle", "pyodbc": "pyodbc", "pymssql": "pymssql"}


def friendly_error(e: BaseException) -> str:
    if isinstance(e, ModuleNotFoundError):
        return f"缺少数据库驱动模块：{e.name}\n请先执行：pip install {_PIP.get(e.name, e.name)}"
    msg = str(getattr(e, "orig", None) or e)
    return f"{type(e).__name__}: {msg}"[:1000]


def test_connection(url: str, schema: str | None = None):
    """返回 (ok, 说明)。"""
    eng = None
    try:
        eng = create_engine(url)
        with eng.connect():
            pass
        n = len(inspect(eng).get_table_names(schema=schema))
        return True, f"连接成功（{'Schema ' + schema if schema else '默认库/Schema'}下共 {n} 张表）"
    except Exception as e:
        return False, friendly_error(e)
    finally:
        if eng is not None:
            eng.dispose()


# ----------------------------------------------------------------------------
# 值归一化 + 行哈希
# ----------------------------------------------------------------------------
class Normalizer:
    def __init__(self, float_places: int = 6, trim: bool = True):
        self.fp = float_places
        self.trim = trim

    @staticmethod
    def _dec(d: Decimal) -> str:
        if not d.is_finite():
            return str(d)
        s = format(d.normalize(), "f")
        return "0" if s == "-0" else s

    def __call__(self, v) -> str:
        if v is None:
            return NULL
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, Decimal):
            return self._dec(v)
        if isinstance(v, float):
            if v != v or v in (float("inf"), float("-inf")):
                return repr(v)
            return self._dec(Decimal(repr(round(v, self.fp))))
        if isinstance(v, datetime):
            if v.tzinfo is not None:
                v = v.astimezone(timezone.utc).replace(tzinfo=None)
            if v.hour == v.minute == v.second == v.microsecond == 0:
                return v.date().isoformat()  # DATE 迁成零点 DATETIME 不算差异
            return v.isoformat(sep=" ")
        if isinstance(v, date):
            return v.isoformat()
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v).hex()
        if isinstance(v, (dict, list)):
            return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if isinstance(v, str):
            return v.rstrip(" ") if self.trim else v
        return str(v)


def digest(values) -> bytes:
    # 带长度前缀，避免 ["ab","c"] 与 ["a","bc"] 拼接后碰撞
    data = "".join(f"{len(s)}:{s}" for s in values)
    return hashlib.blake2b(data.encode("utf-8", "surrogatepass"), digest_size=12).digest()


def show(v, limit=80) -> str:
    s = "NULL" if v is None else (bytes(v).hex() if isinstance(v, (bytes, bytearray, memoryview)) else str(v))
    return s if len(s) <= limit else s[:limit] + "…"


# ----------------------------------------------------------------------------
# 修复 SQL 生成（让目标库向源库看齐）
# ----------------------------------------------------------------------------
class FixWriter:
    def __init__(self, path: str, engine, with_delete: bool, schema: str | None = None):
        self.f = open(path, "w", encoding="utf-8")
        self.dialect = engine.dialect.name
        self.q = engine.dialect.identifier_preparer.quote
        self.schema = schema
        self.with_delete = with_delete
        self.f.write("-- 由 db_diff.py 生成：使 目标库 与 源库 一致\n"
                     "-- 说明：INSERT/UPDATE 仅包含参与对比的列（被忽略的列不会写入）\n"
                     "-- 执行前请先备份并人工复核！\nBEGIN;\n\n")

    def qt(self, table: str) -> str:
        return (self.q(self.schema) + "." if self.schema else "") + self.q(table)

    def comment(self, text: str):
        for line in text.splitlines():
            self.f.write(f"-- {line}\n")

    def lit(self, v) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return ("TRUE" if v else "FALSE") if self.dialect == "postgresql" else ("1" if v else "0")
        if isinstance(v, (int, Decimal)):
            return str(v)
        if isinstance(v, float):
            return repr(v)
        if isinstance(v, (bytes, bytearray, memoryview)):
            h = bytes(v).hex()
            return f"'\\x{h}'::bytea" if self.dialect == "postgresql" else f"X'{h}'"
        if isinstance(v, datetime):
            return "'" + v.isoformat(sep=" ") + "'"
        if isinstance(v, date):
            return f"'{v.isoformat()}'"
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        s = str(v)
        if self.dialect == "mysql":
            s = s.replace("\\", "\\\\")
        return "'" + s.replace("'", "''") + "'"

    def _where(self, cols, vals, key_idx):
        return " AND ".join(
            f"{self.q(cols[i])} IS NULL" if vals[i] is None else f"{self.q(cols[i])} = {self.lit(vals[i])}"
            for i in key_idx
        )

    def insert(self, table, cols, row):
        self.f.write(
            f"INSERT INTO {self.qt(table)} ({', '.join(self.q(c) for c in cols)}) "
            f"VALUES ({', '.join(self.lit(v) for v in row)});\n"
        )

    def update(self, table, cols, row, key_idx):
        sets = [f"{self.q(c)} = {self.lit(v)}" for i, (c, v) in enumerate(zip(cols, row)) if i not in key_idx]
        if sets:
            self.f.write(f"UPDATE {self.qt(table)} SET {', '.join(sets)} WHERE {self._where(cols, row, key_idx)};\n")

    def delete(self, table, cols, row, key_idx):
        prefix = "" if self.with_delete else "-- "
        self.f.write(f"{prefix}DELETE FROM {self.qt(table)} WHERE {self._where(cols, row, key_idx)};\n")

    def close(self):
        self.f.write("\nCOMMIT;\n")
        self.f.close()

    def abort(self):
        """对比被取消或出错时：文件不完整，明确标注并回滚，防止被误执行。"""
        self.f.write("\n-- !!! 对比被中断或出错，本文件不完整，请勿执行 !!!\nROLLBACK;\n")
        self.f.close()


# ----------------------------------------------------------------------------
# 表 / 结构对比
# ----------------------------------------------------------------------------
def _sig_indexes(insp, name, schema, lc):
    sigs = set()
    try:
        for ix in insp.get_indexes(name, schema=schema):
            cols = tuple(lc(c) for c in (ix.get("column_names") or []) if c)
            if cols:
                sigs.add((cols, bool(ix.get("unique"))))
    except Exception:
        pass
    try:
        for uc in insp.get_unique_constraints(name, schema=schema):
            cols = tuple(lc(c) for c in (uc.get("column_names") or []) if c)
            if cols:
                sigs.add((cols, True))
    except Exception:
        pass
    return sigs


def _sig_fks(insp, name, schema, lc):
    sigs = set()
    try:
        for fk in insp.get_foreign_keys(name, schema=schema):
            sigs.add((tuple(lc(c) for c in fk["constrained_columns"]),
                      lc(fk["referred_table"]), tuple(lc(c) for c in fk["referred_columns"])))
    except Exception:
        pass
    return sigs


def compare_schema(si, ti, s_name, t_name, s_schema, t_schema, lc):
    sc = {lc(c["name"]): c for c in si.get_columns(s_name, schema=s_schema)}
    tc = {lc(c["name"]): c for c in ti.get_columns(t_name, schema=t_schema)}
    spk = tuple(lc(c) for c in si.get_pk_constraint(s_name, schema=s_schema).get("constrained_columns", []))
    tpk = tuple(lc(c) for c in ti.get_pk_constraint(t_name, schema=t_schema).get("constrained_columns", []))
    d = {
        "columns_only_in_source": [{"name": sc[k]["name"], "type": type_str(sc[k]["type"])} for k in sc if k not in tc],
        "columns_only_in_target": [{"name": tc[k]["name"], "type": type_str(tc[k]["type"])} for k in tc if k not in sc],
        "column_diffs": [], "primary_key": None, "indexes": {}, "foreign_keys": {},
    }
    for k in sc:
        if k not in tc:
            continue
        a, b = sc[k], tc[k]
        name = a["name"]
        fa, fb = type_family(a["type"]), type_family(b["type"])
        if fa != fb:
            d["column_diffs"].append({"column": name, "issue": "类型不同",
                                      "source": type_str(a["type"]), "target": type_str(b["type"])})
        elif fa == "STRING":
            la, lb = getattr(a["type"], "length", None), getattr(b["type"], "length", None)
            if la and lb and la != lb:
                d["column_diffs"].append({"column": name, "issue": "长度不同", "source": la, "target": lb})
        elif fa == "DECIMAL":
            pa = (getattr(a["type"], "precision", None), getattr(a["type"], "scale", None))
            pb = (getattr(b["type"], "precision", None), getattr(b["type"], "scale", None))
            if None not in pa + pb and pa != pb:
                d["column_diffs"].append({"column": name, "issue": "精度不同", "source": str(pa), "target": str(pb)})
        # 主键列隐含 NOT NULL（SQLite 等库反射出的 nullable 不可靠）
        na = bool(a.get("nullable", True)) and k not in spk
        nb = bool(b.get("nullable", True)) and k not in tpk
        if na != nb:
            d["column_diffs"].append({"column": name, "issue": "可空性不同",
                                      "source": "NULL" if na else "NOT NULL", "target": "NULL" if nb else "NOT NULL"})
    if set(spk) != set(tpk):
        d["primary_key"] = {"source": list(spk), "target": list(tpk)}
    for key, fn in (("indexes", _sig_indexes), ("foreign_keys", _sig_fks)):
        a, b = fn(si, s_name, s_schema, lc), fn(ti, t_name, t_schema, lc)
        if key == "indexes":  # 主键隐含的唯一索引不算差异
            a.discard((spk, True)); b.discard((tpk, True))
        if a != b:
            fmt = _fmt_index if key == "indexes" else _fmt_fk
            d[key] = {"only_in_source": sorted(map(fmt, a - b)), "only_in_target": sorted(map(fmt, b - a))}
    return d


def _fmt_index(sig) -> str:
    cols, unique = sig
    return f"{'唯一索引' if unique else '索引'}({', '.join(cols)})"


def _fmt_fk(sig) -> str:
    cols, ref, rcols = sig
    return f"外键({', '.join(cols)}) → {ref}({', '.join(rcols)})"


def schema_has_diff(d) -> bool:
    return bool(d["columns_only_in_source"] or d["columns_only_in_target"] or d["column_diffs"]
                or d["primary_key"] or d["indexes"] or d["foreign_keys"])


def schema_diff_count(sc) -> int:
    if not sc:
        return 0
    return (len(sc["columns_only_in_source"]) + len(sc["columns_only_in_target"]) + len(sc["column_diffs"])
            + bool(sc["primary_key"]) + bool(sc["indexes"]) + bool(sc["foreign_keys"]))


# ----------------------------------------------------------------------------
# 数据对比
# ----------------------------------------------------------------------------
def scan(engine, table, real_cols, chunk):
    with engine.connect() as conn:
        res = conn.execution_options(stream_results=True).execute(select(*[table.c[c] for c in real_cols]))
        for part in res.partitions(chunk):
            check_cancel()
            for row in part:
                yield tuple(row)


def count_rows(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar()


def hash_pass(engine, table, real_cols, key_idx, norm, chunk):
    dups = 0
    store = {} if key_idx is not None else collections.Counter()
    for row in scan(engine, table, real_cols, chunk):
        nv = [norm(v) for v in row]
        h = digest(nv)
        if key_idx is not None:
            k = tuple(nv[i] for i in key_idx)
            dups += k in store
            store[k] = h
        else:
            store[h] += 1
    return store, dups


def compare_data(s_eng, t_eng, s_tab, t_tab, cols, key_logical, args, norm, fix, t_name):
    logical = [c[0] for c in cols]
    s_real = [c[1] for c in cols]
    t_real = [c[2] for c in cols]
    key_idx = [logical.index(k) for k in key_logical] if key_logical else None
    n = max(int(args.sample), 0)
    res = {"mode": "key" if key_idx else "fullrow", "key": key_logical or [], "note": "",
           "rows_source": count_rows(s_eng, s_tab), "rows_target": count_rows(t_eng, t_tab),
           "missing_in_target": 0, "extra_in_target": 0, "modified": 0, "dup_keys": 0,
           "samples": {"missing_in_target": [], "extra_in_target": [], "modified": []}}
    if args.count_only:
        res["missing_in_target"] = max(res["rows_source"] - res["rows_target"], 0)
        res["extra_in_target"] = max(res["rows_target"] - res["rows_source"], 0)
        return res

    sh, sd = hash_pass(s_eng, s_tab, s_real, key_idx, norm, args.chunk)
    th, td = hash_pass(t_eng, t_tab, t_real, key_idx, norm, args.chunk)
    if key_idx is not None and (sd or td):
        # 键不唯一：按键比较会产生误导结果，自动降级为整行哈希比较
        res["note"] = f"所选键 {','.join(key_logical)} 不唯一（源库重复 {sd}，目标库重复 {td}），已自动降级为整行哈希比较"
        res.update(mode="fullrow", key=[], dup_keys=sd + td)
        key_idx = None
        sh, _ = hash_pass(s_eng, s_tab, s_real, None, norm, args.chunk)
        th, _ = hash_pass(t_eng, t_tab, t_real, None, norm, args.chunk)

    as_dict = lambda row: {c: show(v) for c, v in zip(logical, row)}
    smp = res["samples"]
    need_detail = n > 0 or fix is not None

    if key_idx is not None:
        only_s = {k for k in sh if k not in th}
        only_t = {k for k in th if k not in sh}
        mod = [k for k, h in sh.items() if k in th and th[k] != h]
        res.update(missing_in_target=len(only_s), extra_in_target=len(only_t), modified=len(mod))
        mod_set, mod_sample = set(mod), set(mod[:n])
        kf = lambda nv: tuple(nv[i] for i in key_idx)
        s_mod_rows, t_mod_rows = {}, {}
        if need_detail and (only_s or mod):
            for row in scan(s_eng, s_tab, s_real, args.chunk):
                nv = [norm(v) for v in row]; k = kf(nv)
                if k in only_s:
                    if len(smp["missing_in_target"]) < n: smp["missing_in_target"].append(as_dict(row))
                    if fix: fix.insert(t_name, t_real, row)
                elif k in mod_set:
                    if k in mod_sample: s_mod_rows[k] = row
                    if fix: fix.update(t_name, t_real, row, key_idx)
        if need_detail and (only_t or mod_sample):
            for row in scan(t_eng, t_tab, t_real, args.chunk):
                nv = [norm(v) for v in row]; k = kf(nv)
                if k in only_t:
                    if len(smp["extra_in_target"]) < n: smp["extra_in_target"].append(as_dict(row))
                    if fix: fix.delete(t_name, t_real, row, key_idx)
                elif k in mod_sample:
                    t_mod_rows[k] = row
        for k in mod[:n]:
            if k in s_mod_rows and k in t_mod_rows:
                a, b = s_mod_rows[k], t_mod_rows[k]
                diffs = {c: {"source": show(x), "target": show(y)}
                         for c, x, y in zip(logical, a, b) if norm(x) != norm(y)}
                smp["modified"].append({"key": {logical[i]: show(a[i]) for i in key_idx}, "diffs": diffs})
    else:  # 无主键 / 键不唯一：整行哈希多重集合
        s_extra, t_extra = sh - th, th - sh
        res.update(missing_in_target=sum(s_extra.values()), extra_in_target=sum(t_extra.values()))
        if need_detail and s_extra:
            need = dict(s_extra)
            for row in scan(s_eng, s_tab, s_real, args.chunk):
                h = digest([norm(v) for v in row])
                if need.get(h, 0) > 0:
                    need[h] -= 1
                    if len(smp["missing_in_target"]) < n: smp["missing_in_target"].append(as_dict(row))
                    if fix: fix.insert(t_name, t_real, row)
        if need_detail and t_extra:
            need = dict(t_extra)
            if n > 0:
                for row in scan(t_eng, t_tab, t_real, args.chunk):
                    h = digest([norm(v) for v in row])
                    if need.get(h, 0) > 0:
                        need[h] -= 1
                        if len(smp["extra_in_target"]) < n: smp["extra_in_target"].append(as_dict(row))
                        if len(smp["extra_in_target"]) >= n:
                            break
            if fix:
                fix.comment(f"[{t_name}] 无唯一键，目标库多 {res['extra_in_target']} 行，无法安全生成 DELETE，请人工处理")
    return res


def data_has_diff(d) -> bool:
    return bool(d and (d["missing_in_target"] or d["extra_in_target"] or d["modified"]
                       or d["rows_source"] != d["rows_target"]))


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def split_csv(items):
    if isinstance(items, str):
        items = [items]
    return [p.strip() for it in (items or []) for p in it.replace("\n", ",").split(",") if p.strip()]


def parse_keys(items, lc) -> dict:
    """解析业务键：'orders=order_no; items=order_no,line_no' -> {表: [列...]}（复合键列用 , | + 分隔）。"""
    keys = {}
    if isinstance(items, str):
        items = [items]
    for item in items or []:
        for part in item.replace("\n", ";").split(";"):
            t, _, c = part.partition("=")
            cols = [lc(x.strip()) for x in re.split(r"[,|+]", c) if x.strip()]
            if t.strip() and cols:
                keys[lc(t.strip())] = cols
    return keys


def default_args(**kw) -> argparse.Namespace:
    """GUI / 脚本调用时使用的默认参数。"""
    d = dict(source=None, target=None, src_schema=None, tgt_schema=None, include=None, exclude=None,
             ignore_columns=None, key=None, schema_only=False, count_only=False, case_sensitive=False,
             float_places=6, no_trim=False, sample=20, chunk=5000, json=None, html=None, fix_sql=None,
             with_delete=False)
    d.update(kw)
    return argparse.Namespace(**d)


def run(args, progress=None) -> dict:
    """
    执行对比并返回报告 dict。
    progress(kind, **info) 回调：kind = "stage" | "table" | "table_done"
    被取消时抛出 Cancelled。
    """
    CANCEL.clear()
    emit = progress or (lambda *a, **k: None)
    t_start = time.time()
    s_eng = t_eng = fix = None
    finished = False
    try:
        emit("stage", text="正在连接数据库 ...")
        s_eng, t_eng = create_engine(args.source), create_engine(args.target)
        si, ti = inspect(s_eng), inspect(t_eng)
        ci = not args.case_sensitive
        lc = (lambda s: s.lower()) if ci else (lambda s: s)
        s_tabs = {lc(n): n for n in si.get_table_names(schema=args.src_schema)}
        t_tabs = {lc(n): n for n in ti.get_table_names(schema=args.tgt_schema)}

        inc, exc = [lc(p) for p in split_csv(args.include)], [lc(p) for p in split_csv(args.exclude)]
        ok = lambda n: (not inc or any(fnmatch.fnmatch(n, p) for p in inc)) and not any(fnmatch.fnmatch(n, p) for p in exc)
        s_tabs = {k: v for k, v in s_tabs.items() if ok(k)}
        t_tabs = {k: v for k, v in t_tabs.items() if ok(k)}
        ignore = [lc(p) for p in split_csv(args.ignore_columns)]
        keys = parse_keys(args.key, lc)

        only_s, only_t = sorted(set(s_tabs) - set(t_tabs)), sorted(set(t_tabs) - set(s_tabs))
        common = sorted(set(s_tabs) & set(t_tabs))
        norm = Normalizer(args.float_places, not args.no_trim)
        fix = FixWriter(args.fix_sql, t_eng, args.with_delete, args.tgt_schema) if args.fix_sql else None

        def safe_count(eng, name, schema):
            try:
                return count_rows(eng, Table(name, MetaData(), schema=schema, autoload_with=eng))
            except Exception:
                return None

        emit("stage", text="正在统计仅单边存在的表 ...")
        rep = {
            "meta": {"source": make_url(args.source).render_as_string(hide_password=True),
                     "target": make_url(args.target).render_as_string(hide_password=True),
                     "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "elapsed": 0.0, "mode":
                     "schema-only" if args.schema_only else ("count-only" if args.count_only else "full")},
            "only_in_source": [{"table": s_tabs[k], "rows": safe_count(s_eng, s_tabs[k], args.src_schema)} for k in only_s],
            "only_in_target": [{"table": t_tabs[k], "rows": safe_count(t_eng, t_tabs[k], args.tgt_schema)} for k in only_t],
            "common": [],
        }
        if fix:
            for k in only_s:
                fix.comment(f"表 {s_tabs[k]} 仅存在于源库：请先在目标库建表后再迁移数据")

        for i, k in enumerate(common, 1):
            check_cancel()
            s_name, t_name = s_tabs[k], t_tabs[k]
            entry = {"table": t_name if t_name == s_name else f"{s_name} / {t_name}", "status": "一致",
                     "schema": None, "data": None, "error": None}
            t0 = time.time()
            emit("table", index=i, total=len(common), name=entry["table"])
            try:
                entry["schema"] = compare_schema(si, ti, s_name, t_name, args.src_schema, args.tgt_schema, lc)
                if not args.schema_only:
                    s_tab = Table(s_name, MetaData(), schema=args.src_schema, autoload_with=s_eng)
                    t_tab = Table(t_name, MetaData(), schema=args.tgt_schema, autoload_with=t_eng)
                    s_cols = {lc(c.name): c.name for c in s_tab.columns}
                    t_cols = {lc(c.name): c.name for c in t_tab.columns}
                    ign = lambda c: any(fnmatch.fnmatch(f"{k}.{lc(c)}", p) or fnmatch.fnmatch(lc(c), p) for p in ignore)
                    cols = [(lc(n), s_cols[lc(n)], t_cols[lc(n)]) for n in s_cols if lc(n) in t_cols and not ign(n)]
                    if not cols:
                        raise ValueError("两表没有可对比的公共列（列名完全不同或全部被忽略），已跳过数据对比")
                    logical = [c[0] for c in cols]
                    key = keys.get(k)
                    if not key:
                        key = [lc(c) for c in si.get_pk_constraint(s_name, schema=args.src_schema)["constrained_columns"]] \
                            or [lc(c) for c in ti.get_pk_constraint(t_name, schema=args.tgt_schema)["constrained_columns"]]
                    key = key if key and all(c in logical for c in key) else None
                    if fix:
                        fix.comment(f"==== {t_name} ====")
                        for c in entry["schema"]["columns_only_in_source"]:
                            fix.comment(f"ALTER TABLE {t_name} ADD COLUMN {c['name']} {c['type']};  -- 目标库缺列")
                    entry["data"] = compare_data(s_eng, t_eng, s_tab, t_tab, cols, key, args, norm, fix, t_name)
                sd, dd = schema_has_diff(entry["schema"]), data_has_diff(entry["data"])
                entry["status"] = "结构+数据差异" if sd and dd else "结构差异" if sd else "数据差异" if dd else "一致"
            except Cancelled:
                raise
            except Exception as e:  # 单表失败不影响其它表
                entry["status"], entry["error"] = "对比出错", friendly_error(e)
            rep["common"].append(entry)
            emit("table_done", index=i, total=len(common), name=entry["table"], status=entry["status"],
                 seconds=time.time() - t0)

        bad = [e for e in rep["common"] if e["status"] != "一致"]
        rep["summary"] = {
            "source_tables": len(s_tabs), "target_tables": len(t_tabs), "common_tables": len(common),
            "only_in_source": len(only_s), "only_in_target": len(only_t),
            "common_identical": len(common) - len(bad), "common_different": len(bad),
            "errors": sum(1 for e in bad if e["error"]),
        }
        rep["meta"]["elapsed"] = round(time.time() - t_start, 1)
        finished = True
        return rep
    finally:
        if fix:
            fix.close() if finished else fix.abort()
        for e in (s_eng, t_eng):
            if e is not None:
                e.dispose()


# ----------------------------------------------------------------------------
# 报告输出
# ----------------------------------------------------------------------------
def _w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, n):
    return str(s) + " " * (n - _w(s))


def entry_detail_lines(e) -> list:
    """单张表的差异明细（纯文本行），控制台和 GUI 共用。"""
    L = [f"{e['table']}  [{e['status']}]"]
    if e["error"]:
        L.append(f"   错误: {e['error']}")
        return L
    sc = e["schema"]
    if sc:
        for c in sc["columns_only_in_source"]: L.append(f"   [结构] 目标库缺列: {c['name']} {c['type']}")
        for c in sc["columns_only_in_target"]: L.append(f"   [结构] 目标库多列: {c['name']} {c['type']}")
        for c in sc["column_diffs"]: L.append(f"   [结构] 列 {c['column']} {c['issue']}: {c['source']} -> {c['target']}")
        if sc["primary_key"]: L.append(f"   [结构] 主键不同: 源 {sc['primary_key']['source']} / 目标 {sc['primary_key']['target']}")
        for k, nm in (("indexes", "索引"), ("foreign_keys", "外键")):
            for side, label in (("only_in_source", "仅源库"), ("only_in_target", "仅目标库")):
                for x in sc[k].get(side, []): L.append(f"   [结构] {nm}{label}: {x}")
    d = e["data"]
    if d:
        if d["note"]:
            L.append(f"   [提示] {d['note']}")
        if data_has_diff(d):
            how = ("主键/业务键 " + ",".join(d["key"])) if d["key"] else "整行哈希(无唯一键)"
            L.append(f"   [数据] 比对方式: {how}   行数 源 {d['rows_source']} / 目标 {d['rows_target']}   "
                     f"缺失 {d['missing_in_target']}  多余 {d['extra_in_target']}  不一致 {d['modified']}")
            for lab, k in (("缺失(源有目标无)", "missing_in_target"), ("多余(目标有源无)", "extra_in_target")):
                for r in d["samples"][k]: L.append(f"      - {lab}: {r}")
            for r in d["samples"]["modified"]:
                L.append(f"      - 不一致 key={r['key']}")
                for c, v in r["diffs"].items(): L.append(f"          {c}: 源={v['source']}  目标={v['target']}")
    if len(L) == 1:
        L.append("   （无差异）")
    return L


def console_text(rep) -> str:
    m, s = rep["meta"], rep["summary"]
    O = [f"源库: {m['source']}", f"目标库: {m['target']}",
         f"模式: {m['mode']}   时间: {m['time']}   耗时: {m.get('elapsed', 0)}s", "",
         "=" * 70, "一、表级对比", "=" * 70,
         f"源库 {s['source_tables']} 张表 | 目标库 {s['target_tables']} 张表 | 共有 {s['common_tables']} 张 "
         f"| 仅源库 {s['only_in_source']} | 仅目标库 {s['only_in_target']}"]
    for title, key in (("【仅在源库】（迁移遗漏）", "only_in_source"), ("【仅在目标库】（多余/新增）", "only_in_target")):
        if rep[key]:
            O.append(title)
            for t in rep[key]:
                O.append(f"   - {t['table']}  (行数: {t['rows'] if t['rows'] is not None else '未知'})")
    O += ["", "=" * 70, "二、共有表对比", "=" * 70]
    head = ["表名", "结论", "行数(源/目标)", "缺失", "多余", "不一致", "结构差异项"]
    rows = []
    for e in rep["common"]:
        d = e["data"]
        rows.append([e["table"], e["status"], f"{d['rows_source']}/{d['rows_target']}" if d else "-",
                     d["missing_in_target"] if d else "-", d["extra_in_target"] if d else "-",
                     d["modified"] if d else "-", schema_diff_count(e["schema"])])
    widths = [max(_w(x) for x in col) for col in zip(head, *rows)] if rows else [_w(h) for h in head]
    O.append("  ".join(_pad(h, w) for h, w in zip(head, widths)))
    O.append("  ".join("-" * w for w in widths))
    for r in rows:
        O.append("  ".join(_pad(c, w) for c, w in zip(r, widths)))
    detail = [e for e in rep["common"] if e["status"] != "一致"]
    if detail:
        O += ["", "=" * 70, "三、差异明细", "=" * 70]
    for e in detail:
        O.append("")
        O += entry_detail_lines(e)
    return "\n".join(O)


def print_console(rep):
    print("\n" + console_text(rep))


def render_html(rep) -> str:
    E = html.escape
    s = rep["summary"]
    out = [f"""<!doctype html><html lang="zh"><meta charset="utf-8"><title>数据迁移对比报告</title>
<style>body{{font:14px/1.6 -apple-system,Segoe UI,'Microsoft YaHei',sans-serif;margin:24px auto;max-width:1100px;color:#222;padding:0 16px}}
table{{border-collapse:collapse;width:100%;margin:8px 0 16px}}th,td{{border:1px solid #ddd;padding:5px 9px;text-align:left;vertical-align:top}}
th{{background:#f4f5f7}}.ok{{color:#1a7f37}}.bad{{color:#cf222e;font-weight:600}}.warn{{color:#b45309;font-weight:600}}
code{{background:#f4f5f7;padding:1px 4px;border-radius:3px}}details{{margin:6px 0}}summary{{cursor:pointer;font-weight:600}}
.cards{{display:flex;gap:12px;flex-wrap:wrap}}.card{{border:1px solid #ddd;border-radius:8px;padding:10px 16px;min-width:120px}}
.card b{{display:block;font-size:22px}}</style>
<h1>数据迁移对比报告</h1><p>源库：<code>{E(rep['meta']['source'])}</code><br>目标库：<code>{E(rep['meta']['target'])}</code><br>
时间：{E(rep['meta']['time'])}　耗时：{rep['meta'].get('elapsed', 0)}s　模式：{E(rep['meta']['mode'])}</p><div class="cards">"""]
    for label, key, cls in (("源库表数", "source_tables", ""), ("目标库表数", "target_tables", ""), ("共有表", "common_tables", ""),
                            ("仅在源库", "only_in_source", "warn"), ("仅在目标库", "only_in_target", "warn"),
                            ("共有且一致", "common_identical", "ok"), ("共有但有差异", "common_different", "bad")):
        out.append(f'<div class="card">{label}<b class="{cls if s[key] else ""}">{s[key]}</b></div>')
    out.append("</div><h2>一、不同表</h2>")
    for title, key in (("仅在源库（迁移遗漏）", "only_in_source"), ("仅在目标库（多余）", "only_in_target")):
        out.append(f"<h3>{title}</h3>")
        if rep[key]:
            out.append("<table><tr><th>表名</th><th>行数</th></tr>" + "".join(
                f"<tr><td>{E(t['table'])}</td><td>{t['rows'] if t['rows'] is not None else '未知'}</td></tr>"
                for t in rep[key]) + "</table>")
        else:
            out.append("<p class='ok'>无</p>")
    out.append("<h2>二、相同表对比</h2><table><tr><th>表名</th><th>结论</th><th>行数(源/目标)</th><th>缺失</th><th>多余</th><th>不一致</th></tr>")
    for e in rep["common"]:
        d = e["data"]
        cls = "ok" if e["status"] == "一致" else "bad"
        cells = (f"<td>{d['rows_source']}/{d['rows_target']}</td><td>{d['missing_in_target']}</td>"
                 f"<td>{d['extra_in_target']}</td><td>{d['modified']}</td>") if d else "<td>-</td>" * 4
        out.append(f"<tr><td>{E(e['table'])}</td><td class='{cls}'>{e['status']}</td>{cells}</tr>")
    out.append("</table><h2>三、差异明细</h2>")
    for e in rep["common"]:
        if e["status"] == "一致":
            continue
        out.append(f"<details open><summary>{E(e['table'])} <span class='bad'>[{e['status']}]</span></summary>")
        if e["error"]:
            out.append(f"<p class='bad'>{E(e['error'])}</p></details>")
            continue
        sc, rows = e["schema"], []
        rows += [("目标库缺列", f"{c['name']} {c['type']}") for c in sc["columns_only_in_source"]]
        rows += [("目标库多列", f"{c['name']} {c['type']}") for c in sc["columns_only_in_target"]]
        rows += [(f"列{c['issue']}", f"{c['column']}: {c['source']} → {c['target']}") for c in sc["column_diffs"]]
        if sc["primary_key"]: rows.append(("主键不同", str(sc["primary_key"])))
        for k, nm in (("indexes", "索引"), ("foreign_keys", "外键")):
            rows += [(f"{nm}仅源库", x) for x in sc[k].get("only_in_source", [])]
            rows += [(f"{nm}仅目标库", x) for x in sc[k].get("only_in_target", [])]
        if rows:
            out.append("<h4>结构差异</h4><table>" + "".join(f"<tr><td>{E(a)}</td><td>{E(b)}</td></tr>" for a, b in rows) + "</table>")
        d = e["data"]
        if d and d["note"]:
            out.append(f"<p class='warn'>{E(d['note'])}</p>")
        if d and data_has_diff(d):
            out.append(f"<h4>数据差异（{'键: ' + E(','.join(d['key'])) if d['key'] else '整行哈希，无唯一键'}）</h4>")
            for lab, k in (("缺失(源有目标无)", "missing_in_target"), ("多余(目标有源无)", "extra_in_target")):
                if d["samples"][k]:
                    cols = list(d["samples"][k][0])
                    out.append(f"<p>{lab}：共 {d[k]} 行，样本：</p><table><tr>" + "".join(f"<th>{E(c)}</th>" for c in cols) + "</tr>"
                               + "".join("<tr>" + "".join(f"<td>{E(r[c])}</td>" for c in cols) + "</tr>" for r in d["samples"][k]) + "</table>")
            if d["samples"]["modified"]:
                out.append(f"<p>不一致：共 {d['modified']} 行，样本：</p><table><tr><th>键</th><th>列</th><th>源库</th><th>目标库</th></tr>")
                for r in d["samples"]["modified"]:
                    for c, v in r["diffs"].items():
                        out.append(f"<tr><td>{E(str(r['key']))}</td><td>{E(c)}</td><td>{E(v['source'])}</td><td>{E(v['target'])}</td></tr>")
                out.append("</table>")
        out.append("</details>")
    return "".join(out) + "</html>"


def save_reports(rep, json_path=None, html_path=None):
    if json_path:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
    if html_path:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(render_html(rep))


# ----------------------------------------------------------------------------
# 演示：自动生成两个 SQLite 库
# ----------------------------------------------------------------------------
def build_demo():
    import sqlite3
    d = tempfile.mkdtemp(prefix="db_diff_demo_")
    sp, tp = os.path.join(d, "source.db"), os.path.join(d, "target.db")
    s, t = sqlite3.connect(sp), sqlite3.connect(tp)
    s.executescript("""
    CREATE TABLE users(id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL, email VARCHAR(50), balance NUMERIC(10,2), created_at DATETIME);
    CREATE UNIQUE INDEX uq_email ON users(email);
    CREATE TABLE orders(id INTEGER PRIMARY KEY, user_id INT, amount DECIMAL(10,2));
    CREATE TABLE logs(msg TEXT);
    CREATE TABLE legacy_config(k TEXT PRIMARY KEY, v TEXT);
    INSERT INTO users VALUES (1,'张三','a@x.com',100.50,'2024-01-01 10:00:00'),(2,'李四','b@x.com',20,'2024-01-02 10:00:00'),
                             (3,'王五','c@x.com',0,'2024-01-03 10:00:00'),(4,'赵六','d@x.com',9.9,'2024-01-04 10:00:00');
    INSERT INTO orders VALUES (1,1,10.00),(2,2,20.00),(3,3,30.00);
    INSERT INTO logs VALUES ('start'),('start'),('error'),('end');
    INSERT INTO legacy_config VALUES ('a','1');""")
    t.executescript("""
    CREATE TABLE users(id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL, email VARCHAR(100), balance NUMERIC(10,2), phone VARCHAR(20));
    CREATE TABLE orders(id INTEGER PRIMARY KEY, user_id INT, amount DECIMAL(10,2));
    CREATE TABLE logs(msg TEXT);
    CREATE TABLE new_audit(id INTEGER PRIMARY KEY, note TEXT);
    INSERT INTO users VALUES (1,'张三','a@x.com',100.5,NULL),(2,'李四','b2@x.com',20.00,NULL),(4,'赵六','d@x.com',9.9,NULL),(9,'多余用户','z@x.com',1,NULL);
    INSERT INTO orders VALUES (1,1,10.0),(2,2,20.0),(3,3,30.0);
    INSERT INTO logs VALUES ('start'),('error'),('end'),('end');
    INSERT INTO new_audit VALUES (1,'x');""")
    s.commit(); t.commit(); s.close(); t.close()
    return f"sqlite:///{sp}", f"sqlite:///{tp}", d


# ----------------------------------------------------------------------------
# 命令行入口
# ----------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="数据迁移 源库/目标库 对比工具（表级 + 结构级 + 数据级）",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--source", help="源库 SQLAlchemy URL")
    p.add_argument("--target", help="目标库 SQLAlchemy URL")
    p.add_argument("--src-schema"); p.add_argument("--tgt-schema")
    p.add_argument("--include", action="append", help="只对比这些表(通配符,逗号分隔,可重复)")
    p.add_argument("--exclude", action="append", help="排除这些表(通配符)")
    p.add_argument("--ignore-columns", action="append", help="忽略列，如 '*.update_time,user.last_login'")
    p.add_argument("--key", action="append",
                   help="指定业务键: 表名=列1,列2 (多个表用 ; 分隔，或重复本参数)")
    p.add_argument("--schema-only", action="store_true", help="只比表和结构，不比数据")
    p.add_argument("--count-only", action="store_true", help="数据只比行数（最快）")
    p.add_argument("--case-sensitive", action="store_true", help="表名/列名区分大小写（默认忽略，适合 MySQL→PG 迁移）")
    p.add_argument("--float-places", type=int, default=6, help="浮点比较保留小数位 (默认 6)")
    p.add_argument("--no-trim", action="store_true", help="不去除字符串尾部空格（默认去除，消除 CHAR 补位差异）")
    p.add_argument("--sample", type=int, default=20, help="每类差异最多展示的样本行数 (默认 20)")
    p.add_argument("--chunk", type=int, default=5000, help="流式读取批大小 (默认 5000)")
    p.add_argument("--json", help="输出 JSON 报告路径")
    p.add_argument("--html", help="输出 HTML 报告路径")
    p.add_argument("--fix-sql", help="输出修复 SQL 路径（INSERT/UPDATE，DELETE 默认注释）")
    p.add_argument("--with-delete", action="store_true", help="修复 SQL 中启用 DELETE")
    p.add_argument("--demo", action="store_true", help="生成两个 SQLite 示例库并演示")
    a = p.parse_args(argv)
    if a.demo:
        a.source, a.target, d = build_demo()
        a.html = a.html or os.path.join(d, "report.html")
        a.json = a.json or os.path.join(d, "report.json")
        a.fix_sql = a.fix_sql or os.path.join(d, "fix.sql")
        a.ignore_columns = (a.ignore_columns or []) + ["users.created_at"]
        print(f"[demo] 示例文件目录: {d}", file=sys.stderr)
    if not (a.source and a.target):
        p.error("必须提供 --source 与 --target（或使用 --demo）")
    return a


def _cli_progress(kind, **k):
    if kind == "stage":
        print(k["text"], file=sys.stderr)
    elif kind == "table":
        print(f"[{k['index']}/{k['total']}] 对比 {k['name']} ...", file=sys.stderr, end="", flush=True)
    elif kind == "table_done":
        print(f" {k['status']} ({k['seconds']:.1f}s)", file=sys.stderr)


def main(argv=None) -> int:
    for st in (sys.stdout, sys.stderr):  # 避免 Windows GBK 等控制台遇到特殊字符崩溃
        try:
            st.reconfigure(errors="replace")
        except Exception:
            pass
    args = parse_args(argv)
    try:
        rep = run(args, progress=_cli_progress)
    except Cancelled:
        print("\n已取消。", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"\n对比失败：{friendly_error(e)}", file=sys.stderr)
        return 2
    print_console(rep)
    save_reports(rep, args.json, args.html)
    for label, path in (("JSON", args.json), ("HTML", args.html), ("修复SQL", args.fix_sql)):
        if path:
            print(f"{label} 已输出: {path}")
    s = rep["summary"]
    if s["errors"]:
        return 2
    return 1 if (s["only_in_source"] or s["only_in_target"] or s["common_different"]) else 0


if __name__ == "__main__":
    sys.exit(main())
