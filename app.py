"""
app.py  —  "Chat With Your Spend Data"
--------------------------------------
A Streamlit front end over the DuckDB `spend` table. Ask a plain-English
question; the text_to_sql layer turns it into validated DuckDB SQL, runs it
read-only, and the answer is shown alongside the exact query that produced it.

Reuses the engine from text_to_sql.py rather than duplicating it.

Run:
    pip install streamlit
    export ANTHROPIC_API_KEY=sk-ant-...
    mkdir -p .streamlit && mv config.toml .streamlit/      # theme (optional)
    streamlit run app.py
"""

from __future__ import annotations

import os

import altair as alt
import duckdb
import pandas as pd
import streamlit as st
from anthropic import Anthropic

from text_to_sql import (
    SYSTEM_TEMPLATE,
    ask,
    get_schema,
    get_value_hints,
    MODEL,
)

DB_PATH = os.environ.get("SPEND_DB", "./spend.duckdb")
TABLE = "spend"

st.set_page_config(
    page_title="Spend Ledger",
    page_icon="§",
    layout="wide",
)

# --------------------------------------------------------------------------
# Look & feel — institutional ledger, not a chatbot
# --------------------------------------------------------------------------
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

    html, body, [class*="st-"], .stMarkdown, p, span, label, div {
        font-family: 'IBM Plex Sans', system-ui, sans-serif;
    }
    .block-container { padding-top: 2.2rem; max-width: 1100px; }

    .led-mast { border-bottom: 2px solid #B08D3F; padding-bottom: .55rem; margin-bottom: 1.1rem; }
    .led-mast h1 {
        font-family: 'IBM Plex Serif', serif; font-weight: 600;
        color: #14242E; font-size: 2.1rem; letter-spacing: -.01em; margin: 0;
    }
    .led-mast .sub { color: #5C6B73; font-size: .95rem; margin-top: .15rem; }

    .eyebrow {
        font-family: 'IBM Plex Mono', monospace; font-size: .7rem;
        letter-spacing: .18em; text-transform: uppercase; color: #B08D3F;
        margin: .2rem 0 .35rem;
    }

    .readout { display: flex; flex-wrap: wrap; gap: 0; border: 1px solid #DED8CB;
               border-radius: 8px; overflow: hidden; margin-bottom: 1.4rem; }
    .readout .cell { flex: 1 1 0; min-width: 150px; padding: .85rem 1.1rem;
                     border-right: 1px solid #DED8CB; background: #FBFAF6; }
    .readout .cell:last-child { border-right: none; }
    .readout .num { font-family: 'IBM Plex Mono', monospace; font-weight: 600;
                    font-size: 1.45rem; color: #14242E; line-height: 1.1; }
    .readout .lab { font-size: .72rem; letter-spacing: .12em; text-transform: uppercase;
                    color: #7A8891; margin-top: .25rem; }

    .prov { font-size: .85rem; color: #3A4A52; line-height: 1.5; }
    .prov code { background: #ECE8DF; padding: .05rem .3rem; border-radius: 3px;
                 font-family: 'IBM Plex Mono', monospace; }
    .caveat { border-left: 3px solid #B08D3F; padding: .5rem .7rem; background: #FBFAF6;
              font-size: .82rem; color: #4A5A62; margin-top: .8rem; border-radius: 0 6px 6px 0; }

    [data-testid="stChatMessage"] { background: transparent; }
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------
# Cached engine: one connection, one client, one introspected prompt
# --------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_engine():
    if not os.path.exists(DB_PATH):
        return None
    con = duckdb.connect(DB_PATH, read_only=True)
    client = Anthropic()
    system = SYSTEM_TEMPLATE.format(
        schema=get_schema(con, TABLE),
        hints=get_value_hints(con, TABLE),
    )
    return con, client, system


@st.cache_data(show_spinner=False)
def headline_stats() -> dict:
    con = duckdb.connect(DB_PATH, read_only=True)
    row = con.execute(f"""
        SELECT COUNT(*)                          AS txns,
               SUM(amount)                       AS total,
               COUNT(DISTINCT department)        AS depts,
               MIN(date)                         AS lo,
               MAX(date)                         AS hi
        FROM {TABLE}
    """).fetchone()
    dep = con.execute(f"""
        SELECT department, MIN(date) AS lo, MAX(date) AS hi,
               COUNT(*) AS n, SUM(amount) AS spend
        FROM {TABLE} GROUP BY department ORDER BY department
    """).fetchdf()
    con.close()
    return {"txns": row[0], "total": row[1], "depts": row[2],
            "lo": row[3], "hi": row[4], "by_dept": dep}


# --------------------------------------------------------------------------
# Result rendering: SQL audit trail -> table -> auto chart
# --------------------------------------------------------------------------
def infer_chart(df: pd.DataFrame):
    if df.empty or len(df) < 2:
        return None
    num = df.select_dtypes("number").columns.tolist()
    if not num:
        return None
    dt = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    cat = [c for c in df.columns if df[c].dtype == object]
    # Prefer a spend/amount/total column as the value to plot, else first numeric.
    y = next((c for c in num
              if any(k in c.lower() for k in ("spend", "amount", "total", "value"))),
             num[0])
    if dt:
        return ("line", dt[0], y, cat[0] if cat else None)
    if cat and len(df) <= 30:
        return ("bar", cat[0], y, None)
    return None


def style_money(df: pd.DataFrame):
    fmt = {}
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            fmt[c] = lambda v: v.strftime("%b %Y") if pd.notna(v) else ""
        elif pd.api.types.is_float_dtype(df[c]):
            fmt[c] = "£{:,.0f}"
        elif pd.api.types.is_integer_dtype(df[c]):
            fmt[c] = "{:,}"
    return df.style.format(fmt)


def render_answer(sql: str, df: pd.DataFrame):
    st.markdown('<div class="eyebrow">Generated SQL · audit trail</div>',
                unsafe_allow_html=True)
    st.code(sql, language="sql")

    if df.empty:
        st.info("That query ran but returned no rows. Try widening the "
                "time period, or name a different supplier or department.")
        return

    st.dataframe(style_money(df), use_container_width=True, hide_index=True)

    chart = infer_chart(df)
    try:
        if chart and chart[0] == "line":
            _, x, y, color = chart
            enc = dict(
                x=alt.X(f"{x}:T", title=None),
                y=alt.Y(f"{y}:Q", title=None, axis=alt.Axis(format="~s")),
            )
            if color:
                # Visible proof of which code is running + a real legend.
                st.caption(
                    f"{df[color].nunique()} series — "
                    + ", ".join(map(str, sorted(df[color].unique())))
                )
                enc["color"] = alt.Color(
                    f"{color}:N", title=None,
                    scale=alt.Scale(range=["#B08D3F", "#14242E", "#5C6B73"]),
                )
            line = (alt.Chart(df).mark_line(strokeWidth=2.5)
                    .encode(**enc).properties(height=300))
            st.altair_chart(line, use_container_width=True)
        elif chart and chart[0] == "bar":
            _, x, y, _ = chart
            bar = (alt.Chart(df).mark_bar(color="#B08D3F", cornerRadius=2)
                   .encode(x=alt.X(f"{y}:Q", title=None, axis=alt.Axis(format="~s")),
                           y=alt.Y(f"{x}:N", sort="-x", title=None))
                   .properties(height=max(220, len(df) * 30)))
            st.altair_chart(bar, use_container_width=True)
    except Exception:                          # noqa: BLE001
        pass   # table already shown; a charting quirk shouldn't break the answer


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
st.markdown(
    '<div class="led-mast"><h1>Spend Ledger</h1>'
    '<div class="sub">Ask UK central-government spending over £25,000 in plain English. '
    'Every answer shows the query that produced it.</div></div>',
    unsafe_allow_html=True,
)

engine = get_engine()
if engine is None:
    st.error(
        f"No database found at `{DB_PATH}`. Build it first with "
        "`python load_spend.py --raw ./data/raw --db ./spend.duckdb`, "
        "then reload this page."
    )
    st.stop()

con, client, system = engine
stats = headline_stats()

# Headline readout — the page opens on the real data, not an empty box.
st.markdown(
    f"""
    <div class="readout">
      <div class="cell"><div class="num">{stats['txns']:,}</div>
        <div class="lab">Transactions</div></div>
      <div class="cell"><div class="num">£{stats['total']/1e9:.1f}bn</div>
        <div class="lab">Total value</div></div>
      <div class="cell"><div class="num">{stats['depts']}</div>
        <div class="lab">Departments</div></div>
      <div class="cell"><div class="num">{stats['lo']:%b %Y}–{stats['hi']:%b %Y}</div>
        <div class="lab">Coverage</div></div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Sidebar — provenance and the honesty caveat, always visible.
with st.sidebar:
    st.markdown('<div class="eyebrow">Provenance</div>', unsafe_allow_html=True)
    st.markdown('<div class="prov">Source: published departmental spend over '
                '£25k (Open Government Licence).</div>', unsafe_allow_html=True)
    for _, r in stats["by_dept"].iterrows():
        st.markdown(
            f'<div class="prov" style="margin-top:.5rem">'
            f'<code>{r["department"]}</code> &nbsp; {int(r["n"]):,} rows · '
            f'£{r["spend"]/1e9:.1f}bn<br>'
            f'<span style="color:#7A8891">{r["lo"]:%b %Y} – {r["hi"]:%b %Y}</span>'
            f'</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="caveat">Figures are <b>gross transactional</b> spend — every '
        'payment over £25k, including transfers between public bodies — not net '
        'budgets. Department coverage windows differ; compare like-for-like over '
        'the overlapping period.</div>', unsafe_allow_html=True)

# Conversation state
if "history" not in st.session_state:
    st.session_state.history = []   # list of {role, question, sql, df}

for turn in st.session_state.history:
    with st.chat_message("user"):
        st.write(turn["question"])
    with st.chat_message("assistant"):
        render_answer(turn["sql"], turn["df"])

# Example prompts as an empty-state invitation
EXAMPLES = [
    "Top 10 suppliers by total spend",
    "Monthly spend trend by department in 2025",
    "Which suppliers were paid by both departments, and how much each?",
    "Biggest single payments over £50 million",
]
pending = None
if not st.session_state.history:
    st.markdown('<div class="eyebrow">Try asking</div>', unsafe_allow_html=True)
    cols = st.columns(2)
    for i, ex in enumerate(EXAMPLES):
        if cols[i % 2].button(ex, use_container_width=True, key=f"ex{i}"):
            pending = ex

prompt = st.chat_input("Ask about suppliers, departments, categories, time periods…")
prompt = prompt or pending

if prompt:
    with st.chat_message("user"):
        st.write(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Composing and running the query…"):
            try:
                sql, df = ask(prompt, con, client, system, MODEL)
            except Exception as err:          # noqa: BLE001
                st.error(
                    "Couldn't form a valid query for that one. Try naming a "
                    "supplier, department, category, or time period — or "
                    f"rephrasing.\n\n`{err}`"
                )
                st.stop()
        render_answer(sql, df)
    st.session_state.history.append(
        {"question": prompt, "sql": sql, "df": df}
    )
    if pending:
        st.rerun()
