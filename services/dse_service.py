import asyncio
import logging
import time
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, AsyncGenerator, Set
import httpx
from bs4 import BeautifulSoup
from database import get_db
try:
    from fastapi import WebSocket
except ImportError:
    WebSocket = None  # type: ignore

logger = logging.getLogger("dse_service")
logger.setLevel(logging.INFO)

# BST is UTC+6
BST = timezone(timedelta(hours=6))

DSE_BASE_URL = "https://www.dse.com.bd"
PRICES_ENDPOINT = f"{DSE_BASE_URL}/api/live/prices"
MARKET_ENDPOINT = f"{DSE_BASE_URL}/api/live/market"
NEWS_ENDPOINT = f"{DSE_BASE_URL}/api/live/news"
QUOTES_ENDPOINT = f"{DSE_BASE_URL}/api/live/companies/quotes"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.dse.com.bd/markets/latest-share-price",
    "Accept-Language": "en-US,en;q=0.9"
}

SME_METADATA: Dict[str, Dict[str, str]] = {
    "ACHIASF": {"name": "Achia Sea Foods Limited", "sector": "Food & Allied"},
    "AMPL": {"name": "Agro Organica PLC", "sector": "Food & Allied"},
    "AOPLC": {"name": "Al-Madina Pharmaceuticals Limited", "sector": "Pharmaceuticals & Chemicals"},
    "APEXWEAV": {"name": "Apex Weaving & Finishing Mills Ltd", "sector": "Textile"},
    "BDPAINTS": {"name": "BD Paints Limited", "sector": "Miscellaneous"},
    "BENGALBISC": {"name": "Bengal Biscuits Limited", "sector": "Food & Allied"},
    "CRAFTSMAN": {"name": "Craftsman Footwear and Accessories Limited", "sector": "Tannery Industries"},
    "HIMADRI": {"name": "Himadri Limited", "sector": "Food & Allied"},
    "KBSEED": {"name": "Krishibid Seed Limited", "sector": "Food & Allied"},
    "KFL": {"name": "Krishibid Feed Limited", "sector": "Food & Allied"},
    "MAMUNAGRO": {"name": "Mamun Agro Products Limited", "sector": "Food & Allied"},
    "MASTERAGRO": {"name": "Master Agro Products Limited", "sector": "Food & Allied"},
    "MKFOOTWEAR": {"name": "MK Footwear PLC", "sector": "Tannery Industries"},
    "MOSTFAMETL": {"name": "Mostafa Metal Industries Limited", "sector": "Engineering"},
    "NIALCO": {"name": "Nialco Alloys Limited", "sector": "Engineering"},
    "ORYZAAGRO": {"name": "Oryza Agro Industries Limited", "sector": "Food & Allied"},
    "SADHESIVE": {"name": "Star Adhesives Limited", "sector": "Engineering"},
    "WEBCOATS": {"name": "Web Coats PLC", "sector": "Paper & Printing"},
    "WONDERTOYS": {"name": "Wonder Toys Limited", "sector": "Engineering"},
    "YUSUFLOUR": {"name": "Yusuf Flour Mills Limited", "sector": "Food & Allied"},
}

ATB_METADATA: Dict[str, Dict[str, str]] = {
    "LBS": {"name": "LankaBangla Securities PLC", "sector": "Financial Institutions", "asset_type": "EQUITY", "board": "ATB"},
    "RENATAPS": {"name": "Renata Limited Preference Shares", "sector": "Pharmaceuticals & Chemicals", "asset_type": "EQUITY", "board": "ATB"},
    "BBL2NDSB": {"name": "BRAC Bank 2nd Subordinated Bond", "sector": "Corporate Bond", "asset_type": "CB", "board": "DEBT"},
    "IBBT2BOND3": {"name": "Islami Bank Mudaraba Tier-2 Bond 3", "sector": "Corporate Bond", "asset_type": "CB", "board": "DEBT"},
    "IFICSBOND2": {"name": "IFIC Bank Subordinated Bond 2", "sector": "Corporate Bond", "asset_type": "CB", "board": "DEBT"},
    "IFICSBOND3": {"name": "IFIC Bank Subordinated Bond 3", "sector": "Corporate Bond", "asset_type": "CB", "board": "DEBT"},
    "PALUGB1": {"name": "Pran Agro Limited Guaranteed Bond 1", "sector": "Corporate Bond", "asset_type": "CB", "board": "DEBT"},
    "RAPLCSBOND": {"name": "Robi Axiata PLC Sukuk / Corporate Bond", "sector": "Corporate Bond", "asset_type": "CB", "board": "DEBT"},
    "TR1GSTGZCB": {"name": "Trust Bank 1st Green Sukuk Al-Istisna", "sector": "Corporate Bond", "asset_type": "CB", "board": "DEBT"},
    "TR2GSTGZCB": {"name": "Trust Bank 2nd Subordinated Bond", "sector": "Corporate Bond", "asset_type": "CB", "board": "DEBT"},
}

DSE_SECTOR_NAMES: Dict[str, str] = {
    "Bank": "Bank",
    "Cement": "Cement",
    "Ceramic": "Ceramics",
    "CorpBond": "Corporate Bond",
    "Debenture": "Debenture",
    "Engineering": "Engineering",
    "Financial In": "Financial Inst.",
    "FoodAllied": "Food & Allied",
    "FuelPower": "Fuel & Power",
    "IT": "IT Sector",
    "Insurance": "Insurance",
    "Jute": "Jute",
    "Misc": "Miscellaneous",
    "MutFund": "Mutual Funds",
    "PaperPrint": "Paper & Printing",
    "PharmaChem": "Pharma & Chem",
    "ServRealEst": "Services & Real Estate",
    "TBond": "Treasury Bond",
    "Tannery": "Tannery",
    "Telecom": "Telecommunication",
    "Textile": "Textile",
    "TravelLeisur": "Travel & Leisure",
}

def clean_float(val: Any, default: float = 0.0) -> float:
    if not val or val in ('--', '-', 'N/A', 'None'):
        return default
    try:
        return float(str(val).replace(',', '').strip())
    except Exception:
        return default

def clean_int(val: Any, default: int = 0) -> int:
    if not val or val in ('--', '-', 'N/A', 'None'):
        return default
    try:
        return int(float(str(val).replace(',', '').strip()))
    except Exception:
        return default

