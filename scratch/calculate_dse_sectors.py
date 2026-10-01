import httpx
from bs4 import BeautifulSoup
import json
import re

client = httpx.Client(verify=False, headers={"User-Agent": "Mozilla/5.0"})
r = client.get("https://www.dse.com.bd")

# Look for self.__next_f or initial props
scripts = re.findall(r'<script[^>]*>(.*?)</script>', r.text, re.DOTALL)
print("Total inline scripts:", len(scripts))

for s in scripts:
    if "Textile" in s and "PharmaChem" in s:
        print("Found script with Textile & PharmaChem! Length:", len(s))
        # Look for sector data structure
        for match in re.finditer(r'\{[^{}]*"Textile"[^{}]*\}', s):
            print("Match:", match.group()[:300])
        # Look for JSON arrays or objects
        for m in re.finditer(r'\[\{[^\}]{5,200}"name"[^\}]{5,200}\}\]', s):
            print("Name match:", m.group()[:300])

# Check if there is an algorithm:
# Let's calculate the turnover and % change for each sector from /api/live/prices?view=sector!
r_prices = client.get("https://www.dse.com.bd/api/live/prices?view=sector")
pdata = r_prices.json()
cols = pdata["cols"]
rows = pdata["rows"]
col_map = {c: i for i, c in enumerate(cols)}

sector_stats = {}
for row in rows:
    sec = row[col_map["sector"]]
    val = float(row[col_map["value"]] or 0)
    pct = float(row[col_map["percent"]] or 0)
    ltp = float(row[col_map["ltp"]] or 0)
    ycp = float(row[col_map["ycp"]] or 0)
    if sec not in sector_stats:
        sector_stats[sec] = {"turnover": 0.0, "stocks": 0, "sum_pct": 0.0, "sum_ycp": 0.0, "sum_ltp": 0.0, "weighted_pct_num": 0.0}
    sector_stats[sec]["turnover"] += val
    sector_stats[sec]["stocks"] += 1
    sector_stats[sec]["sum_pct"] += pct
    sector_stats[sec]["sum_ycp"] += ycp
    sector_stats[sec]["sum_ltp"] += ltp
    if val > 0:
        sector_stats[sec]["weighted_pct_num"] += pct * val

print("\n--- Calculated from /api/live/prices (top sectors by turnover) ---")
# Sort by turnover
sorted_sectors = sorted(sector_stats.items(), key=lambda x: x[1]["turnover"], reverse=True)
for sec, stats in sorted_sectors[:15]:
    simple_avg_pct = stats["sum_pct"] / stats["stocks"] if stats["stocks"] > 0 else 0
    weighted_pct = stats["weighted_pct_num"] / stats["turnover"] if stats["turnover"] > 0 else 0
    ycp_ltp_pct = ((stats["sum_ltp"] - stats["sum_ycp"]) / stats["sum_ycp"]) * 100 if stats["sum_ycp"] > 0 else 0
    print(f"{sec:15s} | Turnover: {stats['turnover']:8.2f} mn | Stocks: {stats['stocks']:2d} | SimpleAvg: {simple_avg_pct:+.2f}% | Weighted: {weighted_pct:+.2f}% | YcpLtp: {ycp_ltp_pct:+.2f}%")
