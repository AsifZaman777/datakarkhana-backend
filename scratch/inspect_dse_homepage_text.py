import httpx
from bs4 import BeautifulSoup
import json

client = httpx.Client(verify=False, headers={"User-Agent": "Mozilla/5.0"})
r = client.get("https://www.dse.com.bd")
soup = BeautifulSoup(r.text, "html.parser")

# Print all visible text blocks or headers on homepage
for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "span"]):
    text = tag.get_text(strip=True)
    if any(k in text.lower() for k in ["sector", "turnover", "heatmap", "map", "contribution"]):
        if len(text) < 150:
            print(f"[{tag.name}] {text}")

# Check any iframe on the homepage
for ifr in soup.find_all("iframe"):
    print("Iframe src:", ifr.get("src"))