class DSEMarketService:
    _instance: Optional["DSEMarketService"] = None

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None
        self._bg_task: Optional[asyncio.Task] = None
        self._is_running: bool = False
        
        # In-Memory Cache
        self._last_prices: Dict[str, Dict[str, Any]] = {}
        self._last_market_summary: Dict[str, Any] = {}
        self._recent_news: List[Dict[str, Any]] = []
        self._intraday_history: Dict[str, List[Dict[str, Any]]] = {} # ticker -> list of minute ticks
        self._subscribers: List[asyncio.Queue] = []   # SSE subscribers
        self._ws_clients: Set[Any] = set()             # WebSocket clients
        self._last_scrape_time: float = 0.0
        self._is_market_open: bool = False
        
        # Deep analysis caches
        self._cb_cache: Optional[List[Dict[str, Any]]] = None
        self._cb_cache_ts: float = 0.0
        self._pe_cache: Optional[List[Dict[str, Any]]] = None
        self._pe_cache_ts: float = 0.0
        self._aag_cache: Optional[List[Dict[str, Any]]] = None
        self._aag_cache_ts: float = 0.0
        self._gsec_cache: Optional[List[Dict[str, Any]]] = None
        self._gsec_cache_ts: float = 0.0
        self._sector_heatmap_cache: Optional[Dict[str, Any]] = None
        self._sector_heatmap_cache_ts: float = 0.0

    @classmethod
    def get_instance(cls) -> "DSEMarketService":
        if cls._instance is None:
            cls._instance = DSEMarketService()
        return cls._instance

    async def get_client(self) -> httpx.AsyncClient:
        current_loop = asyncio.get_running_loop()
        client_loop = getattr(self, "_client_loop", None)
        if self._http_client is None or self._http_client.is_closed or client_loop != current_loop:
            self._client_loop = current_loop
            self._http_client = httpx.AsyncClient(
                headers=REQUEST_HEADERS,
                timeout=httpx.Timeout(12.0, connect=6.0),
                follow_redirects=True,
                verify=False
            )
        return self._http_client

    async def _fetch_url_fallback(self, urls: List[str]) -> Optional[str]:
        """Fetch content trying primary URL then fallback URLs"""
        client = await self.get_client()
        for u in urls:
            try:
                res = await client.get(u)
                if res.status_code == 200 and len(res.text) > 100:
                    return res.text
            except Exception as e:
                logger.debug(f"Failed to fetch {u}: {e}")
        return None

    def is_trading_hour(self) -> bool:
        """Trading hours: Sunday (6) to Thursday (3), 10:00 AM to 2:30 PM BST (UTC+6)"""
        now_bst = datetime.now(BST)
        weekday = now_bst.weekday()
        # In Python: Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
        # BD trading days: Sunday (6), Monday (0), Tuesday (1), Wednesday (2), Thursday (3)
        if weekday in (4, 5): # Friday, Saturday
            return False
        
        current_time = now_bst.time()
        start_time = datetime.strptime("09:55:00", "%H:%M:%S").time()
        end_time = datetime.strptime("14:35:00", "%H:%M:%S").time()
        return start_time <= current_time <= end_time

    async def fetch_market_summary(self) -> Dict[str, Any]:
        """Fetch market indices (DSEX, DS30, DSES), turnover, market breadth.
        Broadcasts a market_update SSE event whenever index values change."""
        try:
            client = await self.get_client()
            res = await client.get(MARKET_ENDPOINT)
            if res.status_code == 200:
                data = res.json()

                if "totals" in data and isinstance(data["totals"], dict):
                    if "turnover" in data["totals"] and "value" not in data["totals"]:
                        data["totals"]["value"] = data["totals"]["turnover"]

                # Detect meaningful change in index values before broadcasting
                prev = self._last_market_summary
                prev_dsex = prev.get("dsex", {}).get("index") if isinstance(prev.get("dsex"), dict) else prev.get("dsex")
                new_dsex  = data.get("dsex", {}).get("index") if isinstance(data.get("dsex"), dict) else data.get("dsex")

                self._last_market_summary = data

                if prev_dsex != new_dsex and new_dsex is not None:
                    asyncio.create_task(self._broadcast({
                        "event": "market_update",
                        "timestamp": datetime.now(BST).isoformat(),
                        "market_summary": data
                    }))

                return data
            else:
                logger.warning(f"Market endpoint returned status: {res.status_code}")
        except Exception as e:
            logger.error(f"Error fetching market summary: {e}")
        return self._last_market_summary

    async def fetch_live_prices(self) -> Dict[str, Any]:
        """Fetch live prices, calculate diffs, update in-memory cache and broadcast to subscribers"""
        try:
            client = await self.get_client()
            res = await client.get(PRICES_ENDPOINT)
            if res.status_code != 200:
                logger.warning(f"Prices endpoint returned status {res.status_code}")
                return {"updated_count": 0, "diffs": []}

            data = res.json()
            cols = data.get("cols", [])
            rows = data.get("rows", [])
            session_info = data.get("session", {})

            col_map = {col: idx for idx, col in enumerate(cols)}
            now_iso = datetime.now(BST).isoformat()
            now_ts = int(time.time())

            diffs = []
            parsed_prices: Dict[str, Dict[str, Any]] = {}

            for row in rows:
                if not row or len(row) < len(cols):
                    continue
                code = str(row[col_map["code"]]).strip()
                if not code:
                    continue

                ltp = float(row[col_map.get("ltp", 1)] or 0.0)
                ycp = float(row[col_map.get("ycp", 2)] or 0.0)
                open_p = float(row[col_map.get("open", 3)] or 0.0)
                high_p = float(row[col_map.get("high", 4)] or 0.0)
                low_p = float(row[col_map.get("low", 5)] or 0.0)
                close_p = float(row[col_map.get("close", 6)] or 0.0)
                volume = int(row[col_map.get("volume", 7)] or 0)
                value_mn = float(row[col_map.get("value", 8)] or 0.0)
                trades = int(row[col_map.get("trades", 9)] or 0)
                percent = float(row[col_map.get("percent", 10)] or 0.0)
                category = str(row[col_map.get("category", 11)] or "").strip()
                board = str(row[col_map.get("board", 12)] or "").strip()
                sector = str(row[col_map.get("sector", 13)] or "").strip()
                asset_type = str(row[col_map.get("assetType", 14)] or "").strip()
                change_val = round(ltp - ycp, 2) if ycp > 0 else 0.0

                item = {
                    "ticker": code,
                    "ltp": ltp,
                    "ycp": ycp,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": volume,
                    "value_mn": value_mn,
                    "trades": trades,
                    "percent": percent,
                    "change": change_val,
                    "category": category,
                    "board": board,
                    "sector": sector,
                    "asset_type": asset_type,
                    "updated_at": now_iso
                }
                parsed_prices[code] = item

                # Check for diff
                prev = self._last_prices.get(code)
                if prev is None or prev["ltp"] != ltp or prev["volume"] != volume or prev["trades"] != trades:
                    if prev is not None and prev.get("ltp") is not None and prev["ltp"] != ltp:
                        tick_dir = "up" if ltp > prev["ltp"] else "down"
                    else:
                        tick_dir = "up" if change_val > 0 else ("down" if change_val < 0 else "neutral")

                    diff_item = {
                        "ticker": code,
                        "ltp": ltp,
                        "change": change_val,
                        "percent": percent,
                        "volume": volume,
                        "value_mn": value_mn,
                        "high": high_p,
                        "low": low_p,
                        "trades": trades,
                        "direction": tick_dir
                    }
                    diffs.append(diff_item)

                # Store intraday point (at most one per 60 seconds per ticker)
                if code not in self._intraday_history:
                    self._intraday_history[code] = []
                history = self._intraday_history[code]
                if not history or (now_ts - history[-1]["time"]) >= 60:
                    history.append({
                        "time": now_ts,
                        "value": ltp,
                        "volume": volume
                    })
                    # Keep maximum 300 points (5 hours of 1-min ticks)
                    if len(history) > 300:
                        self._intraday_history[code] = history[-300:]

            # Incorporate live SME, ATB, and G-Sec board prices
            try:
                extra_stocks = await self.fetch_all_board_extra_stocks()
                for s in extra_stocks:
                    code = s["ticker"]
                    parsed_prices[code] = s

                    # Check for diff
                    prev = self._last_prices.get(code)
                    if prev is None or prev.get("ltp") != s["ltp"] or prev.get("volume") != s["volume"] or prev.get("trades") != s["trades"]:
                        if prev is not None and prev.get("ltp") is not None and prev.get("ltp") != s["ltp"]:
                            tick_dir = "up" if s["ltp"] > prev["ltp"] else "down"
                        else:
                            tick_dir = "up" if s["change"] > 0 else ("down" if s["change"] < 0 else "neutral")

                        diff_item = {
                            "ticker": code,
                            "ltp": s["ltp"],
                            "change": s["change"],
                            "percent": s["percent"],
                            "volume": s["volume"],
                            "value_mn": s["value_mn"],
                            "high": s["high"],
                            "low": s["low"],
                            "trades": s["trades"],
                            "direction": tick_dir
                        }
                        diffs.append(diff_item)

                    # Store intraday point (at most one per 60 seconds per ticker)
                    if code not in self._intraday_history:
                        self._intraday_history[code] = []
                    history = self._intraday_history[code]
                    if not history or (now_ts - history[-1]["time"]) >= 60:
                        history.append({
                            "time": now_ts,
                            "value": s["ltp"],
                            "volume": s["volume"]
                        })
                        if len(history) > 300:
                            self._intraday_history[code] = history[-300:]
            except Exception as e:
                logger.debug(f"All boards incorporation note: {e}")

            self._last_prices = parsed_prices
            self._last_scrape_time = time.time()

            # Broadcast diffs to active SSE subscribers
            if diffs:
                await self._broadcast({
                    "event": "tick_diff",
                    "timestamp": now_iso,
                    "count": len(diffs),
                    "ticks": diffs
                })

                # Trigger alert evaluation in background
                asyncio.create_task(self._evaluate_alerts_safe(diffs))

            return {
                "updated_count": len(diffs),
                "total_tickers": len(parsed_prices),
                "diffs": diffs,
                "session": session_info
            }
        except Exception as e:
            logger.error(f"Error fetching live prices: {e}")
            return {"updated_count": 0, "diffs": [], "error": str(e)}

    async def fetch_sme_prices(self) -> List[Dict[str, Any]]:
        """Fetch all 20 SME board equities with full OHLC, volume, value, trades"""
        sme_stocks = []
        now_iso = datetime.now(BST).isoformat()
        urls = [
            "https://sme.dsebd.org/sme_latest_share_price_scroll_l.php",
            "https://sme.dse.com.bd/sme_latest_share_price_scroll_l.php"
        ]
        html = await self._fetch_url_fallback(urls)
        if not html:
            return sme_stocks

        try:
            soup = BeautifulSoup(html, "html.parser")
            for table in soup.find_all("table"):
                rows = table.find_all("tr")
                if len(rows) > 5:
                    headers_row = [c.get_text(strip=True).upper() for c in rows[0].find_all(["th", "td"])]
                    if "TRADING CODE" in headers_row:
                        for tr in rows[1:]:
                            cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
                            if len(cells) > 7:
                                code = cells[1].strip()
                                if not code or code == '#':
                                    continue
                                ltp = clean_float(cells[2])
                                high_p = clean_float(cells[3])
                                low_p = clean_float(cells[4])
                                close_p = clean_float(cells[5])
                                ycp = clean_float(cells[6])
                                chg = clean_float(cells[7])
                                trades = clean_int(cells[8]) if len(cells) > 8 else 0
                                val_mn = clean_float(cells[9]) if len(cells) > 9 else 0.0
                                vol = clean_int(cells[10]) if len(cells) > 10 else 0
                                pct = round((chg / ycp) * 100, 2) if ycp > 0 else 0.0
                                eff_price = ltp if ltp > 0 else (close_p if close_p > 0 else ycp)
                                meta = SME_METADATA.get(code, {})
                                sme_stocks.append({
                                    "ticker": code,
                                    "name": meta.get("name", code),
                                    "ltp": eff_price,
                                    "ycp": ycp,
                                    "open": eff_price,
                                    "high": high_p if high_p > 0 else eff_price,
                                    "low": low_p if low_p > 0 else eff_price,
                                    "close": close_p if close_p > 0 else eff_price,
                                    "volume": vol,
                                    "value_mn": val_mn,
                                    "trades": trades,
                                    "percent": pct,
                                    "change": chg,
                                    "category": "SME",
                                    "board": "SME",
                                    "sector": meta.get("sector", "SME Board"),
                                    "asset_type": "EQUITY",
                                    "updated_at": now_iso
                                })
                        if sme_stocks:
                            break
        except Exception as e:
            logger.debug(f"Error parsing SME prices: {e}")
        return sme_stocks

    async def fetch_atb_prices(self) -> List[Dict[str, Any]]:
        """Fetch ATB Equities and ATB Debt/Bond securities"""
        atb_stocks = []
        now_iso = datetime.now(BST).isoformat()

        # 1. ATB Equities
        eq_html = await self._fetch_url_fallback([
            "https://atb.dsebd.org/latest_share_price_scroll_l.php",
            "https://atb.dse.com.bd/latest_share_price_scroll_l.php"
        ])
        if eq_html:
            try:
                soup = BeautifulSoup(eq_html, "html.parser")
                for table in soup.find_all("table"):
                    rows = table.find_all("tr")
                    if len(rows) >= 2:
                        headers_row = [c.get_text(strip=True).upper() for c in rows[0].find_all(["th", "td"])]
                        if "TRADING CODE" in headers_row:
                            for tr in rows[1:]:
                                cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
                                if len(cells) > 7:
                                    code = cells[1].strip()
                                    if not code or code == '#':
                                        continue
                                    ltp = clean_float(cells[2])
                                    high_p = clean_float(cells[3])
                                    low_p = clean_float(cells[4])
                                    close_p = clean_float(cells[5])
                                    ycp = clean_float(cells[6])
                                    chg = clean_float(cells[7])
                                    trades = clean_int(cells[8]) if len(cells) > 8 else 0
                                    val_mn = clean_float(cells[9]) if len(cells) > 9 else 0.0
                                    vol = clean_int(cells[10]) if len(cells) > 10 else 0
                                    pct = round((chg / ycp) * 100, 2) if ycp > 0 else 0.0
                                    eff_price = ltp if ltp > 0 else (close_p if close_p > 0 else ycp)
                                    meta = ATB_METADATA.get(code, {})
                                    atb_stocks.append({
                                        "ticker": code,
                                        "name": meta.get("name", code),
                                        "ltp": eff_price,
                                        "ycp": ycp,
                                        "open": eff_price,
                                        "high": high_p if high_p > 0 else eff_price,
                                        "low": low_p if low_p > 0 else eff_price,
                                        "close": close_p if close_p > 0 else eff_price,
                                        "volume": vol,
                                        "value_mn": val_mn,
                                        "trades": trades,
                                        "percent": pct,
                                        "change": chg,
                                        "category": "ATB",
                                        "board": "ATB",
                                        "sector": meta.get("sector", "ATB Board"),
                                        "asset_type": "EQUITY",
                                        "updated_at": now_iso
                                    })
                            if atb_stocks:
                                break
            except Exception as e:
                logger.debug(f"Error parsing ATB equities: {e}")

        # 2. ATB Debt & Bonds
        bond_html = await self._fetch_url_fallback([
            "https://atb.dsebd.org/atb_latest_share_price_scroll_treasury_bond.php",
            "https://atb.dse.com.bd/atb_latest_share_price_scroll_treasury_bond.php"
        ])
        if bond_html:
            try:
                soup = BeautifulSoup(bond_html, "html.parser")
                for table in soup.find_all("table"):
                    rows = table.find_all("tr")
                    if len(rows) >= 2:
                        headers_row = [c.get_text(strip=True).upper() for c in rows[0].find_all(["th", "td"])]
                        if "TRADING CODE" in headers_row:
                            for tr in rows[1:]:
                                cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
                                if len(cells) > 7:
                                    code = cells[1].strip()
                                    if not code or code == '#':
                                        continue
                                    ltp = clean_float(cells[2])
                                    high_p = clean_float(cells[3])
                                    low_p = clean_float(cells[4])
                                    close_p = clean_float(cells[5])
                                    ycp = clean_float(cells[6])
                                    chg = clean_float(cells[7])
                                    trades = clean_int(cells[8]) if len(cells) > 8 else 0
                                    val_mn = clean_float(cells[9]) if len(cells) > 9 else 0.0
                                    vol = clean_int(cells[10]) if len(cells) > 10 else 0
                                    pct = round((chg / ycp) * 100, 2) if ycp > 0 else 0.0
                                    eff_price = ltp if ltp > 0 else (close_p if close_p > 0 else ycp)
                                    meta = ATB_METADATA.get(code, {})
                                    atb_stocks.append({
                                        "ticker": code,
                                        "name": meta.get("name", code),
                                        "ltp": eff_price,
                                        "ycp": ycp,
                                        "open": eff_price,
                                        "high": high_p if high_p > 0 else eff_price,
                                        "low": low_p if low_p > 0 else eff_price,
                                        "close": close_p if close_p > 0 else eff_price,
                                        "volume": vol,
                                        "value_mn": val_mn,
                                        "trades": trades,
                                        "percent": pct,
                                        "change": chg,
                                        "category": "DEBT",
                                        "board": "DEBT",
                                        "sector": meta.get("sector", "Corporate Bond"),
                                        "asset_type": "CB",
                                        "updated_at": now_iso
                                    })
                            break
            except Exception as e:
                logger.debug(f"Error parsing ATB bonds: {e}")

        return atb_stocks

    async def fetch_gsec_prices(self) -> List[Dict[str, Any]]:
        """Fetch Bangladesh Government Treasury Bonds (G-Sec) with 90s cache"""
        now_ts = time.time()
        if self._gsec_cache and (now_ts - self._gsec_cache_ts) < 90:
            return self._gsec_cache

        gsec_bonds = []
        now_iso = datetime.now(BST).isoformat()
        try:
            client = await self.get_client()
            res = await client.get(f"{DSE_BASE_URL}/bonds/government-securities/trades")
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                table = soup.find("table")
                if table:
                    rows = table.find_all("tr")
                    if len(rows) > 5:
                        for tr in rows[1:]:
                            cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
                            if len(cells) > 7:
                                code = cells[1].strip()
                                if not code or code == '#':
                                    continue
                                ltp = clean_float(cells[2])
                                high_p = clean_float(cells[3])
                                low_p = clean_float(cells[4])
                                close_p = clean_float(cells[5])
                                ycp = clean_float(cells[6])
                                chg = clean_float(cells[7])
                                trades = clean_int(cells[8]) if len(cells) > 8 else 0
                                val_mn = clean_float(cells[9]) if len(cells) > 9 else 0.0
                                vol = clean_int(cells[10]) if len(cells) > 10 else 0
                                pct = round((chg / ycp) * 100, 2) if ycp > 0 else 0.0
                                eff_price = ltp if ltp > 0 else (close_p if close_p > 0 else ycp)
                                gsec_bonds.append({
                                    "ticker": code,
                                    "name": f"BD Govt Treasury Bond {code}",
                                    "ltp": eff_price,
                                    "ycp": ycp,
                                    "open": eff_price,
                                    "high": high_p if high_p > 0 else eff_price,
                                    "low": low_p if low_p > 0 else eff_price,
                                    "close": close_p if close_p > 0 else eff_price,
                                    "volume": vol,
                                    "value_mn": val_mn,
                                    "trades": trades,
                                    "percent": pct,
                                    "change": chg,
                                    "category": "G-SEC",
                                    "board": "YIELDDBT",
                                    "sector": "Govt Treasury Bond",
                                    "asset_type": "GOVDBT",
                                    "updated_at": now_iso
                                })
            if gsec_bonds:
                self._gsec_cache = gsec_bonds
                self._gsec_cache_ts = now_ts
        except Exception as e:
            logger.debug(f"Error scraping G-Sec bonds: {e}")

        return self._gsec_cache or []

    async def fetch_all_board_extra_stocks(self) -> List[Dict[str, Any]]:
        """Fetch all non-main board instruments: SME + ATB + G-Sec Treasury Bonds"""
        results = await asyncio.gather(
            self.fetch_sme_prices(),
            self.fetch_atb_prices(),
            self.fetch_gsec_prices(),
            return_exceptions=True
        )
        extra = []
        for r in results:
            if isinstance(r, list):
                extra.extend(r)
            elif isinstance(r, Exception):
                logger.debug(f"Board scraper task note: {r}")
        return extra

    async def fetch_live_news(self) -> List[Dict[str, Any]]:
        """Fetch latest corporate announcements from DSE"""
        try:
            client = await self.get_client()
            res = await client.get(NEWS_ENDPOINT)
            if res.status_code == 200:
                data = res.json()
                rows = data.get("rows", [])
                self._recent_news = rows[:50]

                # Store in database and trigger news alerts
                asyncio.create_task(self._persist_news(rows[:20]))
                return self._recent_news
        except Exception as e:
            logger.error(f"Error fetching live news: {e}")
        return self._recent_news

    async def _persist_news(self, news_items: List[Dict[str, Any]]):
        """Save corporate news to SQLite/Postgres and evaluate news alerts"""
        if not news_items:
            return
        try:
            from services.stock_alert_service import StockAlertService
            conn = get_db()
            cursor = conn.cursor()
            new_inserted_items = []

            for item in news_items:
                nid = str(item.get("id", "")).strip()
                code = str(item.get("code", "")).strip()
                name = str(item.get("name", "")).strip()
                ntype = str(item.get("type", "")).strip()
                date_str = str(item.get("date", "")).strip()
                time_str = str(item.get("time", "")).strip()
                summary = str(item.get("summary", "")).strip()
                body = str(item.get("body", "")).strip()

                if not nid or not summary:
                    continue

                try:
                    cursor.execute("""
                        INSERT INTO stock_news (news_id, ticker, company_name, news_type, published_date, published_time, title, body)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (news_id) DO NOTHING;
                    """, (nid, code, name, ntype, date_str, time_str, summary, body))
                    if cursor.rowcount > 0:
                        new_inserted_items.append(item)
                except Exception:
                    try:
                        cursor.execute("""
                            INSERT OR IGNORE INTO stock_news (news_id, ticker, company_name, news_type, published_date, published_time, title, body)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                        """, (nid, code, name, ntype, date_str, time_str, summary, body))
                        if cursor.rowcount > 0:
                            new_inserted_items.append(item)
                    except Exception:
                        pass

            conn.commit()
            conn.close()

            # Trigger news alerts for newly arrived announcements
            if new_inserted_items:
                alert_service = StockAlertService.get_instance()
                for n in new_inserted_items:
                    await alert_service.evaluate_news_alert(n)

        except Exception as e:
            logger.error(f"Error persisting news: {e}")

    async def _evaluate_alerts_safe(self, diffs: List[Dict[str, Any]]):
        """Invoke alert evaluation without throwing exceptions"""
        try:
            from services.stock_alert_service import StockAlertService
            alert_service = StockAlertService.get_instance()
            await alert_service.evaluate_price_alerts(diffs)
        except Exception as e:
            logger.error(f"Error evaluating price alerts: {e}")

    async def _broadcast(self, message: Dict[str, Any]):
        """Broadcast message to all connected SSE and WebSocket clients"""
        payload_str = json.dumps(message)

        # --- SSE subscribers ---
        dead_queues = []
        for q in self._subscribers:
            try:
                if q.qsize() > 50:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                await q.put(message)
            except Exception:
                dead_queues.append(q)
        for dq in dead_queues:
            if dq in self._subscribers:
                self._subscribers.remove(dq)

        # --- WebSocket clients ---
        dead_ws = set()
        for ws in list(self._ws_clients):
            try:
                await ws.send_text(payload_str)
            except Exception:
                dead_ws.add(ws)
        self._ws_clients -= dead_ws

    async def subscribe_stream(self, request: Optional[Any] = None) -> AsyncGenerator[str, None]:
        """Async generator yielding SSE formatted messages to frontend clients with heartbeat keepalive"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(queue)
        
        # Send initial snapshot immediately
        init_payload = {
            "event": "init",
            "market_summary": self._last_market_summary,
            "sector_heatmap": self._sector_heatmap_cache,
            "tickers_count": len(self._last_prices),
            "tickers": list(self._last_prices.values()),
            "recent_news": self._recent_news[:15],
            "timestamp": datetime.now(BST).isoformat()
        }
        import json
        yield f"event: init\ndata: {json.dumps(init_payload)}\n\n"

        try:
            while True:
                if request is not None and await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=2.0)
                    event_name = data.get("event", "message")
                    yield f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat comment to detect client disconnection promptly
                    yield ": ping\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        except Exception as e:
            logger.debug(f"SSE client stream exception/closed: {e}")
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    async def subscribe_websocket(self, websocket: Any):
        """Handle a WebSocket connection: send initial snapshot then stream tick_diff events."""
        await websocket.accept()
        self._ws_clients.add(websocket)
        logger.info(f"[WS] Client connected. Total WS clients: {len(self._ws_clients)}")

        # Send initial snapshot
        init_payload = {
            "event": "init",
            "market_summary": self._last_market_summary,
            "tickers_count": len(self._last_prices),
            "tickers": list(self._last_prices.values()),
            "recent_news": self._recent_news[:15],
            "timestamp": datetime.now(BST).isoformat()
        }
        try:
            await websocket.send_text(json.dumps(init_payload))
        except Exception as e:
            logger.debug(f"[WS] Failed to send init snapshot: {e}")
            self._ws_clients.discard(websocket)
            return

        # Keep alive — receive loop (handles client pings / disconnects)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                    # Respond to client ping
                    if msg == "ping":
                        await websocket.send_text(json.dumps({"event": "pong"}))
                except asyncio.TimeoutError:
                    # Send server-side keepalive ping
                    await websocket.send_text(json.dumps({"event": "ping"}))
        except Exception as e:
            logger.debug(f"[WS] Client disconnected: {e}")
        finally:
            self._ws_clients.discard(websocket)
            logger.info(f"[WS] Client removed. Total WS clients: {len(self._ws_clients)}")

    def get_ws_client_count(self) -> int:
        return len(self._ws_clients)

    def get_all_stocks(
        self,
        search: Optional[str] = None,
        sector: Optional[str] = None,
        category: Optional[str] = None,
        board: Optional[str] = None,
        sort_by: str = "turnover", # 'turnover', 'percent', 'ltp', 'code'
        sort_order: str = "desc"
    ) -> List[Dict[str, Any]]:
        """Filter and sort cached stock prices across all boards"""
        stocks = list(self._last_prices.values())

        if search:
            s_clean = search.upper().strip()
            stocks = [
                s for s in stocks
                if s_clean in s.get("ticker", "").upper()
                or s_clean in s.get("name", "").upper()
                or s_clean in s.get("sector", "").upper()
            ]

        if sector and sector.upper() != "ALL":
            sec_clean = sector.lower().strip()
            stocks = [s for s in stocks if sec_clean in s.get("sector", "").lower()]

        if category and category.upper() != "ALL":
            cat_clean = category.upper().strip()
            stocks = [s for s in stocks if s.get("category", "").upper() == cat_clean]

        if board and board.upper() != "ALL":
            b_clean = board.upper().strip()
            if b_clean in ("DEBT", "CORP_BOND", "CORPORATE DEBT"):
                stocks = [s for s in stocks if s.get("board", "").upper() == "DEBT" or s.get("asset_type", "").upper() == "CB"]
            elif b_clean in ("YIELDDBT", "G-SEC", "GSEC", "TREASURY"):
                stocks = [s for s in stocks if s.get("board", "").upper() == "YIELDDBT" or s.get("asset_type", "").upper() == "GOVDBT"]
            elif b_clean in ("BONDS", "ALL_BONDS", "DEBT_ALL"):
                stocks = [s for s in stocks if s.get("board", "").upper() in ("DEBT", "YIELDDBT") or s.get("asset_type", "").upper() in ("CB", "GOVDBT")]
            else:
                stocks = [s for s in stocks if s.get("board", "").upper() == b_clean]

        # Sorting
        reverse = (sort_order.lower() == "desc")
        if sort_by == "turnover":
            stocks.sort(key=lambda x: x.get("value_mn", 0.0), reverse=reverse)
        elif sort_by == "percent":
            stocks.sort(key=lambda x: x.get("percent", 0.0), reverse=reverse)
        elif sort_by == "ltp":
            stocks.sort(key=lambda x: x.get("ltp", 0.0), reverse=reverse)
        elif sort_by == "volume":
            stocks.sort(key=lambda x: x.get("volume", 0), reverse=reverse)
        elif sort_by == "code":
            stocks.sort(key=lambda x: x.get("ticker", ""), reverse=reverse)

        return stocks

    def get_stock_detail(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get live quote and intraday tick points for a specific ticker"""
        code = ticker.upper().strip()
        stock = self._last_prices.get(code)
        if not stock:
            return None
        return {
            **stock,
            "intraday_series": self._intraday_history.get(code, [])
        }

    def get_boards_summary(self) -> Dict[str, Any]:
        """Return counts and metadata for each active trading board in DSE"""
        stocks = list(self._last_prices.values())
        return {
            "boards": [
                {"id": "ALL", "name": "All Boards", "icon": "🌐", "count": len(stocks)},
                {"id": "PUBLIC", "name": "Public (Main Board)", "icon": "🏛️", "count": len([s for s in stocks if s.get("board") == "PUBLIC"])},
                {"id": "SME", "name": "SME Board", "icon": "🚀", "count": len([s for s in stocks if s.get("board") == "SME"])},
                {"id": "ATB", "name": "Alternative Trading Board (ATB)", "icon": "🔄", "count": len([s for s in stocks if s.get("board") == "ATB"])},
                {"id": "DEBT", "name": "Corporate Debt & Sukuk", "icon": "📜", "count": len([s for s in stocks if s.get("board") == "DEBT" or s.get("asset_type") == "CB"])},
                {"id": "YIELDDBT", "name": "Govt Treasury Bonds (G-Sec)", "icon": "🇧🇩", "count": len([s for s in stocks if s.get("board") == "YIELDDBT" or s.get("asset_type") == "GOVDBT"])},
            ],
            "total_instruments": len(stocks)
        }

    def get_market_summary(self) -> Dict[str, Any]:
        """Get latest market summary indices, breadth, and board statistics"""
        return {
            "summary": self._last_market_summary,
            "is_trading_hour": self.is_trading_hour(),
            "total_tracked": len(self._last_prices),
            "boards_summary": self.get_boards_summary()["boards"],
            "last_scraped_at": datetime.fromtimestamp(self._last_scrape_time, tz=BST).isoformat() if self._last_scrape_time else None
        }

    def get_recent_news(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._recent_news[:limit]

    def get_top_shares(self) -> Dict[str, Any]:
        """Compute Top 20 by Turnover, Gainers, Losers, and Volume"""
        stocks = list(self._last_prices.values())
        by_turnover = sorted(stocks, key=lambda x: x.get("value_mn", 0.0), reverse=True)[:20]
        by_gainers = sorted([s for s in stocks if s.get("percent", 0.0) > 0], key=lambda x: x.get("percent", 0.0), reverse=True)[:20]
        by_losers = sorted([s for s in stocks if s.get("percent", 0.0) < 0], key=lambda x: x.get("percent", 0.0))[:20]
        by_volume = sorted(stocks, key=lambda x: x.get("volume", 0), reverse=True)[:20]
        return {
            "top_turnover": by_turnover,
            "top_gainers": by_gainers,
            "top_losers": by_losers,
            "top_volume": by_volume
        }

    def _parse_scraped_sector_heatmap(self, html: str) -> Dict[str, Any]:
        """Parse official Sector heatmap section directly from DSE homepage (www.dse.com.bd)"""
        try:
            soup = BeautifulSoup(html, "html.parser")
            heading = soup.find(string=re.compile(r"Sector heatmap", re.I))
            if not heading:
                return {}

            parent_box = heading.find_parent(class_=lambda x: x and "flex-col" in x)
            if not parent_box:
                parent_box = heading.find_parent("div")
            if not parent_box:
                return {}

            adv_count = 0
            dec_count = 0
            stats = parent_box.find("span", class_=re.compile(r"tnum"))
            if stats:
                txt = stats.get_text()
                adv_match = re.search(r"(\d+)\s*adv", txt)
                dec_match = re.search(r"(\d+)\s*dec", txt)
                if adv_match:
                    adv_count = int(adv_match.group(1))
                if dec_match:
                    dec_count = int(dec_match.group(1))

            buttons = parent_box.find_all("button")
            sectors = []

            for b in buttons:
                full_text = b.get_text(separator=" ", strip=True)
                pct_match = re.search(r"([+-]?\s*\d+(?:\.\d+)?)\s*%", full_text)
                change_pct = 0.0
                if pct_match:
                    val_str = pct_match.group(1).replace(" ", "")
                    try:
                        change_pct = float(val_str)
                    except ValueError:
                        pass

                name = ""
                divs = b.find_all("div")
                for d in divs:
                    t = d.get_text(strip=True)
                    if not any(c in t for c in ["%", "+", "-"]) and not name and t:
                        name = t
                        break
                if not name:
                    name = full_text.split("+")[0].split("-")[0].strip()

                classes = b.get("class", [])
                col_span = 1
                row_span = 1
                for c in classes:
                    if "col-span-" in c:
                        try:
                            col_span = int(c.split("col-span-")[-1])
                        except ValueError:
                            pass
                    if "row-span-" in c:
                        try:
                            row_span = int(c.split("row-span-")[-1])
                        except ValueError:
                            pass

                style = b.get("style", "")
                bg_match = re.search(r"background:\s*([^;]+)", style)
                bg_color = bg_match.group(1).strip() if bg_match else ""

                sectors.append({
                    "code": name,
                    "name": name,
                    "label": DSE_SECTOR_NAMES.get(name, name),
                    "raw_name": name,
                    "change_pct": change_pct,
                    "col_span": col_span,
                    "row_span": row_span,
                    "bg_color": bg_color,
                })

            # Calculate actual sector turnover from tracked prices if available
            sector_turnovers = {}
            for s in self._last_prices.values():
                sec = str(s.get("sector") or "").strip()
                if sec:
                    val = float(s.get("value_mn") or 0.0)
                    sector_turnovers[sec] = sector_turnovers.get(sec, 0.0) + val

            for i, sec in enumerate(sectors):
                to = sector_turnovers.get(sec["code"], sector_turnovers.get(sec["raw_name"], 0.0))
                sec["turnover"] = round(to, 2) if to > 0 else float(1000 - i * 50)

            return {
                "adv": adv_count,
                "dec": dec_count,
                "sectors": sectors,
                "top_12": sectors[:12],
                "last_scraped_at": datetime.now(BST).isoformat(),
                "source": "scraped_dse_homepage",
            }
        except Exception as e:
            logger.error(f"Error parsing scraped sector heatmap: {e}")
            return {}

    async def fetch_sector_heatmap(self) -> Dict[str, Any]:
        """Fetch and scrape official Sector Heatmap from DSE homepage"""
        try:
            client = await self.get_client()
            res = await client.get(DSE_BASE_URL, timeout=12.0)
            if res.status_code == 200:
                parsed = self._parse_scraped_sector_heatmap(res.text)
                if parsed and parsed.get("sectors"):
                    self._sector_heatmap_cache = parsed
                    self._sector_heatmap_cache_ts = time.time()
                    return parsed
        except Exception as e:
            logger.error(f"Error fetching DSE homepage for sector heatmap: {e}")

        if self._sector_heatmap_cache:
            return self._sector_heatmap_cache

        return {"adv": 0, "dec": 0, "sectors": [], "top_12": [], "source": "scraped_dse_homepage"}

    def get_sector_heatmap(self) -> Dict[str, Any]:
        """Get official Sector Heatmap scraped directly from DSE homepage (www.dse.com.bd).
        Does NOT synthesize or calculate values from individual stock items."""
        if self._sector_heatmap_cache:
            return self._sector_heatmap_cache

        # Fallback quick synchronous scrape if cache is completely empty
        try:
            with httpx.Client(headers=REQUEST_HEADERS, timeout=8.0, verify=False) as client:
                res = client.get(DSE_BASE_URL)
                if res.status_code == 200:
                    parsed = self._parse_scraped_sector_heatmap(res.text)
                    if parsed and parsed.get("sectors"):
                        self._sector_heatmap_cache = parsed
                        self._sector_heatmap_cache_ts = time.time()
                        return parsed
        except Exception as e:
            logger.error(f"Fallback synchronous sector heatmap scrape failed: {e}")

        return {"adv": 0, "dec": 0, "sectors": [], "top_12": [], "source": "scraped_dse_homepage"}

    async def get_market_depth(self, code: str) -> Dict[str, Any]:
        """Fetch 10-level live order book (bids, asks, queues, priceStats) for a given instrument"""
        clean_code = (code or "IPDC").upper().strip()
        try:
            client = await self.get_client()
            url = f"{DSE_BASE_URL}/api/live/depth?code={clean_code}"
            res = await client.get(url)
            if res.status_code == 200:
                return res.json()
            else:
                logger.warning(f"Market depth endpoint returned status {res.status_code} for {clean_code}")
        except Exception as e:
            logger.error(f"Error fetching market depth for {clean_code}: {e}")
        
        return {
            "code": clean_code,
            "bids": [],
            "asks": [],
            "asOf": datetime.now(BST).strftime("%I:%M:%S %p"),
            "priceStats": {}
        }

    async def get_depth_instruments(self) -> Dict[str, Any]:
        """Fetch list of instruments available for market depth"""
        try:
            client = await self.get_client()
            url = f"{DSE_BASE_URL}/api/live/depth/instruments"
            res = await client.get(url)
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            logger.error(f"Error fetching depth instruments: {e}")
        return {"rows": [], "popular": []}

    async def get_circuit_breakers(self) -> List[Dict[str, Any]]:
        """Fetch daily circuit breaker limits for Public, SME, and ATB boards"""
        now_ts = time.time()
        if self._cb_cache and (now_ts - self._cb_cache_ts) < 600:
            return self._cb_cache

        rows = []
        # 1. Main Board Circuit Breakers
        try:
            client = await self.get_client()
            res = await client.get(f"{DSE_BASE_URL}/circuit-breaker")
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                table = soup.find("table")
                if table:
                    trs = table.find_all("tr")
                    for tr in trs[1:]:
                        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                        if len(tds) >= 9:
                            try:
                                rows.append({
                                    "code": tds[1],
                                    "name": tds[2],
                                    "close_price": clean_float(tds[3]),
                                    "breaker_pct": tds[4],
                                    "tick_size": clean_float(tds[5], 0.1),
                                    "open_adj_price": clean_float(tds[6]),
                                    "lower_limit": clean_float(tds[7]),
                                    "upper_limit": clean_float(tds[8]),
                                    "board": "PUBLIC"
                                })
                            except Exception:
                                pass
        except Exception as e:
            logger.error(f"Error parsing main circuit breakers: {e}")

        # 2. SME Board Circuit Breakers
        try:
            sme_cb_html = await self._fetch_url_fallback([
                "https://sme.dsebd.org/circuit_breaker_sme.php",
                "https://sme.dse.com.bd/circuit_breaker_sme.php"
            ])
            if sme_cb_html:
                soup = BeautifulSoup(sme_cb_html, "html.parser")
                for table in soup.find_all("table"):
                    trs = table.find_all("tr")
                    if len(trs) > 5:
                        headers_row = [c.get_text(strip=True).upper() for c in trs[0].find_all(["th", "td"])]
                        if "TRADING CODE" in headers_row:
                            for tr in trs[1:]:
                                tds = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
                                if len(tds) >= 7:
                                    code = tds[1].strip()
                                    if not code or code == '#':
                                        continue
                                    rows.append({
                                        "code": code,
                                        "name": SME_METADATA.get(code, {}).get("name", code),
                                        "close_price": clean_float(tds[4]),
                                        "breaker_pct": tds[2],
                                        "tick_size": clean_float(tds[3], 0.1),
                                        "open_adj_price": clean_float(tds[4]),
                                        "lower_limit": clean_float(tds[5]),
                                        "upper_limit": clean_float(tds[6]),
                                        "board": "SME"
                                    })
                            break
        except Exception as e:
            logger.debug(f"Error parsing SME circuit breakers: {e}")

        # 3. ATB Board Circuit Breakers
        try:
            atb_cb_html = await self._fetch_url_fallback([
                "https://atb.dsebd.org/atb_CircuitBreaker.php",
                "https://atb.dse.com.bd/atb_CircuitBreaker.php"
            ])
            if atb_cb_html:
                soup = BeautifulSoup(atb_cb_html, "html.parser")
                for table in soup.find_all("table"):
                    trs = table.find_all("tr")
                    if len(trs) > 5:
                        headers_row = [c.get_text(strip=True).upper() for c in trs[0].find_all(["th", "td"])]
                        if "TRADE CODE" in headers_row:
                            for tr in trs[1:]:
                                tds = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
                                if len(tds) >= 7:
                                    code = tds[1].strip()
                                    if not code or code == '#':
                                        continue
                                    rows.append({
                                        "code": code,
                                        "name": ATB_METADATA.get(code, {}).get("name", code),
                                        "close_price": clean_float(tds[4]),
                                        "breaker_pct": tds[2],
                                        "tick_size": clean_float(tds[3], 0.5),
                                        "open_adj_price": clean_float(tds[4]),
                                        "lower_limit": clean_float(tds[5]),
                                        "upper_limit": clean_float(tds[6]),
                                        "board": "ATB"
                                    })
                            break
        except Exception as e:
            logger.debug(f"Error parsing ATB circuit breakers: {e}")

        if rows:
            self._cb_cache = rows
            self._cb_cache_ts = now_ts
        return self._cb_cache or []

    async def get_recent_market_info(self, from_date: Optional[str] = None, to_date: Optional[str] = None) -> Dict[str, Any]:
        """Fetch historical daily market totals and DSEX index"""
        try:
            if not to_date:
                to_date = datetime.now(BST).strftime("%Y-%m-%d")
            if not from_date:
                from_date = (datetime.now(BST) - timedelta(days=45)).strftime("%Y-%m-%d")
            client = await self.get_client()
            url = f"{DSE_BASE_URL}/api/live/recent-market-info?from={from_date}&to={to_date}"
            res = await client.get(url)
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            logger.error(f"Error fetching recent market info: {e}")
        return {"rows": []}

    async def get_pe_at_a_glance(self) -> List[Dict[str, Any]]:
        """Fetch Price-to-Earnings ratios for all active stocks"""
        now_ts = time.time()
        if self._pe_cache and (now_ts - self._pe_cache_ts) < 1800:
            return self._pe_cache

        try:
            client = await self.get_client()
            res = await client.get(f"{DSE_BASE_URL}/pe")
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                table = soup.find("table")
                rows = []
                if table:
                    trs = table.find_all("tr")
                    for tr in trs[1:]:
                        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                        if len(tds) >= 11:
                            rows.append({
                                "code": tds[1],
                                "close_price": tds[2],
                                "ycp": tds[3],
                                "pe1": tds[4],
                                "pe2": tds[5],
                                "pe3": tds[6],
                                "pe4": tds[7],
                                "pe5": tds[8],
                                "pe6": tds[9],
                                "trailing_pe": tds[10],
                            })
                self._pe_cache = rows
                self._pe_cache_ts = now_ts
                return rows
        except Exception as e:
            logger.error(f"Error parsing PE: {e}")
        return self._pe_cache or []

    async def get_at_a_glance(self) -> List[Dict[str, Any]]:
        """Fetch Market at a Glance multi-year comparison statistics"""
        now_ts = time.time()
        if self._aag_cache and (now_ts - self._aag_cache_ts) < 1800:
            return self._aag_cache

        try:
            client = await self.get_client()
            res = await client.get(f"{DSE_BASE_URL}/markets/at-a-glance")
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                table = soup.find("table")
                rows = []
                if table:
                    trs = table.find_all("tr")
                    if trs:
                        headers = [th.get_text(strip=True) for th in trs[0].find_all(["th", "td"])]
                        for tr in trs[1:]:
                            tds = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
                            if tds:
                                row_dict = {"particulars": tds[0]}
                                for i in range(1, len(tds)):
                                    col_name = headers[i] if i < len(headers) else f"col_{i}"
                                    row_dict[col_name] = tds[i]
                                rows.append(row_dict)
                self._aag_cache = rows
                self._aag_cache_ts = now_ts
                return rows
        except Exception as e:
            logger.error(f"Error parsing At a Glance: {e}")
        return self._aag_cache or []

    async def _worker_loop(self):
        """Background continuous worker task running in FastAPI process.
        
        During trading hours:
          - Main board prices + market summary: every 3 seconds (fast REST API)
          - Extra boards (SME, ATB, G-Sec): every 30 seconds (slow HTML scraping)
          - News: every 3 minutes
        Outside trading hours:
          - All sources: every 60 seconds
        """
        logger.info("[DSE MARKET SERVICE] Background ingestion loop started (ultra-fast 3s mode).")
        news_tick_counter = 0
        extra_boards_tick_counter = 0

        # Perform initial full fetch on startup
        try:
            await asyncio.gather(
                self.fetch_market_summary(),
                self.fetch_live_prices(),
                self.fetch_live_news(),
                self.fetch_sector_heatmap(),
                return_exceptions=True
            )
        except Exception as e:
            logger.error(f"[DSE INIT SCRAPE ERROR] {e}")

        while self._is_running:
            try:
                if self.is_trading_hour():
                    # Fast parallel fetch: main prices + market summary together
                    await asyncio.gather(
                        self.fetch_market_summary(),
                        self._fetch_main_board_prices_only(),
                        return_exceptions=True
                    )

                    extra_boards_tick_counter += 1
                    news_tick_counter += 1

                    # Extra boards (SME/ATB/G-Sec) every 30s (~10 cycles of 3s)
                    if extra_boards_tick_counter >= 10:
                        extra_boards_tick_counter = 0
                        asyncio.create_task(self._refresh_extra_boards())

                    # News every 3 minutes (~60 cycles of 3s)
                    if news_tick_counter >= 60:
                        news_tick_counter = 0
                        asyncio.create_task(self.fetch_live_news())

                    await asyncio.sleep(3.0)  # 3-second cycle during active trading
                else:
                    # Market closed - slower refresh
                    await asyncio.gather(
                        self.fetch_market_summary(),
                        self.fetch_live_prices(),
                        return_exceptions=True
                    )
                    news_tick_counter += 1
                    if news_tick_counter >= 5:
                        news_tick_counter = 0
                        await self.fetch_live_news()
                    await asyncio.sleep(60.0)  # 60s when market is closed

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[DSE WORKER LOOP ERROR] {e}")
                await asyncio.sleep(5.0)

        logger.info("[DSE MARKET SERVICE] Background ingestion loop stopped.")

    async def _fetch_main_board_prices_only(self):
        """Fetch only main board (PUBLIC) prices — fast REST API call, no HTML scraping."""
        try:
            client = await self.get_client()
            res = await client.get(PRICES_ENDPOINT)
            if res.status_code != 200:
                return

            data = res.json()
            cols = data.get("cols", [])
            rows = data.get("rows", [])
            col_map = {col: idx for idx, col in enumerate(cols)}
            now_iso = datetime.now(BST).isoformat()
            now_ts = int(time.time())
            diffs = []
            parsed_prices: Dict[str, Dict[str, Any]] = dict(self._last_prices)

            for row in rows:
                if not row or len(row) < len(cols):
                    continue
                code = str(row[col_map["code"]]).strip()
                if not code:
                    continue

                ltp = float(row[col_map.get("ltp", 1)] or 0.0)
                ycp = float(row[col_map.get("ycp", 2)] or 0.0)
                open_p = float(row[col_map.get("open", 3)] or 0.0)
                high_p = float(row[col_map.get("high", 4)] or 0.0)
                low_p = float(row[col_map.get("low", 5)] or 0.0)
                close_p = float(row[col_map.get("close", 6)] or 0.0)
                volume = int(row[col_map.get("volume", 7)] or 0)
                value_mn = float(row[col_map.get("value", 8)] or 0.0)
                trades = int(row[col_map.get("trades", 9)] or 0)
                percent = float(row[col_map.get("percent", 10)] or 0.0)
                category = str(row[col_map.get("category", 11)] or "").strip()
                board = str(row[col_map.get("board", 12)] or "").strip()
                sector = str(row[col_map.get("sector", 13)] or "").strip()
                asset_type = str(row[col_map.get("assetType", 14)] or "").strip()
                change_val = round(ltp - ycp, 2) if ycp > 0 else 0.0

                item = {
                    "ticker": code, "ltp": ltp, "ycp": ycp, "open": open_p,
                    "high": high_p, "low": low_p, "close": close_p,
                    "volume": volume, "value_mn": value_mn, "trades": trades,
                    "percent": percent, "change": change_val,
                    "category": category, "board": board, "sector": sector,
                    "asset_type": asset_type, "updated_at": now_iso
                }
                parsed_prices[code] = item

                prev = self._last_prices.get(code)
                if prev is None or prev["ltp"] != ltp or prev["volume"] != volume or prev["trades"] != trades:
                    if prev is not None and prev.get("ltp") is not None and prev["ltp"] != ltp:
                        tick_dir = "up" if ltp > prev["ltp"] else "down"
                    else:
                        tick_dir = "up" if change_val > 0 else ("down" if change_val < 0 else "neutral")
                    diffs.append({
                        "ticker": code, "ltp": ltp, "change": change_val,
                        "percent": percent, "volume": volume, "value_mn": value_mn,
                        "high": high_p, "low": low_p, "trades": trades, "direction": tick_dir
                    })

                # Intraday history (at most one per 60 seconds)
                if code not in self._intraday_history:
                    self._intraday_history[code] = []
                history = self._intraday_history[code]
                if not history or (now_ts - history[-1]["time"]) >= 60:
                    history.append({"time": now_ts, "value": ltp, "volume": volume})
                    if len(history) > 300:
                        self._intraday_history[code] = history[-300:]

            self._last_prices = parsed_prices
            self._last_scrape_time = time.time()

            if diffs:
                await self._broadcast({
                    "event": "tick_diff",
                    "timestamp": now_iso,
                    "count": len(diffs),
                    "ticks": diffs
                })
                asyncio.create_task(self._evaluate_alerts_safe(diffs))

        except Exception as e:
            logger.error(f"Error in fast main-board price fetch: {e}")

    async def _refresh_extra_boards(self):
        """Background task to refresh SME, ATB, and G-Sec prices (HTML-scraped, slower)."""
        try:
            extra_stocks = await self.fetch_all_board_extra_stocks()
            if not extra_stocks:
                return

            now_iso = datetime.now(BST).isoformat()
            now_ts = int(time.time())
            diffs = []

            for s in extra_stocks:
                code = s["ticker"]
                prev = self._last_prices.get(code)
                if prev is None or prev.get("ltp") != s["ltp"] or prev.get("volume") != s["volume"]:
                    if prev is not None and prev.get("ltp") is not None and prev.get("ltp") != s["ltp"]:
                        tick_dir = "up" if s["ltp"] > prev["ltp"] else "down"
                    else:
                        tick_dir = "up" if s["change"] > 0 else ("down" if s["change"] < 0 else "neutral")
                    diffs.append({
                        "ticker": code, "ltp": s["ltp"], "change": s["change"],
                        "percent": s["percent"], "volume": s["volume"], "value_mn": s["value_mn"],
                        "high": s["high"], "low": s["low"], "trades": s["trades"], "direction": tick_dir
                    })
                self._last_prices[code] = s

                if code not in self._intraday_history:
                    self._intraday_history[code] = []
                history = self._intraday_history[code]
                if not history or (now_ts - history[-1]["time"]) >= 60:
                    history.append({"time": now_ts, "value": s["ltp"], "volume": s["volume"]})
                    if len(history) > 300:
                        self._intraday_history[code] = history[-300:]

            if diffs:
                await self._broadcast({
                    "event": "tick_diff",
                    "timestamp": now_iso,
                    "count": len(diffs),
                    "ticks": diffs
                })
        except Exception as e:
            logger.debug(f"Extra boards refresh note: {e}")

    def start_background_worker(self):
        """Start async background ingestion loop"""
        if self._bg_task is None or self._bg_task.done():
            self._is_running = True
            self._bg_task = asyncio.create_task(self._worker_loop())
            logger.info("[DSE MARKET SERVICE] Worker task launched.")

    def stop_background_worker(self):
        """Stop worker task"""
        self._is_running = False
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            self._bg_task = None
            logger.info("[DSE MARKET SERVICE] Worker task cancelled.")
