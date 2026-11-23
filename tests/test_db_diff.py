"""核心对比逻辑的回归测试（全部使用 SQLite，无需外部数据库）。"""
import random
import sqlite3

import pytest
from sqlalchemy.engine import URL

import db_diff as D


def url(path):
    return URL.create("sqlite", database=str(path)).render_as_string(hide_password=False)


def make(tmp_path, src_sql, tgt_sql):
    sp, tp = tmp_path / "s.db", tmp_path / "t.db"
    for p, sql in ((sp, src_sql), (tp, tgt_sql)):
        c = sqlite3.connect(p)
        c.executescript(sql)
        c.commit()
        c.close()
    return sp, tp


def compare(sp, tp, **kw):
    kw.setdefault("sample", 5)
    return D.run(D.default_args(source=url(sp), target=url(tp), **kw))


def entry(rep, name):
    return next(e for e in rep["common"] if e["table"] == name)


# ---------------- 表级 ----------------
def test_table_level(tmp_path):
    sp, tp = make(tmp_path,
                  "CREATE TABLE a(id INT); CREATE TABLE only_s(id INT); CREATE TABLE Common(id INTEGER PRIMARY KEY);",
                  "CREATE TABLE a(id INT); CREATE TABLE only_t(id INT); CREATE TABLE common(id INTEGER PRIMARY KEY);")
    rep = compare(sp, tp)
    assert [t["table"] for t in rep["only_in_source"]] == ["only_s"]
    assert [t["table"] for t in rep["only_in_target"]] == ["only_t"]
    # 默认忽略大小写：Common 与 common 视为同一张表（显示名为 "Common / common"）
    assert len(rep["common"]) == 2 and any("common" in e["table"].lower() for e in rep["common"])


def test_include_exclude(tmp_path):
    ddl = "CREATE TABLE keep_1(id INT); CREATE TABLE tmp_x(id INT); CREATE TABLE tmp_y(id INT);"
    sp, tp = make(tmp_path, ddl, ddl)
    assert len(compare(sp, tp, exclude=["tmp_*"])["common"]) == 1
    assert len(compare(sp, tp, include=["tmp_*"])["common"]) == 2


# ---------------- 结构级 ----------------
def test_schema_diffs(tmp_path):
    sp, tp = make(tmp_path,
                  "CREATE TABLE u(id INTEGER PRIMARY KEY, email VARCHAR(50), n INT, extra TEXT);"
                  "CREATE UNIQUE INDEX ux ON u(email);",
                  "CREATE TABLE u(id INTEGER PRIMARY KEY, email VARCHAR(100), n TEXT, phone TEXT);")
    sc = entry(compare(sp, tp, schema_only=True), "u")["schema"]
    assert [c["name"] for c in sc["columns_only_in_source"]] == ["extra"]
    assert [c["name"] for c in sc["columns_only_in_target"]] == ["phone"]
    issues = {(c["column"], c["issue"]) for c in sc["column_diffs"]}
    assert ("email", "长度不同") in issues and ("n", "类型不同") in issues
    assert sc["indexes"]["only_in_source"] == ["唯一索引(email)"]


def test_pk_nullable_not_false_positive(tmp_path):
    """回归：SQLite 反射的主键列 nullable=True，不应与显式 NOT NULL 的主键报差异。"""
    sp, tp = make(tmp_path, "CREATE TABLE a(id INTEGER PRIMARY KEY);",
                  "CREATE TABLE a(id INTEGER NOT NULL PRIMARY KEY);")
    assert entry(compare(sp, tp), "a")["status"] == "一致"


# ---------------- 数据级 ----------------
def test_data_missing_extra_modified(tmp_path):
    sp, tp = make(tmp_path,
                  "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);"
                  "INSERT INTO t VALUES (1,'a'),(2,'b'),(3,'c');",
                  "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);"
                  "INSERT INTO t VALUES (1,'a'),(2,'B'),(9,'z');")
    d = entry(compare(sp, tp), "t")["data"]
    assert (d["missing_in_target"], d["extra_in_target"], d["modified"]) == (1, 1, 1)
    m = d["samples"]["modified"][0]
    assert m["key"] == {"id": "2"} and m["diffs"]["v"] == {"source": "b", "target": "B"}


