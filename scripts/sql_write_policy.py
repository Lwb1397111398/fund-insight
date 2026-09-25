# -*- coding: utf-8 -*-
""""这条 SQL 到底在写哪一列、写进去的是什么" —— 三处棘轮共用的一份实现。

为什么要有这个文件（第 48 轮两份评审共同点到，A-4 / B-2）：
上一轮我把裸 SQL 的"写这一列"判据补进 `is_correct` / `fund_code` 两条棘轮时，
在 `test_verdict_evidence_badge.py` 里写了一份 `_raw_sql_write_hits`，
而**授予豁免**那条棘轮（`reviewed_by='owner'` + `owner_locked`）仍然用它自己那份正则
`_RAW_SQL_GRANT`。两份实现一好一坏：

  * 好的那份知道"只看 SET 子句" ⇒ `UPDATE … SET status = 1 WHERE owner_locked = true`
    是**读**那一列定位行；
  * 坏的那份不知道 ⇒ 同一个句子在授予棘轮眼里是**三处授予**（实测 `found=3`），
    而真正的授予 `SET reviewed_by = :who, owner_locked = :ok` + 参数字典
    （`{"who": "owner", "ok": True}`，本仓 SQLAlchemy 的常见写法）**一处都不算**。

⇒ 一头是把正常读判成授予（闸门建成墙，将来没人信它），一头是新加一条豁免来源却全绿。
两个方向都得修，而且修法是同一件事：**只留一份判据**。

三档结论（与 `scripts/migration_policy.py` 同一个哲学：**看不见就当可疑，不猜"没事"**）：
  `granted`  —— 看得见写进去的就是那个授予值（`true` / `'t'` / `1` / `'owner'` / 绑定参数的真值）；
  `unclear`  —— 那一列被 SET 了，但值在变量 / 表达式 / 我解不开的绑定参数里 ⇒ 要人写依据；
  其它        —— 没碰这一列（含只在 WHERE 里出现）。
"""
import ast
import re

# 免疫豁免的两列：库里读的是 `if getattr(row, 'owner_locked', None)` ⇒ **真值**即免疫
IMMUNITY_COLUMNS = {'owner_locked', 'reviewed_by'}
# `is_correct` / `fund_code` 两条"唯一入口"的列
GUARDED_COLUMNS = {'is_correct', 'fund_code'}

# PG 的布尔字面量有 `t` / `true` / `'t'` / `1` 好几种合法写法（第 48 轮 B-2 点名少了 `'t'`）
_TRUTHY_SQL = re.compile(r"^\s*(?:true|'t'|t|yes|1|'1'|\"1\")\s*$", re.I)
_OWNER_SQL = re.compile(r"^\s*(?:'owner'|\"owner\"|owner)\s*$", re.I)
_BIND = re.compile(r'^\s*[:@]\s*(\w+)\s*$')
# `UPDATE t SET a = 1, b = 2 WHERE …` / `INSERT INTO t (a, b) VALUES (…)`
_SET_CLAUSE = re.compile(r'\bset\b(.*?)(?:\bwhere\b|$)', re.I | re.S)
_INSERT_LIST = re.compile(r'\binsert\s+into\s+[\w."]+\s*\(([^)]*)\)', re.I)
_PAIR = re.compile(r'([a-zA-Z_][\w]*)\s*=\s*([^,]*)')
_DANGEROUS_STMT = re.compile(r'\b(update|insert)\b', re.I)


def sql_texts(node):
    """一个调用节点里**所有**能看见的 SQL 文本（含 f-string 片段、拼接片段、关键字参数）。"""
    return [c.value for c in ast.walk(node)
            if isinstance(c, ast.Constant) and isinstance(c.value, str)]


def set_targets(text):
    """这段 SQL 的 **SET 子句 / INSERT 列清单** 里被赋值的那些列 → 值的原文。

    只认写入位置（第 48 轮 A-4 的根因就是没这条区分）：
    `UPDATE m SET status = 1 WHERE owner_locked = true` 只给出 `{'status': '1'}`。
    """
    if not isinstance(text, str) or not _DANGEROUS_STMT.search(text):
        return {}
    out = {}
    for clause in _SET_CLAUSE.findall(text):
        for col, value in _PAIR.findall(clause):
            out[col.lower()] = value.strip()
    head = text.lower()
    if 'insert into' in head:
        for cols in _INSERT_LIST.findall(text):
            for col in cols.split(','):
                col = col.strip().strip('"').lower()
                if col:
                    out.setdefault(col, '?')      # INSERT 的列一定是被写的列
    return out


