"""Render monthly spend by department to assets/spend_by_department.png."""
from pathlib import Path
import duckdb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

con = duckdb.connect("spend.duckdb", read_only=True)
df = con.execute("""
    SELECT date_trunc('month', date) AS month,
           department, SUM(amount) / 1e9 AS spend_bn
    FROM spend GROUP BY 1, 2 ORDER BY 1, 2
""").fetchdf()

# Print exact per-department figures (for the README).
summary = con.execute("""
    SELECT department, COUNT(*) AS rows, ROUND(SUM(amount)/1e9, 1) AS bn,
           MIN(date) AS first, MAX(date) AS last
    FROM spend GROUP BY 1 ORDER BY 1
""").fetchdf()
print(summary.to_string(index=False))

COLORS = {"dft": "#14242E", "desnz": "#B08D3F", "dwp": "#3D7A8C"}
LABELS = {"dft": "DfT (Transport)", "desnz": "DESNZ (Energy)",
          "dwp": "DWP (Work & Pensions)"}

fig, ax = plt.subplots(figsize=(10, 4.5), dpi=160)
fig.patch.set_facecolor("#F7F5F0")
ax.set_facecolor("#F7F5F0")
for dept, g in df.groupby("department"):
    ax.plot(g["month"], g["spend_bn"], linewidth=2.5,
            color=COLORS.get(dept, "#5C6B73"), label=LABELS.get(dept, dept))
ax.set_title("Monthly spend by department (gross transactional)",
             loc="left", fontsize=13, color="#14242E", fontweight="bold", pad=12)
ax.set_ylabel("£ billion", color="#5C6B73")
ax.legend(frameon=False, loc="upper right")
ax.set_ylim(bottom=0)
ax.grid(axis="y", color="#E5E0D6", linewidth=0.8)
ax.tick_params(colors="#5C6B73")
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
for s in ("left", "bottom"):
    ax.spines[s].set_color("#DED8CB")
Path("assets").mkdir(exist_ok=True)
fig.tight_layout()
fig.savefig("assets/spend_by_department.png",
            facecolor=fig.get_facecolor(), bbox_inches="tight")
print("wrote assets/spend_by_department.png")
