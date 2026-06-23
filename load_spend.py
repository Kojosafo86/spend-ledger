"""
load_spend.py
-------------
Reconcile UK central-government "spend over £25k" CSVs from multiple
departments into one clean, queryable DuckDB table.

Why this exists
---------------
Every department publishes the same *conceptual* data under slightly
different column names, encodings, and date formats. This script absorbs
that mess and emits a single canonical schema:

    department, entity, date, expense_type, expense_area,
    supplier, transaction_number, amount, description,
    supplier_postcode, source_file

Usage
-----
    python load_spend.py --raw ./data/raw --db ./spend.duckdb

Expected layout (department is read from the sub-folder name):
    data/raw/dft/*.csv
    data/raw/dwp/*.csv
    data/raw/desnz/*.csv

Dependencies: pandas, duckdb   (both already in your `datasci` env)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import duckdb
import pandas as pd

# --------------------------------------------------------------------------
# Canonical schema + header synonyms
# --------------------------------------------------------------------------
# Keys are the canonical column names. Values are the set of *normalised*
# source headers (lowercased, alphanumeric only) that map onto them.
# Add to these sets as you meet new departmental quirks.
SYNONYMS: dict[str, set[str]] = {
    "entity": {
        "entity", "department", "departmentfamily", "departmentalfamily",
        "organisation", "organization", "bodyname",
    },
    "date": {
        "date", "transactiondate", "paymentdate", "postingdate",
        "invoicedate", "clearingdate", "documentdate", "dateofpayment",
    },
    "expense_type": {
        "expensetype", "expendituretype", "subjective", "subjectivecode",
        "subjectiveheading", "categoryl2", "expensecategory",
    },
    "expense_area": {
        "expensearea", "costcentre", "costcenter", "directorate",
        "businessunit", "responsibilitygroup", "team",
    },
    "supplier": {
        "supplier", "suppliername", "vendor", "vendorname", "payee",
        "beneficiary", "merchant", "merchantname",
    },
    "transaction_number": {
        "transactionnumber", "transactionno", "transno", "documentno",
        "documentnumber", "paymentref", "paymentreference",
        "invoicenumber", "invoiceno",
    },
    "amount": {
        "amount", "amountf", "amountgbp", "amount£", "apamount",
        "value", "netamount", "grossamount", "spend", "totalamount",
    },
    "description": {
        "narrative", "description", "details", "expensedescription",
        "linedescription", "transactiondescription", "itemtext",
    },
    "supplier_postcode": {
        "supplierpostcode", "postcode", "vendorpostcode", "postalcode",
    },
}

# Reverse lookup: normalised source header -> canonical name
NORM_TO_CANON: dict[str, str] = {
    src: canon for canon, srcs in SYNONYMS.items() for src in srcs
}

CANONICAL_ORDER = [
    "department", "entity", "date", "expense_type", "expense_area",
    "supplier", "transaction_number", "amount", "description",
    "supplier_postcode", "source_file",
]

# Tokens that, if present in a row, signal it is the real header row.
HEADER_HINTS = {"supplier", "amount", "date", "expense"}


def _norm(s: str) -> str:
    """Lowercase + strip everything that isn't a letter/number."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _find_header_row(path: Path, encoding: str, max_scan: int = 10) -> int:
    """
    Some departments prepend a title/blank row before the real header.
    Scan the first few rows and return the index of the one that looks
    like a header (contains a couple of known column tokens).
    """
    with open(path, encoding=encoding, errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= max_scan:
                break
            tokens = {_norm(c) for c in line.split(",")}
            hits = sum(any(h in t for t in tokens) for h in HEADER_HINTS)
            if hits >= 2:
                return i
    return 0  # fall back to first row


def _read_csv(path: Path) -> pd.DataFrame | None:
    """Read one CSV defensively: try UTF-8, then latin-1; locate header."""
    for enc in ("utf-8-sig", "latin-1"):
        try:
            header_row = _find_header_row(path, enc)
            df = pd.read_csv(
                path,
                encoding=enc,
                skiprows=header_row,
                dtype=str,
                on_bad_lines="skip",
            )
            return df
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
    print(f"  ! could not parse {path.name}", file=sys.stderr)
    return None


def _clean_amount(series: pd.Series) -> pd.Series:
    """'£1,234.50', '(500)', ' 25000 ' -> float. Parens = negative."""
    s = series.astype(str).str.strip()
    neg = s.str.startswith("(") & s.str.endswith(")")
    s = s.str.replace(r"[£,()\s]", "", regex=True)
    out = pd.to_numeric(s, errors="coerce")
    out.loc[neg] = -out.loc[neg].abs()
    return out


def _money_score(series: pd.Series) -> tuple[float, float]:
    """Return (fraction parseable as number, median magnitude) for a column."""
    vals = _clean_amount(series)
    frac = float(vals.notna().mean())
    median_mag = float(vals.abs().median()) if vals.notna().any() else 0.0
    return frac, median_mag


def _canonicalise(df: pd.DataFrame, department: str, source_file: str) -> pd.DataFrame:
    """Map a raw departmental frame onto the canonical schema."""
    rename: dict[str, str] = {}
    for col in df.columns:
        canon = NORM_TO_CANON.get(_norm(col))
        if canon and canon not in rename.values():
            rename[col] = canon

    # Some files have a broken amount header (e.g. DfT publishes it as "?").
    # If nothing mapped to amount, infer it: the unmapped column whose values
    # look most like money. The "over £25k" floor makes this safe — a real
    # amount column has a high median magnitude, unlike refs or postcodes.
    if "amount" not in rename.values():
        best_col, best_frac = None, 0.0
        for col in df.columns:
            if col in rename:                       # already a known field
                continue
            frac, median_mag = _money_score(df[col])
            if frac >= 0.80 and median_mag >= 1000 and frac > best_frac:
                best_col, best_frac = col, frac
        if best_col is not None:
            rename[best_col] = "amount"
            print(f"    inferred amount column from values: {best_col!r}")
        else:
            print(f"    ! WARNING: no amount column found in {source_file}")

    df = df.rename(columns=rename)

    # Keep only columns we recognise; add any missing canon cols as NA.
    for canon in SYNONYMS:
        if canon not in df.columns:
            df[canon] = pd.NA
    df = df[list(SYNONYMS.keys())].copy()

    df["amount"] = _clean_amount(df["amount"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce", dayfirst=True)
    df["supplier"] = df["supplier"].astype(str).str.strip().str.upper()

    df.insert(0, "department", department)
    df["source_file"] = source_file
    return df[CANONICAL_ORDER]


def load(raw_dir: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    csv_paths = sorted(raw_dir.rglob("*.csv"))
    if not csv_paths:
        sys.exit(f"No CSVs found under {raw_dir}. Check the path/layout.")

    for path in csv_paths:
        # department = the immediate sub-folder under raw/ (e.g. raw/dft/x.csv)
        try:
            department = path.relative_to(raw_dir).parts[0]
        except ValueError:
            department = "unknown"
        print(f"  reading {department}/{path.name}")
        raw = _read_csv(path)
        if raw is None or raw.empty:
            continue
        frames.append(_canonicalise(raw, department, path.name))

    if not frames:
        sys.exit("Nothing loaded — every file failed to parse.")

    df = pd.concat(frames, ignore_index=True)
    # Drop rows with no amount or no supplier — usually footers/blanks.
    df = df.dropna(subset=["amount", "supplier"])
    df = df[df["supplier"] != "NAN"]
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description="Load UK spend CSVs into DuckDB.")
    ap.add_argument("--raw", default="./data/raw", type=Path)
    ap.add_argument("--db", default="./spend.duckdb", type=Path)
    ap.add_argument("--table", default="spend")
    args = ap.parse_args()

    print(f"Scanning {args.raw} ...")
    df = load(args.raw)

    con = duckdb.connect(str(args.db))
    con.execute(f"DROP TABLE IF EXISTS {args.table}")
    con.execute(f"CREATE TABLE {args.table} AS SELECT * FROM df")
    con.close()

    # Summary
    print("\nLoaded OK.")
    print(f"  rows         : {len(df):,}")
    print(f"  departments  : {df['department'].nunique()} "
          f"({', '.join(sorted(df['department'].unique()))})")
    if df["date"].notna().any():
        print(f"  date range   : {df['date'].min():%Y-%m-%d} -> {df['date'].max():%Y-%m-%d}")
    print(f"  total spend  : £{df['amount'].sum():,.0f}")
    print(f"  null dates   : {df['date'].isna().sum():,}")
    print("\n  per department:")
    by_dept = df.groupby("department").agg(
        rows=("amount", "size"), spend=("amount", "sum")
    )
    for dept, row in by_dept.iterrows():
        print(f"    {dept:<10} {int(row['rows']):>8,} rows   £{row['spend']:>16,.0f}")
    print(f"\n  -> {args.db} (table: {args.table})")


if __name__ == "__main__":
    main()