def test_value_normalization(tmp_path):
    """Decimal/float 表示差异、CHAR 补空格、date 与零点 datetime 都不应算差异。"""
    sp, tp = make(tmp_path,
                  "CREATE TABLE t(id INTEGER PRIMARY KEY, n NUMERIC(10,2), s TEXT, d DATE);"
                  "INSERT INTO t VALUES (1, 1.50, 'x', '2024-01-01');",
                  "CREATE TABLE t(id INTEGER PRIMARY KEY, n NUMERIC(10,2), s TEXT, d DATETIME);"
                  "INSERT INTO t VALUES (1, 1.5, 'x   ', '2024-01-01 00:00:00');")
    assert entry(compare(sp, tp), "t")["data"]["modified"] == 0


def test_composite_key(tmp_path):
    ddl = "CREATE TABLE c(a INT, b INT, v TEXT, PRIMARY KEY(a,b));"
    sp, tp = make(tmp_path, ddl + "INSERT INTO c VALUES (1,1,'p'),(1,2,'q'),(2,1,'r');",
                  ddl + "INSERT INTO c VALUES (1,1,'p'),(2,1,'R');")
    d = entry(compare(sp, tp), "c")["data"]
    assert d["key"] == ["a", "b"] and (d["missing_in_target"], d["modified"]) == (1, 1)


def test_no_pk_multiset(tmp_path):
    sp, tp = make(tmp_path,
                  "CREATE TABLE l(m TEXT); INSERT INTO l VALUES ('s'),('s'),('e');",
                  "CREATE TABLE l(m TEXT); INSERT INTO l VALUES ('s'),('e'),('e');")
    d = entry(compare(sp, tp), "l")["data"]
    assert d["mode"] == "fullrow" and (d["missing_in_target"], d["extra_in_target"]) == (1, 1)


def test_duplicate_key_falls_back(tmp_path):
    """回归：键不唯一时降级为整行比较，而不是给出误导结果。"""
    sp, tp = make(tmp_path,
                  "CREATE TABLE d(k INT, v TEXT); INSERT INTO d VALUES (1,'a'),(1,'b'),(2,'c');",
                  "CREATE TABLE d(k INT, v TEXT); INSERT INTO d VALUES (1,'a'),(2,'c');")
    d = entry(compare(sp, tp, key=["d=k"]), "d")["data"]
    assert d["mode"] == "fullrow" and d["note"] and d["missing_in_target"] == 1 and d["modified"] == 0


def test_no_common_columns_is_clear_error(tmp_path):
    sp, tp = make(tmp_path, "CREATE TABLE n(x INT);", "CREATE TABLE n(y INT);")
    e = entry(compare(sp, tp), "n")
    assert e["status"] == "对比出错" and "公共列" in e["error"]


def test_ignore_columns(tmp_path):
    sp, tp = make(tmp_path,
                  "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT, ts TEXT); INSERT INTO t VALUES (1,'a','x');",
                  "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT, ts TEXT); INSERT INTO t VALUES (1,'a','y');")
    assert entry(compare(sp, tp), "t")["data"]["modified"] == 1
    assert entry(compare(sp, tp, ignore_columns=["*.ts"]), "t")["data"]["modified"] == 0


def test_count_only_and_schema_only(tmp_path):
    sp, tp = make(tmp_path,
                  "CREATE TABLE t(id INTEGER PRIMARY KEY); INSERT INTO t VALUES (1),(2);",
                  "CREATE TABLE t(id INTEGER PRIMARY KEY); INSERT INTO t VALUES (1);")
    d = entry(compare(sp, tp, count_only=True), "t")["data"]
    assert (d["rows_source"], d["rows_target"], d["missing_in_target"]) == (2, 1, 1)
    assert entry(compare(sp, tp, schema_only=True), "t")["data"] is None


