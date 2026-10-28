"""
supplier_names.py
-----------------
Supplier-name normalisation (entity resolution) for the spend table.

The same payee appears under many spellings across departments and months:

    CAPITA BUSINESS SERVICES LTD
    Capita Business Services Limited
    CAPITA BUSINESS SERVICES LTD.
    CAPITA BUSINESS SERVCES LTD          <- typo

Grouping on the raw `supplier` column splits that spend four ways, which
understates concentration and hides cross-department suppliers. This module
collapses variants onto one `supplier_clean` name, in three auditable passes:

1. Rules  -- deterministic key: case, punctuation, '&' -> AND, and legal-form
             suffixes (LTD, LIMITED, PLC, LLP, ...) removed.
2. Fuzzy  -- within a block of keys sharing a first word, merge near-identical
             keys (typos), with guards against false merges such as
             SOUTH WEST TRAINS vs SOUTH EAST TRAINS or PHASE 1 vs PHASE 2.
3. Manual -- an optional overrides CSV for merges no algorithm should guess,
             e.g. rolling a subsidiary up to its parent.

Every raw name's mapping, method, and score is written to a `supplier_map`
table, so any merge can be inspected and challenged.

Dependencies: pandas, rapidfuzz
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

# Legal-form and filler tokens dropped from the matching key. Deliberately
# conservative: GROUP, HOLDINGS, SERVICES etc. are kept because they often
# distinguish genuinely different legal entities.
LEGAL_SUFFIXES = {
    "LTD", "LIMITED", "PLC", "LLP", "LP", "INC", "CORP", "CORPORATION",
    "CO", "COMPANY", "THE", "UK", "GB", "CIC", "PLC.", "T/A",
}

# Fuzzy thresholds. Whole-key similarity must clear KEY_THRESHOLD, and every
# word that differs between the two keys must itself be a near-match of at
# least MIN_TOKEN_LEN characters, so short words (EAST/WEST, NORTH/SOUTH,
# A/B) can never be "typo-corrected" into each other.
KEY_THRESHOLD = 92
TOKEN_THRESHOLD = 85
MIN_TOKEN_LEN = 5


def match_key(name: str) -> str:
    """Deterministic matching key for a raw supplier name."""
    s = str(name).upper().strip()
    s = s.replace("&", " AND ")
    # Bracketed text is KEPT: it often distinguishes real entities, e.g.
    # 'HS2 (N1)' vs 'HS2 (N2)' contract lots, or 'ROAD MANAGEMENT SERVICES
    # (GLOUCESTER)' vs '(PETERBOROUGH)'. Filler such as '(UK)' disappears
    # anyway because its tokens are in LEGAL_SUFFIXES.
    s = re.sub(r"[^A-Z0-9 ]", " ", s)            # punctuation -> space
    tokens = [t for t in s.split() if t not in LEGAL_SUFFIXES]
    return " ".join(tokens)


def _digits(s: str) -> list[str]:
    return re.findall(r"\d+", s)


def _safe_fuzzy_match(a: str, b: str) -> int | None:
    """Return a similarity score if `a` and `b` are the same name modulo
    typos, else None. Guards make false merges much harder than misses."""
    if _digits(a) != _digits(b):
        return None                                    # PHASE 1 vs PHASE 2
    ta, tb = a.split(), b.split()
    if len(ta) != len(tb):
        return None                                    # extra word = maybe a different entity
    score = fuzz.ratio(a, b)
    if score < KEY_THRESHOLD:
        return None
    for x, y in zip(ta, tb):
        if x == y:
            continue
        if min(len(x), len(y)) < MIN_TOKEN_LEN:
            return None                                # EAST vs WEST
        if fuzz.ratio(x, y) < TOKEN_THRESHOLD:
            return None
    return int(round(score))


def load_overrides(path: Path | None) -> dict[str, str]:
    """overrides CSV: columns `match` (raw name or match key) and `canonical`."""
    if not path or not Path(path).exists():
        return {}
    out: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            m = (row.get("match") or "").strip()
            c = (row.get("canonical") or "").strip()
            if m and c and not m.startswith("#"):
                out[match_key(m)] = c.upper()
    return out


def build_supplier_map(df: pd.DataFrame,
                       overrides_path: Path | None = None) -> pd.DataFrame:
    """
    Build the raw -> clean mapping from a frame with `supplier` and `amount`.

    Returns one row per distinct raw supplier:
        supplier, supplier_clean, match_key, method, score, raw_spend
    method is one of: exact | rules | fuzzy | manual
    """
    raw = (df.groupby("supplier", dropna=True)["amount"]
             .agg(raw_spend="sum", n="size").reset_index())
    raw["match_key"] = raw["supplier"].map(match_key)
    raw.loc[raw["match_key"] == "", "match_key"] = raw["supplier"]

    # --- Pass 2: fuzzy-merge keys, blocked by first word. Keys are visited
    # biggest-spend first and only ever compared to an existing canonical
    # key (never to each other), so merges can't chain A~B~C into A=C.
    keys = (raw.groupby("match_key")["raw_spend"].sum()
               .sort_values(ascending=False))
    key_to_canon: dict[str, tuple[str, int | None]] = {}
    canon_by_block: dict[str, list[str]] = {}
    for key in keys.index:
        block = key.split()[0] if key.split() else key
        hit = None
        for canon in canon_by_block.get(block, []):
            score = _safe_fuzzy_match(key, canon)
            if score is not None:
                hit = (canon, score)
                break
        if hit:
            key_to_canon[key] = hit
        else:
            key_to_canon[key] = (key, None)
            canon_by_block.setdefault(block, []).append(key)

    raw["canon_key"] = raw["match_key"].map(lambda k: key_to_canon[k][0])
    raw["score"] = raw["match_key"].map(lambda k: key_to_canon[k][1])

    # Display name for each cluster: its most-used raw spelling, tidied.
    # Only spellings that match the cluster head exactly (not via fuzzy
    # typo-matching) are eligible, so a typo never becomes the display name.
    tidy = raw["supplier"].map(
        lambda s: re.sub(r"\s+", " ", str(s).upper()).strip(" .,-"))
    display = (raw.assign(tidy=tidy)
                  .loc[raw["score"].isna()]
                  .sort_values(["n", "raw_spend"], ascending=False)
                  .drop_duplicates("canon_key")
                  .set_index("canon_key")["tidy"])
    raw["supplier_clean"] = raw["canon_key"].map(display)

    # Method: how this raw name reached its clean name.
    raw["method"] = "rules"
    raw.loc[tidy == raw["supplier_clean"], "method"] = "exact"
    raw.loc[raw["score"].notna(), "method"] = "fuzzy"

    # --- Pass 3: manual overrides win over everything.
    # If the override's target is already a cluster, adopt that cluster's
    # display name, so the merge joins it rather than inventing a new name.
    overrides = load_overrides(overrides_path)
    if overrides:
        # Two cases:
        #  - target is in the SAME cluster as the source: the override is
        #    choosing a spelling (e.g. correct one over a typo) -> use it as-is;
        #  - target is in ANOTHER cluster: join it, under its display name.
        key_canon = dict(zip(raw["match_key"], raw["canon_key"]))
        target = {}
        for k, c in overrides.items():
            ck = key_canon.get(match_key(c))
            if ck is None or ck == key_canon.get(k, k):
                target[k] = c
            else:
                target[k] = display.get(ck, c)
        for col in ("match_key", "canon_key"):
            hit = raw[col].isin(target.keys()) & (raw["method"] != "manual")
            raw.loc[hit, "supplier_clean"] = raw.loc[hit, col].map(target)
            raw.loc[hit, "method"] = "manual"

    raw["score"] = raw["score"].astype("Int64")
    return raw[["supplier", "supplier_clean", "match_key", "method",
                "score", "raw_spend"]].sort_values("raw_spend", ascending=False)


def summarise(mapping: pd.DataFrame) -> str:
    n_raw = len(mapping)
    n_clean = mapping["supplier_clean"].nunique()
    by = mapping["method"].value_counts().to_dict()
    return (f"  suppliers    : {n_raw:,} raw names -> {n_clean:,} clean "
            f"({n_raw - n_clean:,} merged; "
            + ", ".join(f"{k} {v:,}" for k, v in sorted(by.items())) + ")")
