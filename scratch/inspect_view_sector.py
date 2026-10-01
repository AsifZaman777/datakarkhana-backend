import httpx
import json
from bs4 import BeautifulSoup

client = httpx.Client(verify=False, headers={"User-Agent": "Mozilla/5.0"})

# 1. Inspect /api/live/prices?view=sector
r = client.get("https://www.dse.com.bd/api/live/prices?view=sector")
data = r.json()
print("cols:", data.get("cols"))
sectors_in_api = set()
col_map = {c: i for i, c in enumerate(data.get("cols", []))}
for row in data.get("rows", []):
    sec = row[col_map.get("sector", 13)]
    sectors_in_api.add(sec)
print("Sectors in /api/live/prices?view=sector:", len(sectors_in_api), sorted(list(sectors_in_api)))

# 2. Inspect /markets/latest-share-price?view=sector HTML to see what sectors DSE defines
r_html = client.get("https://www.dse.com.bd/markets/latest-share-price?view=sector")
soup = BeautifulSoup(r_html.text, "html.parser")
# Check sector headers or select options
options = [opt.get_text(strip=True) for opt in soup.find_all("option")]
print("Options in page:", options[:30])

headings = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3", "h4", "h5"])]
print("Headings:", [h for h in headings if len(h) < 50][:30])

# 3. Check DSE homepage HTML for heatmap or treemap or sector summary
r_home = client.get("https://www.dse.com.bd")
soup_home = BeautifulSoup(r_home.text, "html.parser")
# look for treemap, heatmap, sector-related elements
cards = soup_home.find_all(class_=lambda x: x and ("sector" in x.lower() or "heat" in x.lower() or "map" in x.lower()))
print("Cards in home matching sector/heat/map:", len(cards))
for c in cards[:10]:
    print(c.name, c.get("class"), c.get_text(strip=True)[:100])