def _bind_value(node, name):
    """绑定参数 `:name` 在同一个调用里给的真值（`execute(sql, {"who": "owner"})`）。"""
    for holder in [node] + list(ast.walk(node)):
        if not isinstance(holder, ast.Dict):
            continue
        for key, value in zip(holder.keys, holder.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if key.value.lstrip(':').lower() != name.lower():
                continue
            return value
    return None


def _truthy_literal(value):
    """字面量 AST 是不是"真值"（授予值语义，与上一轮的真值语义同一把尺）。"""
    if isinstance(value, ast.Constant):
        v = value.value
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        if isinstance(v, str):
            return v.strip().lower() in ('owner', 'true', 't', '1', 'y', 'yes')
        return False
    if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.Not):
        return _literal_false(value.operand)
    return False


def _literal_false(node):
    return isinstance(node, ast.Constant) and node.value in (False, 0, '', None)


def variable_sqls(node, sql_vars):
    """这个调用的参数里有没有"先前存进变量的那段 SQL"（一跳，见 `resolve_assigned_sql`）。"""
    out = []
    for arg in ast.walk(node):
        if isinstance(arg, ast.Name) and arg.id in sql_vars:
            out.extend(sql_vars[arg.id])
    return out


def classify_sql(node, columns=IMMUNITY_COLUMNS, extra_texts=()):
    """这个调用对 `columns` 里的列做了什么：返回 `(授予的列, 看不清的列)`。

    `授予` = 值看得见且是真值；`看不清` = 列被写了但值来自变量 / 表达式 / 解不开的绑定参数
    —— 这一档**不放过也不硬判**，交给登记表写依据（同 `migration_policy` 的"看不清"）。
    """
    granted, unclear = set(), set()
    for text in list(sql_texts(node)) + list(extra_texts):
        for col, value in set_targets(text).items():
            if col not in columns:
                continue
            if col == 'reviewed_by':
                if _OWNER_SQL.match(value or ''):
                    granted.add(col)
                    continue
            elif _TRUTHY_SQL.match(value or ''):
                granted.add(col)
                continue
            bind = _BIND.match(value or '')
            if bind:
                arg = _bind_value(node, bind.group(1))
                if arg is None:
                    unclear.add(col)                # 值在调用之外，我看不见
                elif _truthy_literal(arg):
                    granted.add(col)                # `:ok` = True ⇒ 就是授予
                else:
                    unclear.add(col)
                continue
            # 值是个表达式（f-string 挖空、`1 == 1`、`len(items) > 0`…）⇒ 不猜
            unclear.add(col)
    return granted, unclear


def writes_column(node, column, extra_texts=()):
    """`is_correct` / `fund_code` 那两条棘轮用的判据：这一列被写了吗。

    列一旦被 SET 就算写，**值是什么不影响"写过"**（那是另一条判据的事）；
    只在 WHERE 里出现不算（第 47 轮立的那条反向对照仍然算数）。
    值在变量里也算写 —— 上一版这条判据看不见"SQL 先存进变量再执行"（本仓常见写法，
    `src/api/main.py:553-560` 就是 `seq_sql = text(f"…")` 然后 `db.execute(seq_sql)`）。
    """
    granted, unclear = classify_sql(node, {column.lower()}, extra_texts)
    return bool(granted or unclear)


def resolve_assigned_sql(tree):
    """模块级 / 函数内的"先把 SQL 存进变量"那一种：返回 {变量名: [SQL 文本]}。

    只解一跳（与 `test_script_db_guards._proves_sqlite` 的"一跳可证"同一尺度）；
    再深的链交给"看不清"那一档，不去猜。
    """
    out = {}
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            targets = [node.targets[0].id]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        else:
            continue
        if value is None:
            continue
        texts = sql_texts(value)
        if any(_DANGEROUS_STMT.search(t or '') for t in texts):
            out.setdefault(targets[0], []).extend(texts)
    return out
