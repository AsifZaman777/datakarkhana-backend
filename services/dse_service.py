import asyncio
import logging
import time
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, AsyncGenerator, Set
import httpx
from bs4 import BeautifulSoup
from database import get_db

try:
    from fastapi import WebSocket
    from starlette.websockets import WebSocketDisconnect
except ImportError:
    WebSocket = None  # type: ignore
    class WebSocketDisconnect(Exception):
        pass

logger = logging.getLogger("dse_service")
logger.setLevel(logging.INFO)

# Bangladesh Standard Time is UTC+6
BST = timezone(timedelta(hours=6))

LANKABD_BASE_URL = "https://lankabd.com"

# Every field that, when changed, should be pushed to clients
DIFF_FIELDS = ("ltp", "change", "percent", "volume", "value_mn", "high", "low", "trades", "close", "open", "ycp")

# Live worker cadence (seconds)
LIVE_CYCLE_SEC = 2.0
CLOSED_CYCLE_SEC = 2.5


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


def parse_depth_table(html_str: Optional[str]) -> List[Dict[str, Any]]:
    """Parse HTML depth tables (both DSE table format and CSE column format) into structured orders"""
    results: List[Dict[str, Any]] = []
    if not html_str:
        return results
    try:
        soup = BeautifulSoup(html_str, "html.parser")
        table = soup.find("table")
        if table:
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) >= 2:
                    try:
                        p = float(tds[0].get_text(strip=True).replace(",", ""))
                        v = int(float(tds[1].get_text(strip=True).replace(",", "")))
                        results.append({"price": p, "volume": v, "orders": 1})
                    except Exception:
                        pass
            return results

        col6s = soup.find_all("div", class_="col-6")
        if len(col6s) >= 2:
            p_divs = col6s[0].find_all("div", class_="aci_text")
            v_divs = col6s[1].find_all("div", class_="aci_text")
            for pd, vd in zip(p_divs, v_divs):
                try:
                    p = float(pd.get_text(strip=True).replace(",", ""))
                    v = int(float(vd.get_text(strip=True).replace(",", "")))
                    results.append({"price": p, "volume": v, "orders": 1})
                except Exception:
                    pass
            return results
    except Exception as e:
        logger.debug(f"[Depth Parse Error] {e}")
    return results


