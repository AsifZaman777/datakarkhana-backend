import httpx
import re
import json
from bs4 import BeautifulSoup

client = httpx.Client(verify=False, headers={"User-Agent": "Mozilla/5.0"})

# 1. Fetch homepage
r = client.get("https://www.dse.com.bd")
print("Homepage status:", r.status_code)

# Check all links containing sector
soup = BeautifulSoup(r.text, "html.parser")
for a in soup.find_all("a"):
    t = a.get_text(strip=True)
    h = a.get("href", "")
    if "sector" in t.lower() or "sector" in h.lower() or "map" in t.lower():
        print(f"Link: {t} -> {h}")

# Search for api calls in scripts
api_matches = set(re.findall(r'["\'](/api/[^"\']+)["\']', r.text))
print("APIs in HTML:", api_matches)

# Check all script files
scripts = [s.get("src") for s in soup.find_all("script") if s.get("src")]
print("Found scripts:", len(scripts))
for sc in scripts:
    if not sc.startswith("http"):
        sc_url = f"https://www.dse.com.bd{sc}" if sc.startswith("/") else f"https://www.dse.com.bd/{sc}"
    else:
        sc_url = sc
    if any(k in sc.lower() for k in ["app", "main", "market", "sector", "chart", "index"]):
        try:
            res = client.get(sc_url, timeout=10)
            apis = set(re.findall(r'["\'](/api/[^"\']+)["\']', res.text))
            if apis:
                print(f"APIs in {sc}:", apis)
            if "sector" in res.text.lower():
                print(f"Script {sc} mentions 'sector'!")
                # find endpoints with sector
                sec_apis = set(re.findall(r'["\']([^"\']*sector[^"\']*)["\']', res.text, re.I))
                print("Sector strings/endpoints in script:", list(sec_apis)[:10])
        except Exception as e:
            pass

# Check /api/live/market for sector info
rm = client.get("https://www.dse.com.bd/api/live/market")
if rm.status_code == 200:
    m_json = rm.json()
    print("Market API keys:", m_json.keys())

# Test common sector endpoints on DSE:
test_urls = [
    "https://www.dse.com.bd/api/live/sectors",
    "https://www.dse.com.bd/api/live/sector",
    "https://www.dse.com.bd/api/sectors",
    "https://www.dse.com.bd/api/sector-summary",
    "https://www.dse.com.bd/api/market-summary",
    "https://www.dse.com.bd/api/live/sector-pe",
    "https://www.dse.com.bd/api/live/indices",
    "https://www.dse.com.bd/api/live/prices?view=sector",
    "https://www.dse.com.bd/markets/latest-share-price?view=sector",
]

for u in test_urls:
    try:
        ru = client.get(u, timeout=5)
        print(f"{u} -> {ru.status_code} (len: {len(ru.text)})")
        if ru.status_code == 200 and "application/json" in ru.headers.get("content-type", ""):
            print("  JSON keys:", ru.json().keys() if isinstance(ru.json(), dict) else len(ru.json()))
    except Exception as e:
        print(f"{u} -> Error: {e}")
