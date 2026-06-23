"""
text_to_sql.py
--------------
Natural-language questions -> validated DuckDB SQL -> results, over the
`spend` table built by load_spend.py.

Design (defence in depth)
-------------------------
1. Schema is read live from DuckDB, so the prompt always matches the real
   table — no hardcoded column list to drift out of sync.
2. The model is given the schema, low-cardinality value hints (e.g. the
   actual department codes), dialect notes, and a few worked examples.
3. Every generated query passes a validation gate: one statement only,
   must start with SELECT/WITH.
4. Execution happens on a READ-ONLY connection — DuckDB itself refuses any
   write, so a bad query can damage nothing.
5. If a query errors, the error is fed back to the model once to self-correct.

Setup
-----
    pip install anthropic duckdb pandas
    export ANTHROPIC_API_KEY=sk-ant-...

Usage
-----
    python text_to_sql.py                         # interactive REPL
    python text_to_sql.py -q "top 10 suppliers by spend"
    python text_to_sql.py --db ./spend.duckdb --model claude-sonnet-4-6
"""

from __future__ import annotations

import argparse
import re
import sys

import duckdb
import pandas as pd
from anthropic import Anthropic

MODEL = "claude-sonnet-4-6"
MAX_ROWS = 1000          # auto-LIMIT applied when the model omits one
LOW_CARD = 30            # columns with <= this many distinct values get value hints


# --------------------------------------------------------------------------
# Schema + context (read live from the database)
# --------------------------------------------------------------------------
def get_schema(con: duckdb.DuckDBPyConnection, table: str) -> str:
    rows = con.execute(f"PRAGMA table_info('{table}')").fetchall()
    cols = ",\n".join(f"    {name} {dtype}" for _, name, dtype, *_ in rows)
    return f"CREATE TABLE {table} (\n{cols}\n);"


def get_value_hints(con: duckdb.DuckDBPyConnection, table: str) -> str:
    """For low-cardinality text columns, list the actual values so the model
    filters on real codes (e.g. department = 'dft', not 'DfT')."""
    rows = con.execute(f"PRAGMA table_info('{table}')").fetchall()
    hints: list[str] = []
    for _, name, dtype, *_ in rows:
        if "VARCHAR" not in dtype.upper():
            continue
        n = con.execute(f"SELECT COUNT(DISTINCT {name}) FROM {table}").fetchone()[0]
        if 0 < n <= LOW_CARD:
            vals = [r[0] for r in con.execute(
                f"SELECT DISTINCT {name} FROM {table} "
                f"WHERE {name} IS NOT NULL ORDER BY 1"
            ).fetchall()]
            hints.append(f"  {name} ({n}): {vals}")
    return "\n".join(hints) if hints else "  (none)"


SYSTEM_TEMPLATE = """\
You translate questions about UK central-government spending into a single \
DuckDB SQL query.

Schema:
{schema}

Low-cardinality column values (filter on these exact strings):
{hints}

Important facts about the data:
- One row per payment over £25,000. Figures are GROSS transactional spend \
(including intra-government transfers), not net budgets.
- `amount` is in GBP. `date` is a TIMESTAMP. `department` holds short \
lowercase codes (see hints above).
- `supplier` is stored UPPER-CASE. Match supplier/category text with ILIKE \
and wildcards, e.g. supplier ILIKE '%capita%'.
- Some `entity` / `expense_*` values may be NULL.

DuckDB dialect notes:
- date_trunc('month', date), year(date), month(date), strftime(date, '%Y-%m').
- ILIKE for case-insensitive matching. Use QUALIFY for window-function filters.

Rules:
- Return ONE SELECT statement. Never write/modify data.
- Use only columns that exist in the schema.
- Add a sensible LIMIT for "top N" style questions.
- Output ONLY the SQL. No prose, no markdown fences, no trailing semicolon \
explanation.

Worked examples:
Q: Who are the top 10 suppliers by total spend?
A: SELECT supplier, SUM(amount) AS total_spend FROM spend GROUP BY supplier ORDER BY total_spend DESC LIMIT 10

Q: What was monthly spend for DfT during 2025?
A: SELECT date_trunc('month', date) AS month, SUM(amount) AS spend FROM spend WHERE department = 'dft' AND year(date) = 2025 GROUP BY month ORDER BY month

Q: How much did each department spend on consultancy?
A: SELECT department, SUM(amount) AS consultancy_spend FROM spend WHERE expense_type ILIKE '%consult%' OR expense_area ILIKE '%consult%' GROUP BY department ORDER BY consultancy_spend DESC

Q: What is the average transaction size by department?
A: SELECT department, AVG(amount) AS avg_txn, COUNT(*) AS n FROM spend GROUP BY department ORDER BY avg_txn DESC
"""


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def generate_sql(client: Anthropic, model: str, system: str,
                 messages: list[dict]) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=600,
        system=system,
        messages=messages,
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return _strip_fences(text)


# --------------------------------------------------------------------------
# Validation + execution
# --------------------------------------------------------------------------
def validate_sql(sql: str) -> str:
    """Structural guard: single statement, read-only shape. The read-only
    connection is the real enforcement; this just fails fast with a clear
    message and blocks statement chaining."""
    sql = sql.strip().rstrip(";").strip()
    if ";" in sql:
        raise ValueError("only a single statement is allowed")
    head = sql.lstrip("(").lstrip().lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise ValueError("only SELECT / WITH queries are allowed")
    return sql


def ensure_limit(sql: str, max_rows: int = MAX_ROWS) -> str:
    if not re.search(r"\blimit\b", sql, re.IGNORECASE):
        return f"{sql}\nLIMIT {max_rows}"
    return sql


def ask(question: str, con: duckdb.DuckDBPyConnection, client: Anthropic,
        system: str, model: str, retries: int = 1) -> tuple[str, pd.DataFrame]:
    messages = [{"role": "user", "content": question}]
    last_err: Exception | None = None
    for _ in range(retries + 1):
        raw = generate_sql(client, model, system, messages)
        try:
            sql = ensure_limit(validate_sql(raw))
            df = con.execute(sql).fetchdf()
            return sql, df
        except Exception as err:                      # noqa: BLE001
            last_err = err
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content":
                 f"That query failed with: {err}. "
                 f"Return a corrected DuckDB SQL query, SQL only."},
            ]
    raise RuntimeError(f"could not produce a working query: {last_err}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Chat with your spend data.")
    ap.add_argument("--db", default="./spend.duckdb")
    ap.add_argument("--table", default="spend")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("-q", "--question", help="ask one question and exit")
    args = ap.parse_args()

    # Read-only connection: the database cannot be modified, full stop.
    con = duckdb.connect(args.db, read_only=True)
    client = Anthropic()

    system = SYSTEM_TEMPLATE.format(
        schema=get_schema(con, args.table),
        hints=get_value_hints(con, args.table),
    )

    pd.set_option("display.max_rows", 50)
    pd.set_option("display.width", 120)
    pd.set_option("display.float_format", lambda v: f"{v:,.2f}")

    def run(q: str) -> None:
        try:
            sql, df = ask(q, con, client, system, args.model)
        except Exception as err:                      # noqa: BLE001
            print(f"  ! {err}")
            return
        print(f"\nSQL:\n{sql}\n")
        print(df.to_string(index=False))
        print(f"\n({len(df)} rows)")

    if args.question:
        run(args.question)
        return

    print("Chat with your spend data. Ctrl-D or 'exit' to quit.\n")
    while True:
        try:
            q = input("ask> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if q.lower() in {"exit", "quit"}:
            break
        if q:
            run(q)
            print()


if __name__ == "__main__":
    main()
