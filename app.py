"""
app.py  —  "Chat With Your Spend Data"
--------------------------------------
A Streamlit front end over the DuckDB `spend` table. Ask a plain-English
question; the text_to_sql layer turns it into validated DuckDB SQL, runs it
read-only, and the answer is shown alongside the exact query that produced it.

Design: answers are drawn as a sheet of accounting analysis paper — feint
ruled rows, red money-column rules, a red double margin, and totals
double-underlined the way a bookkeeper would. Everything around the sheet is
kept quiet.

Reuses the engine from text_to_sql.py rather than duplicating it.

Run:
    pip install streamlit
    export ANTHROPIC_API_KEY=sk-ant-...
    streamlit run app.py
"""

from __future__ import annotations

import html
import inspect
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
SHEET_MAX_ROWS = 100          # larger results fall back to a scrolling grid

DEPT_NAMES = {
    "dft": "Department for Transport",
    "desnz": "Department for Energy Security and Net Zero",
    "dwp": "Department for Work and Pensions",
}

# Palette — accounting analysis paper.
PAPER = "#F3F7F2"     # pale green-white page
SHEET = "#FCFDFB"     # the sheet itself
RULE = "#CFE0D2"      # feint green row ruling
INK = "#1B2A3A"       # blue-black ink
BLUE = "#2D5B8A"      # ink blue: links, first chart series
RED = "#B23A3A"       # ledger red: margin and money-column rules
MUTED = "#5B6B63"
SERIES = [BLUE, RED, "#4F7D5C", "#8A6D2D", "#6B5B8A"]