def test_parse_keys():
    assert D.parse_keys(["Orders=a,b; items=x|y"], str.lower) == {"orders": ["a", "b"], "items": ["x", "y"]}


# ---------------- 修复 SQL：对比 → 执行 → 再对比 应完全一致 ----------------
WORDS = ["a", "B", "it's", 'say "hi"', "back\\slash", "中文", "emoji😀", "", "multi\nline", None]


def _row(i, rnd):
    return (i, rnd.choice(WORDS), rnd.choice([None, 1, -5, 3.14159, 0]),
            rnd.choice([None, "2024-01-01 10:00:00", "2024-06-01 00:00:00"]), rnd.choice([None, b"\x00\x01", b""]))


def test_fix_sql_roundtrip(tmp_path):
    rnd = random.Random(7)
    ddl = ("CREATE TABLE f(id INTEGER PRIMARY KEY, txt TEXT, num NUMERIC, ts DATETIME, bin BLOB);"
           "CREATE TABLE g(a INT, b TEXT, v TEXT, PRIMARY KEY(a,b));")
    sp, tp = make(tmp_path, ddl, ddl)
    s, t = sqlite3.connect(sp), sqlite3.connect(tp)
    for i in range(1, 301):
        r = _row(i, rnd)
        s.execute("INSERT INTO f VALUES (?,?,?,?,?)", r)
        x = rnd.random()
        if x < 0.1:
            pass
        elif x < 0.2:
            t.execute("INSERT INTO f VALUES (?,?,?,?,?)", _row(i, rnd))
        else:
            t.execute("INSERT INTO f VALUES (?,?,?,?,?)", r)
        g = (i % 40, f"k{i}", rnd.choice(WORDS))
        s.execute("INSERT INTO g VALUES (?,?,?)", g)
        if rnd.random() > 0.1:
            t.execute("INSERT INTO g VALUES (?,?,?)", g)
    for i in range(301, 330):
        t.execute("INSERT INTO f VALUES (?,?,?,?,?)", _row(i, rnd))
    s.commit(); t.commit(); s.close(); t.close()

    fix = tmp_path / "fix.sql"
    rep = compare(sp, tp, fix_sql=str(fix), with_delete=True, chunk=100)
    assert all(e["status"] != "一致" for e in rep["common"])
    c = sqlite3.connect(tp)
    c.executescript(fix.read_text(encoding="utf-8"))
    c.commit(); c.close()
    assert [e["status"] for e in compare(sp, tp)["common"]] == ["一致", "一致"]


def test_cancel_marks_fix_sql_unsafe(tmp_path):
    ddl = "CREATE TABLE t1(id INTEGER PRIMARY KEY); CREATE TABLE t2(id INTEGER PRIMARY KEY);"
    sp, tp = make(tmp_path, ddl, ddl)
    fix = tmp_path / "fix.sql"

    def cancel_on_table(kind, **k):
        if kind == "table":
            D.CANCEL.set()

    with pytest.raises(D.Cancelled):
        D.run(D.default_args(source=url(sp), target=url(tp), fix_sql=str(fix)), progress=cancel_on_table)
    text = fix.read_text(encoding="utf-8")
    assert "ROLLBACK" in text and "COMMIT" not in text


def test_fix_sql_has_schema_prefix(tmp_path):
    sp, tp = make(tmp_path, "CREATE TABLE t(id INTEGER PRIMARY KEY); INSERT INTO t VALUES (1);",
                  "CREATE TABLE t(id INTEGER PRIMARY KEY);")
    fix = tmp_path / "fix.sql"
    compare(sp, tp, fix_sql=str(fix), tgt_schema="main")
    assert 'INSERT INTO main.t' in fix.read_text(encoding="utf-8")


def test_reports_render(tmp_path):
    sp, tp = make(tmp_path, "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t VALUES (1,'<b>');",
                  "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t VALUES (1,'x');")
    rep = compare(sp, tp)
    html = D.render_html(rep)
    assert "&lt;b&gt;" in html and "<b>" not in html.split("<h2>三")[1]  # HTML 转义
    assert "差异明细" in D.console_text(rep)