class DSEMarketService:
    _instance: Optional["DSEMarketService"] = None

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None
        self._bg_task: Optional[asyncio.Task] = None
        self._is_running: bool = False

        # LankaBangla anti-forgery token state
        self._lankabd_token: Optional[str] = None
        self._token_time: float = 0.0
        self._token_lock: asyncio.Lock = asyncio.Lock()

        # In-Memory Cache
        self._last_prices: Dict[str, Dict[str, Any]] = {}
        self._last_market_summary: Dict[str, Any] = {}
        self._recent_news: List[Dict[str, Any]] = []
        self._intraday_history: Dict[str, List[Dict[str, Any]]] = {}  # ticker -> list of minute ticks
        self._subscribers: List[asyncio.Queue] = []  # SSE subscribers
        self._ws_clients: Set[Any] = set()  # WebSocket clients
        self._last_scrape_time: float = 0.0
        self._ticker_sector_map: Dict[str, str] = {}
        self._sector_heatmap_cache: Optional[Dict[str, Any]] = None

        # Pro Binance Market Depth & Movers caches
        self._depth_cache: Dict[str, Any] = {}
        self._block_market_cache: List[Dict[str, Any]] = []
        self._last_block_market_time: float = 0.0
        self._movers_cache: List[Dict[str, Any]] = []
        self._last_movers_time: float = 0.0
        self._top_movers_cache: Dict[str, Any] = {}
        self._last_top_movers_time: float = 0.0

        # Signatures & Real-time WS client states
        self._market_sig: str = ""
        self._news_ids: Set[str] = set()
        self._ws_subscriptions: Dict[Any, Dict[str, str]] = {}
        self._recent_trades: List[Dict[str, Any]] = []

        # Company Overview and CID Map from LankaBangla
        self._ticker_cid_map: Dict[str, int] = {}
        self._ticker_name_map: Dict[str, str] = {}
        self._company_overview_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}

        # LankaBangla real-time market status & exchanges cache
        self._exchanges_cache: List[Dict[str, Any]] = []
        self._last_exchanges_time: float = 0.0
        self._market_status_map: Dict[str, str] = {"DSE": "Closed", "CSE": "Closed"}

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
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=httpx.Timeout(15.0, connect=8.0),
                follow_redirects=True,
                verify=False
            )
        return self._http_client

    async def get_lankabd_headers(self, force_refresh: bool = False) -> Dict[str, str]:
        """Obtain or refresh LankaBangla Anti-Forgery token and headers"""
        async with self._token_lock:
            now = time.time()
            if force_refresh or not self._lankabd_token or (now - self._token_time) > 1800:
                try:
                    client = await self.get_client()
                    res = await client.get(f"{LANKABD_BASE_URL}/Home/DSELiveStockWatch")
                    soup = BeautifulSoup(res.text, "html.parser")
                    inp = soup.find("input", {"name": "__RequestVerificationToken"})
                    if inp and inp.get("value"):
                        self._lankabd_token = inp.get("value")
                        self._token_time = now
                        logger.info(f"[LankaBD] Anti-forgery verification token refreshed (len={len(self._lankabd_token)})")
                    else:
                        logger.warning("[LankaBD] Failed to find __RequestVerificationToken in HTML")
                except Exception as e:
                    logger.error(f"[LankaBD] Error acquiring verification token: {e}")

            return {
                "Accept": "application/json, text/plain, */*",
                "RequestVerificationToken": self._lankabd_token or "",
                "Referer": f"{LANKABD_BASE_URL}/Home/DSELiveStockWatch",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            }

    def _is_clock_trading_hour(self) -> bool:
        """Fallback trading hours: Sunday (6) to Thursday (3), 09:55 AM to 2:35 PM BST (UTC+6)."""
        now_bst = datetime.now(BST)
        weekday = now_bst.weekday()
        if weekday in (4, 5):  # Friday, Saturday
            return False
        current_time = now_bst.time()
        start_time = datetime.strptime("09:55:00", "%H:%M:%S").time()
        end_time = datetime.strptime("14:35:00", "%H:%M:%S").time()
        return start_time <= current_time <= end_time

    def is_trading_hour(self, exchange: str = "DSE") -> bool:
        """
        Trading hour state directly synchronized from LankaBangla portal live marketStatus.
        Returns True if status is 'Open' or 'Pre-Open'. Falls back to clock schedule if uninitialized.
        """
        st = self._market_status_map.get(exchange.upper(), "").strip().lower()
        if st:
            return st in ("open", "pre-open")
        return self._is_clock_trading_hour()

    def get_market_status(self, exchange: str = "DSE") -> str:
        """Get live market status string (e.g. 'Open', 'Pre-Open', 'Post-Close', 'Closed') directly from LankaBangla"""
        st = self._market_status_map.get(exchange.upper())
        if st:
            return st
        return "Open" if self._is_clock_trading_hour() else "Closed"

    async def fetch_sector_heatmap(self) -> Dict[str, Any]:
        """Fetch all 19 sectors, stock mappings, and generate sector heatmap layout from LankaBangla"""
        try:
            client = await self.get_client()
            headers = await self.get_lankabd_headers()
            res = await client.get(f"{LANKABD_BASE_URL}/api/datafeed/IndexLiveData/SectorWiseMarketMapd3", headers=headers)
            
            if res.status_code == 400:
                headers = await self.get_lankabd_headers(force_refresh=True)
                res = await client.get(f"{LANKABD_BASE_URL}/api/datafeed/IndexLiveData/SectorWiseMarketMapd3", headers=headers)

            if res.status_code != 200:
                logger.warning(f"[LankaBD] Sector heatmap API returned status: {res.status_code}")
                return self._sector_heatmap_cache or {}

            data = res.json()
            children = data.get("children", [])
            sectors_list = []
            adv_count = 0
            dec_count = 0
            unch_count = 0

            for s in children:
                s_name = s.get("name", "").strip()
                stocks = s.get("children", [])
                total_turnover = sum(float(x.get("turnover") or 0.0) for x in stocks)
                pcts = []
                for x in stocks:
                    sym = str(x.get("symbol") or "").strip()
                    if sym:
                        self._ticker_sector_map[sym] = s_name

                    ch = float(x.get("pricechange") or 0.0)
                    if ch > 0:
                        adv_count += 1
                    elif ch < 0:
                        dec_count += 1
                    else:
                        unch_count += 1

                    try:
                        pcts.append(float(x.get("pricechangepct") or 0.0))
                    except (ValueError, TypeError):
                        pass

                avg_pct = round(sum(pcts) / len(pcts), 2) if pcts else 0.0

                if avg_pct > 0:
                    alpha = min(0.9, max(0.4, 0.4 + (avg_pct / 5.0) * 0.5))
                    bg_color = f"rgba(29, 122, 63, {alpha:.2f})"
                elif avg_pct < 0:
                    alpha = min(0.9, max(0.4, 0.4 + (abs(avg_pct) / 5.0) * 0.5))
                    bg_color = f"rgba(192, 57, 43, {alpha:.2f})"
                else:
                    bg_color = "rgba(100, 116, 139, 0.5)"

                sectors_list.append({
                    "code": s_name,
                    "name": s_name,
                    "change_pct": avg_pct,
                    "turnover": round(total_turnover, 2),
                    "bg_color": bg_color,
                    "stocks_count": len(stocks)
                })

            sorted_sectors = sorted(sectors_list, key=lambda x: x["turnover"], reverse=True)
            top_12 = []
            for idx, sec in enumerate(sorted_sectors[:12]):
                col = 2 if idx < 7 else 1
                row = 2 if idx < 3 else 1
                top_12.append({**sec, "col_span": col, "row_span": row})

            result = {
                "adv": adv_count,
                "dec": dec_count,
                "unch": unch_count,
                "sectors": sorted_sectors,
                "top_12": top_12,
                "last_scraped_at": datetime.now(BST).isoformat(),
                "source": "LankaBangla Portal"
            }
            self._sector_heatmap_cache = result
            return result
        except Exception as e:
            logger.error(f"[LankaBD] Error fetching sector heatmap: {e}")
            return self._sector_heatmap_cache or {}

    async def fetch_market_summary(self) -> Dict[str, Any]:
        """Fetch market indices (DSEX, DS30, DSES), turnover, volume, and breadth from LankaBangla"""
        try:
            client = await self.get_client()
            headers = await self.get_lankabd_headers()

            # 1. Fetch trade statistics
            stat_res = await client.get(f"{LANKABD_BASE_URL}/api/datafeed/IndexLiveData/LiveDSETradeStatistics", headers=headers)
            if stat_res.status_code == 400:
                headers = await self.get_lankabd_headers(force_refresh=True)
                stat_res = await client.get(f"{LANKABD_BASE_URL}/api/datafeed/IndexLiveData/LiveDSETradeStatistics", headers=headers)

            stat = stat_res.json() if stat_res.status_code == 200 else {}

            # 2. Fetch live indices CDP
            async def get_index(sym: str) -> Optional[Dict[str, Any]]:
                try:
                    r = await client.get(f"{LANKABD_BASE_URL}/api/datafeed/IndexLiveData/LiveIndexSummaryCDP?symbol={sym}", headers=headers)
                    if r.status_code == 200:
                        v = r.json()
                        val = clean_float(v[0])
                        chg = clean_float(v[1])
                        pct = clean_float(v[2])
                        return {
                            "key": sym,
                            "value": val,
                            "change": chg,
                            "percent": pct,
                            "prev": round(val - chg, 2)
                        }
                except Exception as ex:
                    logger.debug(f"[LankaBD] Index {sym} note: {ex}")
                return None

            dsex, ds30, dses = await asyncio.gather(get_index("DSEX"), get_index("DS30"), get_index("DSES"))
            indices = [i for i in [dsex, ds30, dses] if i]

            turnover_val = clean_float(stat.get("value"))
            trades_cnt = clean_int(stat.get("trades"))
            vol_cnt = clean_int(stat.get("volume"))
            trade_time = stat.get("timestamp") or datetime.now(BST).strftime("%Y-%m-%d %H:%M:%S")

            breadth = {
                "advanced": clean_int(stat.get("priceupsymbols")),
                "declined": clean_int(stat.get("pricedownsymbols")),
                "unchanged": clean_int(stat.get("priceflatsymbols"))
            }

            date_part = trade_time.split(" ")[0] if " " in trade_time else datetime.now(BST).strftime("%Y-%m-%d")
            time_part = trade_time.split(" ")[1] if " " in trade_time else ""

            dse_status = self.get_market_status("DSE")
            summary_data = {
                "indices": indices,
                "totals": {
                    "trades": trades_cnt,
                    "volume": vol_cnt,
                    "value": turnover_val,
                    "turnover": turnover_val,
                    "tradeTime": trade_time
                },
                "breadth": breadth,
                "market_status": dse_status,
                "exchanges": self.fetch_exchanges(),
                "session": {
                    "state": dse_status.upper(),
                    "date": date_part,
                    "time": time_part
                }
            }

            sig = json.dumps(summary_data, sort_keys=True, default=str)
            changed = sig != self._market_sig
            self._market_sig = sig
            self._last_market_summary = summary_data

            if changed:
                asyncio.create_task(self._broadcast({
                    "event": "market_update",
                    "timestamp": datetime.now(BST).isoformat(),
                    "is_trading_hour": self.is_trading_hour(),
                    "market_status": dse_status,
                    "exchanges": self.fetch_exchanges(),
                    "total_tracked": len(self._last_prices),
                    "market_summary": summary_data
                }))

            return summary_data
        except Exception as e:
            logger.error(f"[LankaBD] Error fetching market summary: {e}")
            return self._last_market_summary

    async def fetch_live_prices(self) -> Dict[str, Any]:
        """Fetch real-time stock prices from LankaBangla LiveStockWatchData, compute diffs, and stream"""
        try:
            client = await self.get_client()
            headers = await self.get_lankabd_headers()
            res = await client.get(f"{LANKABD_BASE_URL}/api/datafeed/IndexLiveData/LiveStockWatchData", headers=headers)

            if res.status_code == 400:
                headers = await self.get_lankabd_headers(force_refresh=True)
                res = await client.get(f"{LANKABD_BASE_URL}/api/datafeed/IndexLiveData/LiveStockWatchData", headers=headers)

            if res.status_code != 200:
                logger.warning(f"[LankaBD] LiveStockWatchData returned status: {res.status_code}")
                return {"updated_count": 0, "diffs": []}

            data = res.json()
            items = data.get("items", [])
            now_iso = datetime.now(BST).isoformat()
            now_ts = int(time.time())

            diffs = []
            parsed_prices: Dict[str, Dict[str, Any]] = {}

            for it in items:
                code = str(it.get("mkistaT_INSTRUMENT_CODE") or "").strip()
                if not code:
                    continue

                ltp = clean_float(it.get("lastTradedPrice") or it.get("mkistaT_PUB_LAST_TRADED_PRICE"))
                ycp = clean_float(it.get("mkistaT_YDAY_CLOSE_PRICE"))
                open_p = clean_float(it.get("mkistaT_OPEN_PRICE"))
                high_p = clean_float(it.get("mkistaT_HIGH_PRICE"))
                low_p = clean_float(it.get("mkistaT_LOW_PRICE"))
                close_p = clean_float(it.get("mkistaT_CLOSE_PRICE"))
                volume = clean_int(it.get("mkistaT_TOTAL_VOLUME"))
                value_mn = clean_float(it.get("mkistaT_TOTAL_VALUE"))
                trades = clean_int(it.get("mkistaT_TOTAL_TRADES"))
                
                chg = clean_float(it.get("priceChange"))
                if chg == 0.0 and ycp > 0 and ltp > 0:
                    chg = round(ltp - ycp, 2)

                pct = clean_float(it.get("priceChangePCT"))
                if pct == 0.0 and ycp > 0 and chg != 0.0:
                    pct = round((chg / ycp) * 100, 2)

                cat_raw = str(it.get("mkistaT_QUOTE_BASES") or "A").strip()
                cat = cat_raw.split("-")[0] if "-" in cat_raw else cat_raw
                name = str(it.get("companyName") or code).strip()
                cid_val = clean_int(it.get("companyID"))
                if cid_val > 0:
                    self._ticker_cid_map[code.upper()] = cid_val
                    self._ticker_name_map[code.upper()] = name

                sector = self._ticker_sector_map.get(code, "General")
                asset_type = "MF" if "MF" in cat_raw.upper() else ("CB" if "BOND" in code or "BOND" in cat_raw.upper() else "EQUITY")
                board = "DEBT" if asset_type == "CB" else ("SME" if "SME" in cat_raw.upper() else "MAIN")

                ticker_record = {
                    "ticker": code,
                    "name": name,
                    "cid": cid_val if cid_val > 0 else None,
                    "ltp": ltp,
                    "ycp": ycp,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": volume,
                    "value_mn": value_mn,
                    "trades": trades,
                    "percent": pct,
                    "change": chg,
                    "category": cat,
                    "board": board,
                    "sector": sector,
                    "asset_type": asset_type,
                    "updated_at": it.get("mkistaT_LM_DATE_TIME") or now_iso
                }
                parsed_prices[code] = ticker_record

                diff_item = self._compute_diff(code, ticker_record, self._last_prices.get(code))
                if diff_item:
                    diffs.append(diff_item)

                # Record intraday history point
                self._record_intraday(code, ltp, volume, now_ts)

            self._last_prices = parsed_prices
            self._last_scrape_time = time.time()

            if diffs:
                # Generate real-time executed trade ticks for Binance Market Trades stream
                trade_ticks = []
                for d in diffs:
                    trade_ticks.append({
                        "id": f"tr_{d['ticker']}_{now_ts}_{len(trade_ticks)}",
                        "ticker": d["ticker"],
                        "price": d.get("ltp") or 0.0,
                        "size": d.get("volume") or 0,
                        "time": datetime.now(BST).strftime("%H:%M:%S"),
                        "is_buy": d.get("direction") == "up",
                        "direction": d.get("direction") or "neutral",
                    })

                self._recent_trades = (trade_ticks + self._recent_trades)[:60]

                await self._broadcast({
                    "event": "tick_diff",
                    "timestamp": now_iso,
                    "source": "lankabd_stream",
                    "count": len(diffs),
                    "ticks": diffs,
                    "trades": trade_ticks[:15]
                })
                asyncio.create_task(self._evaluate_alerts_safe(diffs))

            return {
                "updated_count": len(diffs),
                "total_tickers": len(parsed_prices),
                "diffs": diffs
            }
        except Exception as e:
            logger.error(f"[LankaBD] Error fetching live prices: {e}")
            return {"updated_count": 0, "diffs": [], "error": str(e)}

    async def fetch_live_news(self) -> List[Dict[str, Any]]:
        """Fetch market announcements & corporate disclosures from LankaBangla portal"""
        try:
            client = await self.get_client()
            headers = await self.get_lankabd_headers()
            parsed_news: List[Dict[str, Any]] = []

            # 1. Scrape corporate announcements (PSI & notices)
            try:
                ann_res = await client.get(f"{LANKABD_BASE_URL}/Home/marketannouncements?catName=Last_7_Days")
                if ann_res.status_code == 200:
                    soup = BeautifulSoup(ann_res.text, "html.parser")
                    items = soup.find_all("div", class_="list-group-item")
                    for it in items:
                        sym_a = it.find("a", href=lambda h: h and "/Company/Overview" in h)
                        sym = sym_a.get_text(strip=True) if sym_a else "DSE"
                        p = it.find("p")
                        body = p.get_text(strip=True) if p else ""
                        date_span = it.find("span", class_="text-dark")
                        d_str = date_span.get_text(strip=True) if date_span else datetime.now(BST).strftime("%d %b, %Y")
                        summary = body[:130] + "..." if len(body) > 130 else body
                        parsed_news.append({
                            "id": f"ann_{sym}_{abs(hash(body)) % 1000000}",
                            "code": sym,
                            "name": sym,
                            "type": "ANNOUNCEMENT",
                            "date": d_str,
                            "time": "",
                            "summary": summary,
                            "body": body
                        })
            except Exception as ex:
                logger.debug(f"[LankaBD] Announcements fetch note: {ex}")

            # 2. Fetch stock market news
            try:
                news_res = await client.get(f"{LANKABD_BASE_URL}/api/APINews/GetNewsPagedList?size=15", headers=headers)
                if news_res.status_code == 200:
                    n_data = news_res.json()
                    for n in n_data.get("items", []):
                        title = str(n.get("title") or "").strip()
                        raw_desc = n.get("description") or ""
                        desc = BeautifulSoup(raw_desc, "html.parser").get_text(strip=True) if raw_desc else title
                        parsed_news.append({
                            "id": f"news_{abs(hash(title)) % 1000000}",
                            "code": "MARKET",
                            "name": "DSE Market",
                            "type": "NEWS",
                            "date": datetime.now(BST).strftime("%d %b, %Y"),
                            "time": "",
                            "summary": title,
                            "body": desc
                        })
            except Exception as ex:
                logger.debug(f"[LankaBD] News feed fetch note: {ex}")

            if parsed_news:
                self._recent_news = parsed_news[:50]
                current_ids = {str(r.get("id", "")) for r in self._recent_news if r.get("id")}
                new_ids = current_ids - self._news_ids if self._news_ids else set()
                self._news_ids |= current_ids

                if new_ids:
                    asyncio.create_task(self._broadcast({
                        "event": "news_update",
                        "timestamp": datetime.now(BST).isoformat(),
                        "new_ids": sorted(new_ids),
                        "news": self._recent_news[:25]
                    }))
                    asyncio.create_task(self._persist_news(parsed_news[:20]))

            return self._recent_news
        except Exception as e:
            logger.error(f"[LankaBD] Error fetching news: {e}")
            return self._recent_news

    def _build_fallback_depth(self, symbol: str, exchange: str = "DSE") -> Dict[str, Any]:
        """Construct realistic order book depth based on last traded price if exchange is closed or book is thin"""
        sym = symbol.upper().strip()
        t = self._last_prices.get(sym) or {}
        ltp = clean_float(t.get("ltp") or t.get("close") or t.get("ycp"), default=100.0)
        if ltp <= 0:
            ltp = 100.0

        tick_step = 0.1 if ltp < 100 else (0.2 if ltp < 500 else 0.5)
        bids = []
        asks = []

        for i in range(1, 11):
            bp = round(ltp - (i * tick_step), 2)
            bv = max(10, int(5000 / (i + 1)) * 10)
            bids.append({"price": bp, "volume": bv, "orders": max(1, 10 - i)})

            ap = round(ltp + (i * tick_step), 2)
            av = max(10, int(4500 / (i + 1)) * 10)
            asks.append({"price": ap, "volume": av, "orders": max(1, 10 - i)})

        cum_bids = 0
        for b in bids:
            cum_bids += b["volume"]
            b["total"] = cum_bids

        cum_asks = 0
        for a in asks:
            cum_asks += a["volume"]
            a["total"] = cum_asks

        tot_b = sum(b["volume"] for b in bids)
        tot_a = sum(a["volume"] for a in asks)
        tot = tot_b + tot_a or 1
        buy_pct = round((tot_b / tot) * 100, 2)
        sell_pct = round((tot_a / tot) * 100, 2)

        return {
            "symbol": sym,
            "exchange": exchange,
            "bids": bids,
            "asks": asks,
            "buy_percentage": buy_pct,
            "sell_percentage": sell_pct,
            "total_buy_volume": tot_b,
            "total_sell_volume": tot_a,
            "stats": {
                "ltp": ltp,
                "open": clean_float(t.get("open") or ltp),
                "high": clean_float(t.get("high") or ltp),
                "low": clean_float(t.get("low") or ltp),
                "close": clean_float(t.get("close") or ltp),
                "ycp": clean_float(t.get("ycp") or ltp),
                "trades": clean_int(t.get("trades")),
                "volume": clean_int(t.get("volume")),
                "value_mn": clean_float(t.get("value_mn")),
            },
            "as_of": datetime.now(BST).isoformat()
        }

    async def fetch_market_depth(self, symbol: str, exchange: str = "DSE") -> Dict[str, Any]:
        """Fetch real-time order book / market depth from LankaBangla for DSE or CSE"""
        sym = symbol.upper().strip()
        exch = exchange.upper().strip() if exchange else "DSE"
        cache_key = f"{sym}_{exch}"
        now = time.time()

        cached = self._depth_cache.get(cache_key)
        if cached and (now - cached[0]) < 1.5:
            return cached[1]

        try:
            client = await self.get_client()
            headers = await self.get_lankabd_headers()
            depth_headers = {
                **headers,
                "Referer": f"{LANKABD_BASE_URL}/Home/MarketDepth?mktDepthSymbol={sym}",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }
            post_data = {"Symbol": sym, "Exchange": exch}
            res = await client.post(f"{LANKABD_BASE_URL}/Home/MarketDepthData", data=post_data, headers=depth_headers)

            if res.status_code == 400:
                try:
                    ref_res = await client.get(f"{LANKABD_BASE_URL}/Home/MarketDepth?mktDepthSymbol={sym}")
                    ref_soup = BeautifulSoup(ref_res.text, "html.parser")
                    inp = ref_soup.find("input", {"name": "__RequestVerificationToken"})
                    if inp and inp.get("value"):
                        self._lankabd_token = inp.get("value")
                        depth_headers["RequestVerificationToken"] = self._lankabd_token
                except Exception:
                    pass
                res = await client.post(f"{LANKABD_BASE_URL}/Home/MarketDepthData", data=post_data, headers=depth_headers)

            if res.status_code == 200:
                data = res.json()
                bids = parse_depth_table(data.get("buyPriceTable"))
                asks = parse_depth_table(data.get("sellPriceTable"))

                if not bids and not asks:
                    fallback = self._build_fallback_depth(sym, exch)
                    self._depth_cache[cache_key] = (now, fallback)
                    return fallback

                # Sort bids descending, asks ascending
                bids.sort(key=lambda x: x["price"], reverse=True)
                asks.sort(key=lambda x: x["price"])

                cum_bids = 0
                for b in bids:
                    cum_bids += b["volume"]
                    b["total"] = cum_bids

                cum_asks = 0
                for a in asks:
                    cum_asks += a["volume"]
                    a["total"] = cum_asks

                buy_pct = clean_float(data.get("buyPercentage"))
                sell_pct = clean_float(data.get("sellPercentage"))
                tot_b = clean_int(data.get("totalBuyVolume")) or sum(b["volume"] for b in bids)
                tot_a = clean_int(data.get("totalSellVolume")) or sum(a["volume"] for a in asks)
                if buy_pct == 0.0 and sell_pct == 0.0 and (tot_b + tot_a) > 0:
                    buy_pct = round((tot_b / (tot_b + tot_a)) * 100, 2)
                    sell_pct = round((tot_a / (tot_b + tot_a)) * 100, 2)

                ltp_val = clean_float(data.get("lastTradePrice"))
                if ltp_val <= 0.0:
                    t = self._last_prices.get(sym) or {}
                    ltp_val = clean_float(t.get("ltp") or t.get("close") or t.get("ycp"))

                ycp_val = clean_float(data.get("yesterdayClosePrice")) or ltp_val
                depth_result = {
                    "symbol": sym,
                    "exchange": exch,
                    "bids": bids,
                    "asks": asks,
                    "buy_percentage": buy_pct,
                    "sell_percentage": sell_pct,
                    "total_buy_volume": tot_b,
                    "total_sell_volume": tot_a,
                    "lankabd_url": f"https://lankabd.com/Home/MarketDepth?mktDepthSymbol={sym}",
                    "circuit_upper": round(ycp_val * 1.10, 2) if ycp_val > 0 else round(ltp_val * 1.10, 2),
                    "circuit_lower": round(ycp_val * 0.90, 2) if ycp_val > 0 else round(ltp_val * 0.90, 2),
                    "stats": {
                        "ltp": ltp_val,
                        "open": clean_float(data.get("openPrice")),
                        "high": clean_float(data.get("daysHigh")),
                        "low": clean_float(data.get("daysLow")),
                        "close": clean_float(data.get("closePrice")),
                        "ycp": clean_float(data.get("yesterdayClosePrice")),
                        "trades": clean_int(data.get("noOfTrade")),
                        "volume": clean_int(data.get("totalVolume")),
                        "value_mn": clean_float(data.get("totalValueMN") or data.get("totalValueBDT")),
                    },
                    "y_stats": {
                        "open": clean_float(data.get("yOpenPrice")),
                        "high": clean_float(data.get("yDaysHigh")),
                        "low": clean_float(data.get("yDaysLow")),
                        "close": clean_float(data.get("yClosePrice")),
                        "ltp": clean_float(data.get("yLastTradePrice")),
                        "trades": clean_int(data.get("yNoOfTrade")),
                        "volume": clean_int(data.get("yTotalVolume")),
                        "value_bdt": clean_float(data.get("yTotalValueBDT")),
                    },
                    "as_of": datetime.now(BST).isoformat()
                }
                self._depth_cache[cache_key] = (now, depth_result)
                return depth_result

        except Exception as e:
            logger.error(f"[LankaBD Depth Error] {sym} on {exch}: {e}")

        fallback = cached[1] if cached else self._build_fallback_depth(sym, exch)
        self._depth_cache[cache_key] = (now, fallback)
        return fallback

    async def fetch_block_market(self) -> List[Dict[str, Any]]:
        """Fetch latest block market transactions from LankaBangla"""
        now = time.time()
        if self._block_market_cache and (now - self._last_block_market_time) < 15.0:
            return self._block_market_cache

        try:
            client = await self.get_client()
            headers = await self.get_lankabd_headers()
            res = await client.get(f"{LANKABD_BASE_URL}/api/APIMarket/GetLatestBlockMarket", headers=headers)
            if res.status_code == 400:
                headers = await self.get_lankabd_headers(force_refresh=True)
                res = await client.get(f"{LANKABD_BASE_URL}/api/APIMarket/GetLatestBlockMarket", headers=headers)

            if res.status_code == 200:
                raw_items = res.json()
                parsed = []
                for it in raw_items:
                    parsed.append({
                        "symbol": it.get("symbol"),
                        "company_name": it.get("companyName"),
                        "max_price": clean_float(it.get("maxPrice")),
                        "min_price": clean_float(it.get("minPrice")),
                        "trades": clean_int(it.get("noOfTrades")),
                        "quantity": clean_int(it.get("quantity")),
                        "value_mn": clean_float(it.get("valueMn")),
                        "exchange": "DSE" if it.get("exchangeID") == 1 else "CSE",
                        "date": it.get("date"),
                    })
                self._block_market_cache = parsed
                self._last_block_market_time = now
                return parsed
        except Exception as e:
            logger.error(f"[LankaBD Block Market Error] {e}")
        return self._block_market_cache

    async def fetch_index_movers(self, count: int = 15) -> List[Dict[str, Any]]:
        """Fetch index movers and contribution points from LankaBangla"""
        now = time.time()
        if self._movers_cache and (now - self._last_movers_time) < 15.0:
            return self._movers_cache

        try:
            client = await self.get_client()
            headers = await self.get_lankabd_headers()
            res = await client.get(f"{LANKABD_BASE_URL}/api/APIMarket/GetIndexMover?count={count}", headers=headers)
            if res.status_code == 400:
                headers = await self.get_lankabd_headers(force_refresh=True)
                res = await client.get(f"{LANKABD_BASE_URL}/api/APIMarket/GetIndexMover?count={count}", headers=headers)

            if res.status_code == 200:
                raw = res.json()
                parsed = []
                for it in raw:
                    parsed.append({
                        "symbol": it.get("symbol"),
                        "company_name": it.get("companyName"),
                        "ltp": clean_float(it.get("ltp")),
                        "ycp": clean_float(it.get("ycp")),
                        "change_percent": clean_float(it.get("changePercent")),
                        "total_volume": clean_int(it.get("totalVolume")),
                        "total_value_mn": clean_float(it.get("totalValue")),
                        "index_mover_points": clean_float(it.get("index_Mover")),
                        "market_cap": clean_float(it.get("marketCap_T")),
                        "updated_at": it.get("idxDate"),
                    })
                self._movers_cache = parsed
                self._last_movers_time = now
                return parsed
        except Exception as e:
            logger.error(f"[LankaBD Movers Error] {e}")
        return self._movers_cache

    async def fetch_top_movers(self) -> Dict[str, Any]:
        """Fetch top gainers, losers, turnover, and volume leaders from LankaBangla"""
        now = time.time()
        if self._top_movers_cache and (now - self._last_top_movers_time) < 15.0:
            return self._top_movers_cache

        try:
            client = await self.get_client()
            headers = await self.get_lankabd_headers()

            async def _fetch_list(endpoint: str) -> List[Dict[str, Any]]:
                try:
                    r = await client.get(f"{LANKABD_BASE_URL}{endpoint}", headers=headers)
                    if r.status_code == 200:
                        items = r.json()
                        res = []
                        for it in items[:10]:
                            code = str(it.get("mkistaT_INSTRUMENT_CODE") or it.get("symbol") or "").strip()
                            ltp = clean_float(it.get("lastTradedPrice") or it.get("mkistaT_PUB_LAST_TRADED_PRICE"))
                            chg = clean_float(it.get("priceChange"))
                            pct = clean_float(it.get("priceChangePCT"))
                            vol = clean_int(it.get("mkistaT_TOTAL_VOLUME"))
                            val_mn = clean_float(it.get("mkistaT_TOTAL_VALUE"))
                            trades = clean_int(it.get("mkistaT_TOTAL_TRADES"))
                            name = str(it.get("companyName") or code).strip()
                            res.append({
                                "ticker": code,
                                "name": name,
                                "ltp": ltp,
                                "change": chg,
                                "percent": pct,
                                "volume": vol,
                                "value_mn": val_mn,
                                "trades": trades,
                            })
                        return res
                except Exception as ex:
                    logger.debug(f"[Top Movers error {endpoint}] {ex}")
                return []

            gainers, losers, turnovers, volumes = await asyncio.gather(
                _fetch_list("/api/datafeed/IndexLiveData/LiveTop20Gainers?top=10"),
                _fetch_list("/api/datafeed/IndexLiveData/LiveTop20Losers?top=10"),
                _fetch_list("/api/datafeed/IndexLiveData/LiveTop20Turnovers?top=10"),
                _fetch_list("/api/datafeed/IndexLiveData/LiveTop20Volumes?top=10"),
                return_exceptions=True
            )

            all_stocks = list(self._last_prices.values())
            if isinstance(gainers, Exception) or not gainers:
                gainers = sorted([s for s in all_stocks if s.get("percent", 0) > 0], key=lambda x: x.get("percent", 0), reverse=True)[:10]
            if isinstance(losers, Exception) or not losers:
                losers = sorted([s for s in all_stocks if s.get("percent", 0) < 0], key=lambda x: x.get("percent", 0))[:10]
            if isinstance(turnovers, Exception) or not turnovers:
                turnovers = sorted(all_stocks, key=lambda x: x.get("value_mn", 0), reverse=True)[:10]
            if isinstance(volumes, Exception) or not volumes:
                volumes = sorted(all_stocks, key=lambda x: x.get("volume", 0), reverse=True)[:10]

            self._top_movers_cache = {
                "top_gainers": gainers,
                "top_losers": losers,
                "top_turnover": turnovers,
                "top_volume": volumes,
                "updated_at": datetime.now(BST).isoformat()
            }
            self._last_top_movers_time = now
            return self._top_movers_cache
        except Exception as e:
            logger.error(f"[LankaBD Top Movers Error] {e}")
            return self._top_movers_cache or {}

    async def fetch_exchanges_status(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Fetch real-time market status directly from LankaBangla (/api/APIMarket/GetExchanges).
        Returns live status for both DSE and CSE (Open, Pre-Open, Post-Close, Closed).
        """
        now = time.time()
        if not force_refresh and self._exchanges_cache and (now - self._last_exchanges_time) < 8.0:
            return self._exchanges_cache

        try:
            client = await self.get_client()
            headers = await self.get_lankabd_headers()
            res = await client.get(f"{LANKABD_BASE_URL}/api/APIMarket/GetExchanges", headers=headers)
            if res.status_code in (400, 401):
                headers = await self.get_lankabd_headers(force_refresh=True)
                res = await client.get(f"{LANKABD_BASE_URL}/api/APIMarket/GetExchanges", headers=headers)

            if res.status_code == 200:
                raw_list = res.json()
                parsed = []
                for item in raw_list:
                    code = item.get("code", "DSE").upper()
                    mkt_status = item.get("marketStatus") or ("Open" if self._is_clock_trading_hour() else "Closed")
                    self._market_status_map[code] = mkt_status
                    parsed.append({
                        "code": code,
                        "name": item.get("name", f"{code} Stock Exchange"),
                        "exchangeID": item.get("exchangeID", 1 if code == "DSE" else 2),
                        "marketStatus": mkt_status,
                        "status": mkt_status,
                        "selected": item.get("selected", 1),
                        "logoPath": item.get("logoPath", f"/images/{code.lower()}_logo.png"),
                        "logoSizeCSS": item.get("logoSizeCSS", ""),
                        "currency": "BDT",
                        "city": "Dhaka" if code == "DSE" else "Chittagong",
                        "country": "Bangladesh",
                        "indices": ["DSEX", "DS30", "DSES"] if code == "DSE" else ["CASPI", "CSCX", "CSE30"],
                        "trading_hours": "10:00 AM - 02:30 PM (BST)"
                    })
                self._exchanges_cache = parsed
                self._last_exchanges_time = now
                return parsed
        except Exception as e:
            logger.error(f"[LankaBD Exchanges Error] {e}")

        return self.fetch_exchanges()

    def fetch_exchanges(self) -> List[Dict[str, Any]]:
        """Return list of supported stock exchanges with current cached LankaBangla status"""
        if self._exchanges_cache:
            return self._exchanges_cache
        clock_open = self._is_clock_trading_hour()
        dse_st = self._market_status_map.get("DSE", "Open" if clock_open else "Closed")
        cse_st = self._market_status_map.get("CSE", "Open" if clock_open else "Closed")
        return [
            {
                "code": "DSE",
                "name": "Dhaka Stock Exchange PLC.",
                "exchangeID": 1,
                "marketStatus": dse_st,
                "status": dse_st,
                "selected": 1,
                "logoPath": "/images/dse_logo.png",
                "currency": "BDT",
                "city": "Dhaka",
                "country": "Bangladesh",
                "indices": ["DSEX", "DS30", "DSES"],
                "trading_hours": "10:00 AM - 02:30 PM (BST)"
            },
            {
                "code": "CSE",
                "name": "Chittagong Stock Exchange PLC.",
                "exchangeID": 2,
                "marketStatus": cse_st,
                "status": cse_st,
                "selected": 1,
                "logoPath": "/images/cse_logo.png",
                "currency": "BDT",
                "city": "Chittagong",
                "country": "Bangladesh",
                "indices": ["CASPI", "CSCX", "CSE30"],
                "trading_hours": "10:00 AM - 02:30 PM (BST)"
            }
        ]


    def get_ticker_detail(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return full details, calculated technicals and intraday history for a single ticker"""
        sym = symbol.upper().strip()
        t = self._last_prices.get(sym)
        if not t:
            for code, data in self._last_prices.items():
                if code.upper() == sym:
                    t = data
                    sym = code
                    break

        if not t:
            return None

        series = self._intraday_history.get(sym, [])
        ltp = t.get("ltp") or 0.0
        high = t.get("high") or ltp
        low = t.get("low") or ltp
        ycp = t.get("ycp") or ltp

        est_52w_high = round(max(high, ltp * 1.35), 2)
        est_52w_low = round(min(low, ltp * 0.72), 2)

        return {
            **t,
            "intraday_series": series,
            "est_52w_high": est_52w_high,
            "est_52w_low": est_52w_low,
            "tick_size": 0.1,
            "circuit_upper": round(ycp * 1.10, 2) if ycp > 0 else round(ltp * 1.10, 2),
            "circuit_lower": round(ycp * 0.90, 2) if ycp > 0 else round(ltp * 0.90, 2),
            "source": "LankaBangla Portal"
        }

    async def fetch_company_overview(self, symbol: str) -> Dict[str, Any]:
        """Fetch rich company overview from LankaBangla matching OverviewV2 specifications"""
        sym = symbol.upper().strip()
        now = time.time()
        cached = self._company_overview_cache.get(sym)
        if cached and (now - cached[0]) < 300.0:
            return cached[1]

        cid = self._ticker_cid_map.get(sym)
        if not cid:
            t = self._last_prices.get(sym)
            if t and t.get("cid"):
                cid = t["cid"]
            else:
                await self.fetch_live_prices()
                cid = self._ticker_cid_map.get(sym)

        t = self._last_prices.get(sym) or {}
        company_name = self._ticker_name_map.get(sym) or t.get("name") or sym

        if not cid:
            logger.warning(f"[LankaBD Overview] Could not resolve CID for symbol {sym}")
            fallback = {
                "symbol": sym,
                "name": company_name,
                "cid": None,
                "sector": t.get("sector", "General"),
                "category": t.get("category", "A"),
                "board": t.get("board", "MAIN"),
                "ltp": t.get("ltp", 0.0),
                "profile": {},
                "statistics": {},
                "shareholding": [],
                "financial_ratios": [],
                "dividend_history": [],
                "interim_reports": [],
                "board_members": [],
                "auditors": [],
                "contacts": [],
                "lankabd_url": f"{LANKABD_BASE_URL}/Company/OverviewV2?sn={sym}",
                "as_of": datetime.now(BST).isoformat()
            }
            return fallback

        try:
            client = await self.get_client()
            ref_url = f"{LANKABD_BASE_URL}/Company/OverviewV2?cid={cid}&sn={sym}"
            ref_res = await client.get(ref_url)
            soup = BeautifulSoup(ref_res.text, "html.parser")
            inp = soup.find("input", {"name": "__RequestVerificationToken"})
            token_val = inp["value"] if inp else (self._lankabd_token or "")

            api_headers = {
                "Accept": "application/json, text/plain, */*",
                "RequestVerificationToken": token_val,
                "Referer": ref_url,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            }

            async def _safe_get(endpoint: str) -> Any:
                try:
                    r = await client.get(f"{LANKABD_BASE_URL}{endpoint}", headers=api_headers, timeout=12.0)
                    if r.status_code == 200:
                        return r.json()
                except Exception as ex:
                    logger.debug(f"[LankaBD Overview {endpoint}] {ex}")
                return None

            profile_res, stats_res, shareholding_res, ratios_res, div_res, interim_res, board_res, auditor_res, contacts_res = await asyncio.gather(
                _safe_get(f"/api/Company/Profile?cid={cid}"),
                _safe_get(f"/api/company/StockStatisticsV2?cid={cid}"),
                _safe_get(f"/api/company/ShareholdingPattern?cid={cid}"),
                _safe_get(f"/api/company/FinancialRatiosV2?cid={cid}"),
                _safe_get(f"/api/company/StatsDividendHistory?cid={cid}"),
                _safe_get(f"/api/company/StatsInterimFinReport?cid={cid}"),
                _safe_get(f"/api/company/BoardMembers?cid={cid}"),
                _safe_get(f"/api/company/Auditors?cid={cid}"),
                _safe_get(f"/api/company/Contacts?cid={cid}"),
                return_exceptions=True
            )

            profile = profile_res if isinstance(profile_res, dict) else {}
            
            raw_stats = stats_res[0] if (isinstance(stats_res, list) and stats_res) else (stats_res if isinstance(stats_res, dict) else {})
            statistics = {}
            if isinstance(raw_stats, dict):
                for k, v in raw_stats.items():
                    clean_k = k.split("-")[0].strip().replace(" ", "_").lower()
                    statistics[clean_k] = v
                statistics["raw"] = raw_stats

            shareholding = shareholding_res if isinstance(shareholding_res, list) else []
            ratios_list = []
            if isinstance(ratios_res, list):
                for yr_item in ratios_res:
                    if isinstance(yr_item, dict) and "ratioList" in yr_item:
                        yr = yr_item.get("year")
                        for r in yr_item.get("ratioList", []):
                            if isinstance(r, dict):
                                r_entry = dict(r)
                                r_entry["year"] = yr
                                ratios_list.append(r_entry)
                    elif isinstance(yr_item, dict) and "Name" in yr_item:
                        ratios_list.append(yr_item)
            elif isinstance(ratios_res, dict):
                ratios_list = ratios_res.get("data", [])
            dividends = div_res if isinstance(div_res, list) else []
            interim = interim_res if isinstance(interim_res, list) else []

            board_members = []
            if isinstance(board_res, list):
                for b in board_res:
                    board_members.append({
                        "name": b.get("nameENG") or b.get("nameBEN"),
                        "designation": b.get("designation"),
                        "level": b.get("managementLevelNo"),
                    })

            auditors = []
            if isinstance(auditor_res, list):
                for a in auditor_res:
                    auditors.append({
                        "name": a.get("nameENG") or a.get("nameBEN"),
                    })

            contacts = []
            if isinstance(contacts_res, list):
                for c in contacts_res:
                    contacts.append({
                        "name": c.get("nameENG"),
                        "contact_person": c.get("contactPerson"),
                        "address": c.get("address1"),
                        "phone": c.get("phone1"),
                        "fax": c.get("fax1"),
                        "is_headquarter": c.get("isHeadQuarter"),
                    })

            cname = profile.get("nameENG") or company_name
            cn_slug = cname.replace(" ", "_").replace(".", "").replace(",", "")

            overview_data = {
                "symbol": sym,
                "cid": cid,
                "name": cname,
                "sector": t.get("sector") or profile.get("sector", "General"),
                "category": t.get("category") or profile.get("category", "A"),
                "board": t.get("board") or profile.get("board", "Main"),
                "ltp": t.get("ltp", 0.0),
                "change": t.get("change", 0.0),
                "percent": t.get("percent", 0.0),
                "profile": profile,
                "statistics": statistics,
                "shareholding": shareholding,
                "financial_ratios": ratios_list,
                "dividend_history": dividends,
                "interim_reports": interim,
                "board_members": board_members,
                "auditors": auditors,
                "contacts": contacts,
                "lankabd_url": f"{LANKABD_BASE_URL}/Company/OverviewV2?cid={cid}&sn={sym}&cn={cn_slug}",
                "as_of": datetime.now(BST).isoformat()
            }

            self._company_overview_cache[sym] = (now, overview_data)
            return overview_data

        except Exception as e:
            logger.error(f"[LankaBD Overview Error] {sym}: {e}")
            return {
                "symbol": sym,
                "name": company_name,
                "cid": cid,
                "sector": t.get("sector", "General"),
                "category": t.get("category", "A"),
                "board": t.get("board", "MAIN"),
                "ltp": t.get("ltp", 0.0),
                "lankabd_url": f"{LANKABD_BASE_URL}/Company/OverviewV2?cid={cid}&sn={sym}",
                "as_of": datetime.now(BST).isoformat()
            }

    def get_tickers(
        self,
        exchange: Optional[str] = None,
        board: Optional[str] = None,
        category: Optional[str] = None,
        sector: Optional[str] = None,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Filter and return cached tickers"""
        res = list(self._last_prices.values())

        if search:
            q = search.upper().strip()
            res = [s for s in res if q in s.get("ticker", "") or q in s.get("name", "").upper() or q in s.get("sector", "").upper()]

        if board and board.upper() != "ALL":
            b_up = board.upper()
            if b_up == "DEBT":
                res = [s for s in res if s.get("board") == "DEBT" or s.get("asset_type") == "CB" or "BOND" in s.get("ticker", "")]
            elif b_up == "SME":
                res = [s for s in res if s.get("board") == "SME"]
            else:
                res = [s for s in res if s.get("board", "").upper() == b_up]

        if category and category.upper() != "ALL":
            cat_up = category.upper()
            res = [s for s in res if s.get("category", "").upper() == cat_up]

        if sector and sector.strip():
            sec_q = sector.lower().strip()
            res = [s for s in res if sec_q in s.get("sector", "").lower()]

        if sort_by:
            rev = True
            if sort_by.startswith("-"):
                sort_by = sort_by[1:]
                rev = True
            elif sort_by.startswith("+"):
                sort_by = sort_by[1:]
                rev = False

            if sort_by in ("turnover", "value_mn"):
                res.sort(key=lambda x: x.get("value_mn", 0.0), reverse=rev)
            elif sort_by in ("percent", "change_pct"):
                res.sort(key=lambda x: x.get("percent", 0.0), reverse=rev)
            elif sort_by in ("volume",):
                res.sort(key=lambda x: x.get("volume", 0), reverse=rev)
            elif sort_by in ("ltp", "price"):
                res.sort(key=lambda x: x.get("ltp", 0.0), reverse=rev)
            elif sort_by in ("trades",):
                res.sort(key=lambda x: x.get("trades", 0), reverse=rev)
            elif sort_by in ("ticker", "symbol"):
                res.sort(key=lambda x: x.get("ticker", ""), reverse=rev)

        if limit and limit > 0:
            res = res[:limit]

        return res

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

            if new_inserted_items:
                alert_service = StockAlertService.get_instance()
                for n in new_inserted_items:
                    await alert_service.evaluate_news_alert(n)

        except Exception as e:
            logger.debug(f"[LankaBD] Persist news note: {e}")

    async def _evaluate_alerts_safe(self, diffs: List[Dict[str, Any]]):
        """Invoke alert evaluation without throwing exceptions"""
        try:
            from services.stock_alert_service import StockAlertService
            alert_service = StockAlertService.get_instance()
            await alert_service.evaluate_price_alerts(diffs)
        except Exception as e:
            logger.debug(f"[LankaBD] Price alerts note: {e}")

    def _compute_diff(self, code: str, item: Dict[str, Any], prev: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Return a tick diff if ANY tracked field changed (or ticker is new), else None"""
        if prev is None:
            changed = list(DIFF_FIELDS)
        else:
            changed = [f for f in DIFF_FIELDS if prev.get(f) != item.get(f)]
            if not changed:
                return None

        ltp = item.get("ltp") or 0.0
        prev_ltp = prev.get("ltp") if prev else None
        if prev_ltp is not None and prev_ltp != ltp:
            direction = "up" if ltp > prev_ltp else "down"
        elif prev is not None:
            direction = "neutral"
        else:
            ch = item.get("change") or 0.0
            direction = "up" if ch > 0 else ("down" if ch < 0 else "neutral")

        diff: Dict[str, Any] = {f: item.get(f) for f in DIFF_FIELDS}
        diff.update({
            "ticker": code,
            "name": item.get("name", code),
            "prev_ltp": prev_ltp,
            "direction": direction,
            "changed": changed,
            "category": item.get("category"),
            "board": item.get("board"),
            "sector": item.get("sector"),
            "updated_at": item.get("updated_at"),
        })
        if prev is None:
            diff = {**item, **diff, "is_new": True}
        return diff

    def _record_intraday(self, code: str, ltp: float, volume: int, now_ts: int):
        """Store at most one intraday point per 60s per ticker (max 300 points)"""
        history = self._intraday_history.setdefault(code, [])
        if not history or (now_ts - history[-1]["time"]) >= 60:
            history.append({"time": now_ts, "value": ltp, "volume": volume})
            if len(history) > 300:
                self._intraday_history[code] = history[-300:]

    async def _broadcast(self, message: Dict[str, Any]):
        """Broadcast message to all connected SSE and WebSocket clients (concurrent, non-blocking)"""
        payload_str = json.dumps(message, default=str)

        # SSE subscribers
        for q in list(self._subscribers):
            try:
                while q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                q.put_nowait(message)
            except Exception:
                if q in self._subscribers:
                    self._subscribers.remove(q)

        # WebSocket clients
        clients = list(self._ws_clients)
        if not clients:
            return

        async def _send(ws):
            await asyncio.wait_for(ws.send_text(payload_str), timeout=5.0)

        results = await asyncio.gather(*(_send(ws) for ws in clients), return_exceptions=True)
        for ws, r in zip(clients, results):
            if isinstance(r, Exception):
                self._ws_clients.discard(ws)

    async def subscribe_stream(self, request: Optional[Any] = None) -> AsyncGenerator[str, None]:
        """Async generator yielding SSE formatted messages to frontend clients"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(queue)
        
        init_payload = self._build_init_payload()
        yield f"retry: 2000\nevent: init\ndata: {json.dumps(init_payload, default=str)}\n\n"

        try:
            while True:
                if request is not None and await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                    ev_type = msg.get("event", "message")
                    yield f"event: {ev_type}\ndata: {json.dumps(msg, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield f"event: ping\ndata: {{\"timestamp\": \"{datetime.now(BST).isoformat()}\"}}\n\n"
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    async def subscribe_websocket(self, websocket: Any):
        """Handle a WebSocket connection: send initial snapshot then stream events and process real-time client actions"""
        await websocket.accept()
        self._ws_clients.add(websocket)
        self._ws_subscriptions[websocket] = {"symbol": "GP", "exchange": "DSE"}
        logger.info(f"[WS] Client connected. Total WS clients: {len(self._ws_clients)}")

        # Send initial full snapshot including depth for active default symbol
        init_payload = await self._build_init_payload_async(symbol="GP", exchange="DSE")
        try:
            await websocket.send_text(json.dumps(init_payload, default=str))
        except (WebSocketDisconnect, Exception) as e:
            logger.debug(f"[WS] Client disconnected during init snapshot: {e}")
            self._ws_clients.discard(websocket)
            self._ws_subscriptions.pop(websocket, None)
            return

        # Keep alive & action receive loop
        try:
            while True:
                try:
                    msg_text = await asyncio.wait_for(websocket.receive_text(), timeout=35.0)
                    if msg_text == "ping":
                        await websocket.send_text(json.dumps({"event": "pong"}))
                        continue

                    # Process JSON client action (subscribe_depth, etc.)
                    try:
                        cmd = json.loads(msg_text)
                        action = cmd.get("action")
                        if action == "subscribe_depth":
                            sym = str(cmd.get("symbol") or "GP").upper().strip()
                            exch = str(cmd.get("exchange") or "DSE").upper().strip()
                            self._ws_subscriptions[websocket] = {"symbol": sym, "exchange": exch}
                            # Send immediate real-time depth update for this symbol
                            depth_data = await self.fetch_market_depth(sym, exch)
                            await websocket.send_text(json.dumps({
                                "event": "depth_update",
                                "symbol": sym,
                                "exchange": exch,
                                "depth": depth_data,
                                "timestamp": datetime.now(BST).isoformat()
                            }, default=str))
                        elif action == "ping":
                            client_ts = cmd.get("client_ts") or cmd.get("ts")
                            await websocket.send_text(json.dumps({
                                "event": "pong",
                                "client_ts": client_ts,
                                "server_ts": int(time.time() * 1000)
                            }))
                    except Exception:
                        pass

                except asyncio.TimeoutError:
                    await websocket.send_text(json.dumps({
                        "event": "ping",
                        "server_ts": int(time.time() * 1000)
                    }))
        except WebSocketDisconnect:
            logger.info("[WS] Client disconnected normally")
        except Exception as e:
            logger.debug(f"[WS] Client connection ended: {e}")
        finally:
            self._ws_clients.discard(websocket)
            self._ws_subscriptions.pop(websocket, None)
            logger.info(f"[WS] Client removed. Total WS clients: {len(self._ws_clients)}")

    async def _build_init_payload_async(self, symbol: str = "GP", exchange: str = "DSE") -> Dict[str, Any]:
        """Full snapshot sent to WebSocket clients on connect, including initial live depth"""
        init_depth = await self.fetch_market_depth(symbol, exchange)

        if not self._recent_trades and self._last_prices:
            seed_trades = []
            top_stocks = sorted(list(self._last_prices.values()), key=lambda x: x.get("value_mn", 0), reverse=True)[:15]
            for idx, s in enumerate(top_stocks):
                ltp_val = clean_float(s.get("ltp"), 100.0)
                seed_trades.append({
                    "id": f"tr_{s.get('ticker')}_{idx}",
                    "ticker": s.get("ticker"),
                    "price": ltp_val,
                    "size": max(10, clean_int(s.get("volume"), 1000) // max(1, clean_int(s.get("trades"), 10))),
                    "time": datetime.now(BST).strftime("%H:%M:%S"),
                    "is_buy": clean_float(s.get("change")) >= 0,
                    "direction": "up" if clean_float(s.get("change")) >= 0 else "down"
                })
            self._recent_trades = seed_trades

        return {
            "event": "init",
            "market_summary": self._last_market_summary,
            "is_trading_hour": self.is_trading_hour(),
            "market_status": self.get_market_status("DSE"),
            "sector_heatmap": self._sector_heatmap_cache,
            "tickers_count": len(self._last_prices),
            "tickers": list(self._last_prices.values()),
            "recent_news": self._recent_news[:25],
            "block_market": self._block_market_cache[:10],
            "movers": self._movers_cache[:10],
            "top_lists": self._top_movers_cache,
            "recent_trades": self._recent_trades[:25],
            "initial_depth": init_depth,
            "exchanges": self.fetch_exchanges(),
            "last_scraped_at": datetime.fromtimestamp(self._last_scrape_time, tz=BST).isoformat() if self._last_scrape_time else None,
            "timestamp": datetime.now(BST).isoformat()
        }

    def _build_init_payload(self) -> Dict[str, Any]:
        """Synchronous snapshot sent to SSE clients on connect"""
        return {
            "event": "init",
            "market_summary": self._last_market_summary,
            "is_trading_hour": self.is_trading_hour(),
            "market_status": self.get_market_status("DSE"),
            "sector_heatmap": self._sector_heatmap_cache,
            "tickers_count": len(self._last_prices),
            "tickers": list(self._last_prices.values()),
            "recent_news": self._recent_news[:25],
            "block_market": self._block_market_cache[:10],
            "movers": self._movers_cache[:10],
            "top_lists": self._top_movers_cache,
            "recent_trades": self._recent_trades[:20],
            "exchanges": self.fetch_exchanges(),
            "last_scraped_at": datetime.fromtimestamp(self._last_scrape_time, tz=BST).isoformat() if self._last_scrape_time else None,
            "timestamp": datetime.now(BST).isoformat()
        }

    def get_ws_client_count(self) -> int:
        return len(self._ws_clients)

    async def _worker_loop(self):
        """Main background worker: scrapes LankaBangla live data and broadcasts via WebSocket"""
        logger.info("[LankaBD ENGINE] Background worker loop started.")
        cycle = 0

        # Initial bootstrap
        try:
            await self.fetch_exchanges_status()
            await self.fetch_sector_heatmap()
            await self.fetch_market_summary()
            await self.fetch_live_prices()
            await self.fetch_live_news()
            await self.fetch_block_market()
            await self.fetch_index_movers()
            await self.fetch_top_movers()
            logger.info(f"[LankaBD ENGINE] Bootstrap ready: {len(self._last_prices)} tickers, {len(self._ticker_sector_map)} sectors mapped.")
        except Exception as e:
            logger.warning(f"[LankaBD ENGINE] Bootstrap note: {e}")

        while self._is_running:
            try:
                cycle += 1
                started = time.monotonic()
                live = self.is_trading_hour()

                # 1. Fetch live prices, market summary & exchange status every cycle
                await asyncio.gather(
                    self.fetch_live_prices(),
                    self.fetch_market_summary(),
                    self.fetch_exchanges_status(),
                    return_exceptions=True
                )

                # 2. Push real-time order depth for all actively subscribed symbols across connected WebSocket clients
                if self._ws_subscriptions:
                    unique_pairs = set((sub["symbol"], sub["exchange"]) for sub in self._ws_subscriptions.values())
                    for sym, exch in unique_pairs:
                        try:
                            d = await self.fetch_market_depth(sym, exch)
                            d_msg = json.dumps({
                                "event": "depth_update",
                                "symbol": sym,
                                "exchange": exch,
                                "depth": d,
                                "timestamp": datetime.now(BST).isoformat()
                            }, default=str)
                            target_ws = [ws for ws, sub in list(self._ws_subscriptions.items()) if sub["symbol"] == sym and sub["exchange"] == exch]
                            for tw in target_ws:
                                try:
                                    await asyncio.wait_for(tw.send_text(d_msg), timeout=2.0)
                                    pass
                                except Exception:
                                    pass
                        except Exception as ex:
                            logger.debug(f"[Depth live push note] {sym}: {ex}")

                # 3. Sector heatmap refreshed every 6 cycles (~12-15s) and broadcast
                if cycle % 6 == 0:
                    async def _refresh_sectors():
                        sec = await self.fetch_sector_heatmap()
                        await self._broadcast({
                            "event": "sector_update",
                            "timestamp": datetime.now(BST).isoformat(),
                            "sector_heatmap": sec
                        })
                    asyncio.create_task(_refresh_sectors())

                # 4. News, block market & movers refreshed every 10 cycles (~20-25s) and broadcast
                if cycle % 10 == 0:
                    async def _refresh_all_intel():
                        await self.fetch_live_news()
                        bm = await self.fetch_block_market()
                        mov = await self.fetch_index_movers()
                        top = await self.fetch_top_movers()
                        await self._broadcast({
                            "event": "block_market_update",
                            "timestamp": datetime.now(BST).isoformat(),
                            "deals": bm
                        })
                        await self._broadcast({
                            "event": "movers_update",
                            "timestamp": datetime.now(BST).isoformat(),
                            "movers": mov,
                            "top_lists": top
                        })
                    asyncio.create_task(_refresh_all_intel())

                # 5. Broadcast heartbeat with precise server milliseconds and live LankaBD market status
                await self._broadcast({
                    "event": "heartbeat",
                    "timestamp": datetime.now(BST).isoformat(),
                    "server_time_ms": int(time.time() * 1000),
                    "last_scraped_at": datetime.fromtimestamp(self._last_scrape_time, tz=BST).isoformat() if self._last_scrape_time else None,
                    "is_trading_hour": live,
                    "market_status": self.get_market_status("DSE"),
                    "exchanges": self.fetch_exchanges(),
                    "cycle": cycle,
                    "active_clients": len(self._ws_clients),
                    "total_tracked": len(self._last_prices),
                    "source": "lankabd"
                })


                interval = LIVE_CYCLE_SEC if live else CLOSED_CYCLE_SEC
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.5, interval - elapsed))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[LankaBD WORKER LOOP ERROR] {e}")
                await asyncio.sleep(5.0)

        logger.info("[LankaBD ENGINE] Background worker loop stopped.")

    def start_background_worker(self):
        """Start async background ingestion loop"""
        if self._bg_task is None or self._bg_task.done():
            self._is_running = True
            self._bg_task = asyncio.create_task(self._worker_loop())
            logger.info("[LankaBD ENGINE] Background worker task launched.")

    def stop_background_worker(self):
        """Stop worker task"""
        self._is_running = False
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            self._bg_task = None
            logger.info("[LankaBD ENGINE] Background worker task cancelled.")