st.set_page_config(
    page_title="Spend Ledger",
    page_icon="§",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# --------------------------------------------------------------------------
# Look & feel
# --------------------------------------------------------------------------
st.markdown(
    f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');

    :root {{
        --paper: {PAPER}; --sheet: {SHEET}; --rule: {RULE}; --ink: {INK};
        --blue: {BLUE}; --red: {RED}; --muted: {MUTED};
    }}

    /* Text font. Set on containers and inherited, never on bare span/div,
       so Streamlit's icon font is left alone. */
    .stApp, .stApp .stMarkdown, .stApp button, .stApp input,
    .stApp textarea, .stApp label {{
        font-family: 'Instrument Sans', system-ui, sans-serif;
        font-feature-settings: "tnum" 1;
    }}
    [data-testid="stIconMaterial"], [data-testid="stIconMaterial"] * {{
        font-family: 'Material Symbols Rounded' !important;
    }}
    .stApp {{ background: var(--paper); color: var(--ink); }}
    [data-testid="stHeader"] {{ background: transparent; }}
    [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] {{ display: none; }}
    [data-testid="stBottom"] > div, [data-testid="stBottomBlockContainer"] {{
        background: var(--paper);
    }}
    .block-container {{ padding-top: 3.2rem; padding-bottom: 7rem; max-width: 820px; }}

    /* Masthead */
    .mast h1 {{
        font-family: 'Instrument Sans', sans-serif; font-weight: 700;
        font-size: 2.75rem; line-height: 1.05; letter-spacing: -0.025em;
        color: var(--ink); margin: 0 0 .6rem; padding: 0;
    }}
    .mast p {{ font-size: 1.06rem; line-height: 1.55; color: var(--muted);
              max-width: 58ch; margin: 0; }}
    .mast p strong {{ color: var(--ink); font-weight: 600; }}

    /* Coverage timeline */
    .cov {{ margin: 2rem 0 .4rem; }}
    .cov-row {{ display: grid; grid-template-columns: 11.5rem 1fr 8.5rem;
                align-items: center; column-gap: 1rem; padding: .42rem 0;
                border-bottom: 1px solid var(--rule); }}
    .cov-row:first-child {{ border-top: 1px solid var(--rule); }}
    .cov-name {{ font-size: .9rem; line-height: 1.25; color: var(--ink); }}
    .cov-track {{ position: relative; height: 12px; }}
    .cov-band {{ position: absolute; top: -9px; bottom: -9px;
                 background: rgba(45, 91, 138, .09); }}
    .cov-bar {{ position: absolute; top: 3px; height: 6px; background: var(--ink);
                border-radius: 1px; }}
    .cov-fig {{ font-size: .9rem; color: var(--ink); text-align: right;
                white-space: nowrap; line-height: 1.25; }}
    .cov-fig small {{ display: block; font-size: .78rem; color: var(--muted); }}
    .cov-axis {{ display: grid; grid-template-columns: 11.5rem 1fr 8.5rem;
                 column-gap: 1rem; font-size: .8rem; color: var(--muted);
                 padding-top: .35rem; }}
    .cov-axis .ends {{ display: flex; justify-content: space-between; }}
    .cov-note {{ font-size: .86rem; line-height: 1.5; color: var(--muted);
                 margin: .9rem 0 0; max-width: 64ch; }}
    .cov-note b {{ color: var(--blue); font-weight: 600; }}

    /* Example questions: plain ruled lines, like entries waiting in a book */
    .examples-head {{ font-size: .95rem; font-weight: 600; color: var(--ink);
                      margin: 2.6rem 0 .3rem; }}
    .stButton > button {{
        width: 100%; justify-content: flex-start; text-align: left;
        background: transparent; border: none; border-radius: 0;
        border-bottom: 1px solid var(--rule); color: var(--blue);
        padding: .55rem 0; min-height: 0; font-size: 1rem;
    }}
    .stButton > button:hover {{ color: var(--red); background: transparent; }}
    .stButton > button:focus-visible {{ outline: 2px solid var(--blue);
                                        outline-offset: 2px; }}
    .stButton > button > div, .stButton > button [data-testid="stMarkdownContainer"] {{
        justify-content: flex-start; text-align: left; width: 100%; }}
    .stButton > button p {{ font-size: 1rem; text-align: left; }}

    /* Index of asked questions, like a ledger's contents page */
    .index-head {{ font-size: .95rem; font-weight: 600; color: var(--ink);
                   margin: 2.4rem 0 .4rem; }}
    ol.index, .stMarkdown ol.index {{ list-style: none; padding: 0 !important;
                                      margin: 0 !important; counter-reset: q; }}
    .stMarkdown ol.index li {{ margin: 0; padding-left: 0; }}
    ol.index li {{ counter-increment: q; display: grid;
                   grid-template-columns: 2rem 1fr; padding: .45rem 0;
                   border-bottom: 1px solid var(--rule); font-size: 1rem; }}
    ol.index li::before {{ content: counter(q); color: var(--muted);
                           font-size: .88rem; padding-top: .1rem; }}
    ol.index a, .totop a {{ color: var(--blue); text-decoration: none; }}
    ol.index a:hover, .totop a:hover {{ color: var(--red); text-decoration: underline; }}
    .totop {{ font-size: .88rem; margin: .6rem 0 0; text-align: right; }}
    .st-key-reset .stButton > button, .st-key-reset2 .stButton > button {{
        width: auto; border: 1px solid var(--ink); border-radius: 4px;
        color: var(--ink); padding: .35rem .9rem; margin-top: .9rem;
    }}
    .st-key-reset .stButton > button:hover, .st-key-reset2 .stButton > button:hover {{
        border-color: var(--red); color: var(--red); }}

    /* An answered question */
    .q {{ font-size: 1.32rem; font-weight: 600; line-height: 1.3;
          letter-spacing: -0.01em; color: var(--ink);
          margin: 3rem 0 1rem; padding-top: 1.6rem; scroll-margin-top: 1rem;
          border-top: 2px solid var(--ink); }}
    .rowcount {{ font-size: .85rem; color: var(--muted); margin: .45rem 0 0; }}
    .sql-head {{ font-size: .88rem; color: var(--muted); margin: 1.4rem 0 .3rem; }}
    .stCode pre, .stCode code {{ font-family: 'IBM Plex Mono', monospace !important;
                                 font-size: .82rem !important; }}
    .stCode pre {{ background: #E8F0E9 !important; }}

    /* The ledger sheet */
    .sheet-wrap {{ overflow-x: auto; }}
    table.sheet {{
        width: 100%; border-collapse: collapse; background: var(--sheet);
        border: 1px solid var(--rule);
        box-shadow: inset 6px 0 0 -3px var(--sheet), inset 7px 0 0 -3px var(--red),
                    inset 10px 0 0 -3px var(--sheet), inset 11px 0 0 -3px var(--red);
        font-size: .93rem;
    }}
    .sheet th {{ text-align: left; font-weight: 600; color: var(--ink);
                 padding: .7rem .8rem .5rem; border-bottom: 1.5px solid var(--ink);
                 font-size: .84rem; }}
    .sheet th:first-child, .sheet td:first-child {{ padding-left: 1.6rem; }}
    .sheet td {{ padding: .5rem .8rem; border-bottom: 1px solid var(--rule);
                 color: var(--ink); vertical-align: top; }}
    .sheet .num {{ text-align: right; white-space: nowrap;
                   border-left: 1px solid rgba(178, 58, 58, .35); }}
    .sheet th.num {{ border-left: 1px solid rgba(178, 58, 58, .35); }}
    .sheet tfoot td {{ font-weight: 600; border-bottom: none; padding-top: .55rem; }}
    .sheet tfoot td.num span {{ display: inline-block; padding: .1rem 0 .15rem;
        border-top: 1px solid var(--ink); border-bottom: 3px double var(--ink); }}
    .sheet .neg {{ color: var(--red); }}

    @media (max-width: 640px) {{
        .mast h1 {{ font-size: 2.1rem; }}
        .cov-row, .cov-axis {{ grid-template-columns: 1fr; row-gap: .3rem; }}
        .cov-fig {{ text-align: left; }}
        .cov-track {{ margin: .55rem 0; }}
        .cov-axis > div:first-child, .cov-axis > div:last-child {{ display: none; }}
    }}
    @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
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
        SELECT COUNT(*), SUM(amount), COUNT(DISTINCT department),
               MIN(date), MAX(date)
        FROM {TABLE}
    """).fetchone()
    dep = con.execute(f"""
        SELECT department, MIN(date) AS lo, MAX(date) AS hi,
               COUNT(*) AS n, SUM(amount) AS spend
        FROM {TABLE} GROUP BY department ORDER BY spend DESC
    """).fetchdf()
    con.close()
    return {"txns": row[0], "total": row[1], "depts": row[2],
            "lo": row[3], "hi": row[4], "by_dept": dep}


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
def money(v: float) -> str:
    if abs(v) >= 1e9:
        return f"£{v / 1e9:,.1f}bn"
    if abs(v) >= 1e6:
        return f"£{v / 1e6:,.0f}m"
    return f"£{v:,.0f}"


def humanise(col: str) -> str:
    s = str(col).replace("_", " ").strip()
    return s[:1].upper() + s[1:]


MONEY_HINTS = ("spend", "amount", "total", "value", "paid", "payment")
NOT_SUMMABLE = ("avg", "mean", "median", "share", "pct", "percent", "ratio",
                "rank", "count", "n_", "rows", "departments", "year", "month")


def is_money(col: str, s: pd.Series) -> bool:
    return (pd.api.types.is_float_dtype(s)
            or any(k in col.lower() for k in MONEY_HINTS)
            ) and pd.api.types.is_numeric_dtype(s) and not any(
                k in col.lower() for k in ("count", "departments", "rows"))


def summable(col: str) -> bool:
    c = col.lower()
    return any(k in c for k in MONEY_HINTS) and not any(k in c for k in NOT_SUMMABLE)


def fmt_cell(col: str, s: pd.Series, v) -> tuple[str, bool]:
    """Return (text, is_negative)."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return "", False
    if pd.api.types.is_datetime64_any_dtype(s):
        return pd.Timestamp(v).strftime("%b %Y"), False
    if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
        if is_money(col, s):
            return (f"£{v:,.0f}" if v >= 0 else f"(£{abs(v):,.0f})"), v < 0
        if pd.api.types.is_integer_dtype(s):
            return f"{int(v):,}", False
        return f"{v:,.2f}", False
    return str(v), False


def ledger_sheet(df: pd.DataFrame) -> str:
    """Render a result as a ruled ledger sheet (HTML)."""
    num_cols = {c for c in df.columns
                if pd.api.types.is_numeric_dtype(df[c])
                and not pd.api.types.is_bool_dtype(df[c])}
    head = "".join(
        f'<th class="{"num" if c in num_cols else ""}">{html.escape(humanise(c))}</th>'
        for c in df.columns)
    body = []
    for _, r in df.iterrows():
        cells = []
        for c in df.columns:
            text, neg = fmt_cell(c, df[c], r[c])
            cls = " ".join(x for x in ("num" if c in num_cols else "",
                                        "neg" if neg else "") if x)
            cells.append(f'<td class="{cls}">{html.escape(text)}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")

    # Totals line, only where adding up makes sense (a ranking of spend).
    foot = ""
    sum_cols = [c for c in df.columns if c in num_cols and summable(c)]
    if len(df) > 1 and sum_cols:
        cells = []
        for i, c in enumerate(df.columns):
            if c in sum_cols:
                text, _ = fmt_cell(c, df[c], float(df[c].sum()))
                cells.append(f'<td class="num"><span>{html.escape(text)}</span></td>')
            elif i == 0:
                cells.append("<td>Total of rows shown</td>")
            else:
                cells.append(f'<td class="{"num" if c in num_cols else ""}"></td>')
        foot = "<tfoot><tr>" + "".join(cells) + "</tr></tfoot>"

    return (f'<div class="sheet-wrap"><table class="sheet"><thead><tr>{head}'
            f'</tr></thead><tbody>{"".join(body)}</tbody>{foot}</table></div>')


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------
def infer_chart(df: pd.DataFrame):
    if df.empty or len(df) < 2:
        return None
    num = df.select_dtypes("number").columns.tolist()
    if not num:
        return None
    dt = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    # Any non-numeric, non-datetime column is a category. DuckDB returns
    # Arrow-backed strings whose dtype is not the legacy `object`, so an
    # `== object` check silently misses them and collapses the series split.
    cat = [c for c in df.columns
           if not pd.api.types.is_numeric_dtype(df[c])
           and not pd.api.types.is_datetime64_any_dtype(df[c])]
    y = next((c for c in num if any(k in c.lower() for k in MONEY_HINTS)), num[0])
    if dt:
        return ("line", dt[0], y, cat[0] if cat else None)
    if cat and len(df) <= 30:
        return ("bar", cat[0], y, None)
    return None


def _chart_config(chart):
    return (chart.configure(font="Instrument Sans, Source Sans Pro, Helvetica, sans-serif",
                            background="transparent")
            .configure_view(strokeWidth=0)
            .configure_axis(labelColor=MUTED, titleColor=MUTED, gridColor=RULE,
                            domainColor=RULE, tickColor=RULE, labelFontSize=11))


def render_chart(df: pd.DataFrame) -> None:
    chart = infer_chart(df)
    if not chart:
        return
    kind, x, y, color = chart
    yfmt = alt.Axis(labelExpr=(
        "datum.value >= 1e9 ? '£' + format(datum.value / 1e9, ',.0f') + 'bn' : "
        "datum.value >= 1e6 ? '£' + format(datum.value / 1e6, ',.0f') + 'm' : "
        "'£' + format(datum.value, ',.0f')"), tickCount=6)
    if kind == "line":
        enc = dict(x=alt.X(f"{x}:T", title=None),
                   y=alt.Y(f"{y}:Q", title=None, axis=yfmt))
        if color:
            enc["color"] = alt.Color(f"{color}:N", title=None,
                                     scale=alt.Scale(range=SERIES),
                                     legend=alt.Legend(orient="top"))
        c = alt.Chart(df).mark_line(strokeWidth=2.2).encode(**enc).properties(height=280)
    else:
        c = (alt.Chart(df).mark_bar(color=BLUE, size=14)
             .encode(x=alt.X(f"{y}:Q", title=None, axis=yfmt),
                     y=alt.Y(f"{x}:N", sort="-x", title=None,
                             axis=alt.Axis(labelLimit=200)))
             .properties(height=max(160, len(df) * 26)))
    st.altair_chart(_chart_config(c), use_container_width=True)


DEPT_SHORT = {"dft": "DfT", "desnz": "DESNZ", "dwp": "DWP"}


def for_display(df: pd.DataFrame) -> pd.DataFrame:
    """Readable labels: department codes -> DfT / DESNZ / DWP."""
    out = df.copy()
    for c in out.columns:
        if c.lower() in ("department", "dept") and not pd.api.types.is_numeric_dtype(out[c]):
            out[c] = out[c].map(lambda v: DEPT_SHORT.get(v, v))
    return out


def as_sheet(df: pd.DataFrame) -> pd.DataFrame:
    """A time series split by a category reads better as a ledger page: one
    row per period, one column per category (e.g. month x department)."""
    dt = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    num = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    cat = [c for c in df.columns if c not in dt and c not in num]
    if len(dt) == 1 and len(cat) == 1 and len(num) == 1 and df[cat[0]].nunique() <= 8:
        wide = df.pivot_table(index=dt[0], columns=cat[0], values=num[0],
                              aggfunc="sum").reset_index()
        wide.columns = [str(c) for c in wide.columns]
        # keep the money/summable meaning of the value column in each header
        rename = {c: f"{c} {num[0]}" for c in wide.columns[1:]}
        wide = wide.rename(columns=rename)
        return wide
    return df


_CODE_WRAPS = "wrap_lines" in inspect.signature(st.code).parameters


def render_answer(question: str, sql: str, df: pd.DataFrame, idx: int = 0) -> None:
    df = for_display(df)
    sheet = as_sheet(df) if not df.empty else df
    st.markdown(f'<div class="q" id="a{idx}">{html.escape(question)}</div>',
                unsafe_allow_html=True)
    if df.empty:
        st.markdown('<p class="rowcount">No payments matched. Try a wider date '
                    'range, or a different supplier or department.</p>',
                    unsafe_allow_html=True)
    elif len(sheet) <= SHEET_MAX_ROWS:
        st.markdown(ledger_sheet(sheet), unsafe_allow_html=True)
        st.markdown(f'<p class="rowcount">{len(sheet):,} row{"s" if len(sheet) != 1 else ""}'
                    '</p>', unsafe_allow_html=True)
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.markdown(f'<p class="rowcount">{len(df):,} rows</p>',
                    unsafe_allow_html=True)
    if not df.empty:
        try:
            render_chart(df)
        except Exception:                      # noqa: BLE001
            pass   # the sheet is already shown; a chart quirk shouldn't break it
    st.markdown('<p class="sql-head">The SQL that produced this answer</p>',
                unsafe_allow_html=True)
    if _CODE_WRAPS:
        st.code(sql, language="sql", wrap_lines=True)
    else:
        st.code(sql, language="sql")
    st.markdown('<p class="totop"><a href="#top">Back to the top</a></p>',
                unsafe_allow_html=True)


def coverage_html(stats: dict) -> str:
    dep = stats["by_dept"]
    lo, hi = pd.Timestamp(stats["lo"]), pd.Timestamp(stats["hi"])
    span = max((hi - lo).total_seconds(), 1)

    def pct(t) -> float:
        return 100 * (pd.Timestamp(t) - lo).total_seconds() / span

    o_lo, o_hi = pd.Timestamp(dep["lo"].max()), pd.Timestamp(dep["hi"].min())
    has_overlap = len(dep) > 1 and o_lo < o_hi
    band = (f'<div class="cov-band" style="left:{pct(o_lo):.2f}%;'
            f'width:{pct(o_hi) - pct(o_lo):.2f}%"></div>') if has_overlap else ""

    rows = []
    for _, r in dep.iterrows():
        name = DEPT_NAMES.get(r["department"], r["department"].upper())
        left, width = pct(r["lo"]), pct(r["hi"]) - pct(r["lo"])
        rows.append(
            f'<div class="cov-row"><div class="cov-name">{html.escape(name)}</div>'
            f'<div class="cov-track">{band}<div class="cov-bar" '
            f'style="left:{left:.2f}%;width:{max(width, .8):.2f}%"></div></div>'
            f'<div class="cov-fig">{money(r["spend"])}<small>{int(r["n"]):,} payments'
            f'</small></div></div>')
    axis = (f'<div class="cov-axis"><div></div><div class="ends">'
            f'<span>{lo:%b %Y}</span><span>{hi:%b %Y}</span></div><div></div></div>')
    note = ""
    if has_overlap:
        note = (f'<p class="cov-note">Each department publishes over a different '
                f'period. They all overlap from <b>{o_lo:%B %Y} to {o_hi:%B %Y}</b> '
                f'(shaded), so compare departments within that window. Figures '
                f'are gross payments, including transfers between public bodies, '
                f'not departmental budgets.</p>')
    return f'<div class="cov">{"".join(rows)}{axis}</div>{note}'


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
engine = get_engine()
if engine is None:
    st.markdown('<div class="mast"><h1>Spend Ledger</h1></div>',
                unsafe_allow_html=True)
    st.error(
        f"No database found at `{DB_PATH}`. Build it with "
        "`python load_spend.py --raw ./data/raw --db ./spend.duckdb`, "
        "then reload this page."
    )
    st.stop()

con, client, system = engine
stats = headline_stats()

st.markdown(
    f"""
    <div class="mast" id="top">
      <h1>Spend Ledger</h1>
      <p>Ask a question about UK government spending in plain English. Every
      answer comes with the SQL that produced it, so you can check the working.
      The ledger holds <strong>{stats['txns']:,} payments over £25,000</strong>,
      worth <strong>{money(stats['total'])}</strong>, from
      {stats['lo']:%B %Y} to {stats['hi']:%B %Y}.</p>
    </div>
    """,
    unsafe_allow_html=True,
)
st.markdown(coverage_html(stats), unsafe_allow_html=True)

if "history" not in st.session_state:
    st.session_state.history = []   # list of {question, sql, df}
if "scroll_to" not in st.session_state:
    st.session_state.scroll_to = None

EXAMPLES = [
    "Top 10 suppliers by total spend",
    "Which suppliers were paid by more than one department?",
    "Monthly spend by department in 2025",
    "Biggest single payments over £50 million",
]
history = st.session_state.history
pending = None


def start_again() -> None:
    st.session_state.history = []
    st.session_state.scroll_to = None


def example_buttons(heading: str, prefix: str) -> str | None:
    asked = {t["question"] for t in history}
    left = [e for e in EXAMPLES if e not in asked]
    if not left:
        return None
    st.markdown(f'<div class="examples-head">{heading}</div>', unsafe_allow_html=True)
    chosen = None
    for i, ex in enumerate(left):
        if st.button(ex, key=f"{prefix}{i}", use_container_width=True):
            chosen = ex
    return chosen


if not history:
    pending = example_buttons("Try one of these, or ask your own below", "ex")
else:
    # Contents: jump to any answer, or clear the page.
    items = "".join(f'<li><a href="#a{i}">{html.escape(t["question"])}</a></li>'
                    for i, t in enumerate(history))
    st.markdown(f'<div class="index-head">Your questions</div>'
                f'<ol class="index">{items}</ol>', unsafe_allow_html=True)
    with st.container(key="reset"):
        st.button("Clear answers and start again", on_click=start_again,
                  key="reset_btn")

for i, turn in enumerate(history):
    render_answer(turn["question"], turn["sql"], turn["df"], i)

if history:
    pending = example_buttons("Ask another", "more") or pending
    with st.container(key="reset2"):
        st.button("Clear answers and start again", on_click=start_again,
                  key="reset_btn2")

# After a new answer, bring it into view (the page otherwise stays put).
if st.session_state.scroll_to is not None:
    import streamlit.components.v1 as components
    components.html(
        f"""<script>
        // Streamlit pins the page to the bottom after a rerun when a chat
        // input is present, so scroll after it has settled.
        const go = () => {{
          const el = window.parent.document.getElementById("a{st.session_state.scroll_to}");
          if (el) el.scrollIntoView({{behavior: "auto", block: "start"}});
        }};
        setTimeout(go, 350); setTimeout(go, 900);
        </script>""",
        height=0,
    )
    st.session_state.scroll_to = None

prompt = st.chat_input("Ask about suppliers, departments or time periods")
prompt = prompt or pending

if prompt:
    with st.spinner("Writing and running the query…"):
        try:
            sql, df = ask(prompt, con, client, system, MODEL)
        except Exception as err:          # noqa: BLE001
            st.markdown(f'<div class="q">{html.escape(prompt)}</div>',
                        unsafe_allow_html=True)
            st.error(
                "That question couldn't be turned into a working query. Try "
                "naming a supplier, department or time period.\n\n"
                f"Details: `{err}`"
            )
            st.stop()
    history.append({"question": prompt, "sql": sql, "df": df})
    st.session_state.scroll_to = len(history) - 1
    st.rerun()
