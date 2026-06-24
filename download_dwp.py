"""DWP spend over £25k downloader (transparency-filtered)."""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SEARCH = "https://www.gov.uk/api/search.json"
CONTENT = "https://www.gov.uk/api/content"
OUT = Path("data/raw/dwp")
HEADERS = {"User-Agent": "Mozilla/5.0 (portfolio data fetch)"}
DEFAULT_LIMIT = 15


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    limit = None if arg == "all" else (int(arg) if arg else DEFAULT_LIMIT)
    OUT.mkdir(parents=True, exist_ok=True)

    qs = urllib.parse.urlencode({
        "filter_organisations": "department-for-work-pensions",
        "filter_content_store_document_type": "transparency",
        "order": "-public_timestamp",
        "count": "200",
        "fields": "link",
    })
    results = get_json(f"{SEARCH}?{qs}").get("results", [])
    pages = [r["link"] for r in results
             if "dwp-spending-over-25000" in r.get("link", "")]

    if not pages:
        print("No DWP spend pages found — the API may have changed.")
        return
    if limit:
        pages = pages[:limit]
    print(f"Found {len(pages)} DWP monthly pages.\n")

    got = 0
    for bp in pages:
        slug = bp.rstrip("/").split("/")[-1]
        try:
            page = get_json(CONTENT + bp)
        except Exception as err:
            print(f"  ! {slug}: {err}")
            continue
        csvs = [a for a in page.get("details", {}).get("attachments", [])
                if a.get("url", "").lower().endswith(".csv")]
        if not csvs:
            print(f"  - no CSV on {slug}")
            continue
        dest = OUT / f"{slug}.csv"
        if dest.exists():
            print(f"  = already have {dest.name}")
            continue
        try:
            req = urllib.request.Request(csvs[0]["url"], headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                dest.write_bytes(resp.read())
            got += 1
            print(f"  + {dest.name}")
        except Exception as err:
            print(f"  ! {dest.name}: {err}")

    print(f"\nDone. {got} new file(s) in {OUT}")


if __name__ == "__main__":
    main()
