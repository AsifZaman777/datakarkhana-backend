import httpx
from bs4 import BeautifulSoup
import json
import re

client = httpx.Client(verify=False, headers={"User-Agent": "Mozilla/5.0"})
r = client.get("https://www.dse.com.bd")
soup = BeautifulSoup(r.text, "html.parser")

out = []
for tag in soup.find_all(string=re.compile("Sector heatmap", re.I)):
    parent = tag.parent
    for _ in range(5):
        if parent and parent.parent:
            parent = parent.parent
    if parent:
        out.append("--- SECTOR HEATMAP HTML ---")
        out.append(parent.prettify())
        break

# Find all next-data or state scripts
for script in soup.find_all("script"):
    if script.get("id") == "__NEXT_DATA__":
        out.append("--- __NEXT_DATA__ ---")
        out.append(script.string[:5000])
    elif script.string and ("heatmap" in script.string.lower() or "sector" in script.string.lower()):
        out.append("--- SCRIPT ---")
        out.append(script.string[:2000])

with open("scratch/dse_heatmap_html.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))

print("Wrote to scratch/dse_heatmap_html.txt. Size:", len("\n".join(out)))
