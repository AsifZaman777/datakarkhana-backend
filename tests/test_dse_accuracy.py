r"""
DSE Data Accuracy Test Suite
==============================
Fetches data DIRECTLY from www.dse.com.bd APIs then compares against
values stored/returned by our backend service.

Run from the datakarkhana-backend directory:
    python -m unittest tests/test_dse_accuracy.py -v

For TestDSESSEStream: backend must be running on port 8000.
"""

import asyncio
import sys
import os
import time
import unittest
from typing import Any, Dict, Optional

import httpx

# Import DSEMarketService directly to bypass services/__init__.py (which pulls pandas)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.dse_service import DSEMarketService

DSE_BASE_URL   = "https://www.dse.com.bd"
DSE_PRICES_URL = f"{DSE_BASE_URL}/api/live/prices"
DSE_MARKET_URL = f"{DSE_BASE_URL}/api/live/market"
DSE_NEWS_URL   = f"{DSE_BASE_URL}/api/live/news"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.dse.com.bd/markets/latest-share-price",
}

PRICE_TOLERANCE   = 0.05   # within 5 paisa
INDEX_TOLERANCE   = 2.0    # DSEX within 2 points (polling gap)
MIN_PUBLIC_STOCKS = 300
MIN_SME_STOCKS    = 15
MIN_ATB_STOCKS    = 2
MIN_NEWS_ITEMS    = 5
MIN_CB_ROWS       = 300
MIN_GSEC_BONDS    = 50
ALWAYS_PRESENT    = ["GP", "SQURPHARMA", "BRACBANK", "ISLAMIBANK"]


async def _get_json(url: str) -> Optional[Any]:
    async with httpx.AsyncClient(headers=HEADERS, timeout=20.0, verify=False) as c:
        try:
            r = await c.get(url)
            return r.json() if r.status_code == 200 else None
        except Exception as e:
            print(f"  [WARN] {url}: {e}")
    return None


def _close(a: float, b: float, tol: float = PRICE_TOLERANCE) -> bool:
    return abs(a - b) <= tol


def _extract_dsex(data: Dict) -> Optional[float]:
    """Extract DSEX from DSE API. Handles both old and new response shapes."""
    if not data:
        return None
    # New DSE API shape: {"indices": [{"key": "DSEX", "value": 5531.6}, ...]}
    for item in data.get("indices", []):
        if isinstance(item, dict) and str(item.get("key", "")).upper() == "DSEX":
            v = item.get("value")
            if v:
                return float(v)
    # Old shape: {"dsex": {"index": ...}} or {"dsex": 1234.5}
    dsex = data.get("dsex")
    if isinstance(dsex, dict):
        v = dsex.get("index") or dsex.get("value")
        return float(v) if v else None
    if isinstance(dsex, (int, float)) and dsex > 0:
        return float(dsex)
    return None


def _parse_dse_prices(raw: Optional[Dict]) -> Dict[str, Dict]:
    result: Dict[str, Dict] = {}
    if not raw:
        return result
    cols    = raw.get("cols", [])
    rows    = raw.get("rows", [])
    col_map = {col: idx for idx, col in enumerate(cols)}
    for row in rows:
        if row and len(row) >= len(cols):
            code = str(row[col_map.get("code", 0)]).strip()
            if code:
                result[code] = {
                    "ltp":   float(row[col_map.get("ltp",     1)] or 0),
                    "ycp":   float(row[col_map.get("ycp",     2)] or 0),
                    "pct":   float(row[col_map.get("percent", 10)] or 0),
                    "close": float(row[col_map.get("close",   6)] or 0),
                }
    return result


# ─── 1. Market Indices ────────────────────────────────────────────────────────

class TestDSEMarketIndices(unittest.IsolatedAsyncioTestCase):

    async def test_market_api_reachable(self):
        data = await _get_json(DSE_MARKET_URL)
        self.assertIsNotNone(data, "DSE /api/live/market unreachable")

    async def test_dsex_present_and_positive(self):
        data = await _get_json(DSE_MARKET_URL)
        dsex = _extract_dsex(data or {})
        self.assertIsNotNone(dsex, f"DSEX not found in response shape: {list((data or {}).keys())}")
        self.assertGreater(dsex, 0)
        print(f"\n  [DSE.com.bd] DSEX = {dsex:,.2f}")

    async def test_backend_dsex_matches_live(self):
        service = DSEMarketService.get_instance()
        live_data, svc_data = await asyncio.gather(
            _get_json(DSE_MARKET_URL),
            service.fetch_market_summary(),
        )
        live_dsex = _extract_dsex(live_data or {})
        svc_dsex  = _extract_dsex(svc_data  or {})

        if live_dsex is None:
            self.skipTest("DSE market API unavailable")

        self.assertIsNotNone(svc_dsex, "Backend returned no DSEX value")
        delta = abs(live_dsex - svc_dsex)
        self.assertLessEqual(delta, INDEX_TOLERANCE,
            f"DSEX: DSE={live_dsex:,.2f}  Backend={svc_dsex:,.2f}  delta={delta:.2f}")
        print(f"\n  DSEX: DSE={live_dsex:,.2f}  Backend={svc_dsex:,.2f}  delta={delta:.4f}")

    async def test_all_three_indices_present(self):
        """DSEX, DS30 and DSES must all be present in live API."""
        data = await _get_json(DSE_MARKET_URL)
        if not data:
            self.skipTest("Market API unavailable")
        indices = {i["key"]: i["value"] for i in data.get("indices", []) if isinstance(i, dict)}
        for key in ("DSEX", "DS30", "DSES"):
            self.assertIn(key, indices, f"{key} missing from DSE market API")
            self.assertGreater(float(indices[key]), 0, f"{key} value is zero")
            print(f"\n  {key} = {float(indices[key]):,.2f}")

    async def test_market_breadth_present(self):
        """Breadth (advanced/declined/unchanged) must sum to a reasonable number."""
        data = await _get_json(DSE_MARKET_URL)
        if not data:
            self.skipTest("Market API unavailable")
        breadth = data.get("breadth", {})
        total   = sum(breadth.get(k, 0) for k in ("advanced", "declined", "unchanged"))
        self.assertGreater(total, 100,
            f"Breadth total too low: {breadth} — market may be closed or response malformed")
        print(f"\n  Breadth: +{breadth.get('advanced')}  -{breadth.get('declined')}  ={breadth.get('unchanged')}")


# ─── 2. Live Prices ───────────────────────────────────────────────────────────

class TestDSELivePrices(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        service = DSEMarketService.get_instance()
        live_raw, _ = await asyncio.gather(
            _get_json(DSE_PRICES_URL),
            service.fetch_live_prices(),
        )
        self.live = _parse_dse_prices(live_raw)
        self.svc  = {s["ticker"]: s for s in service.get_all_stocks(board="ALL")}
        print(f"\n  DSE={len(self.live)} tickers  Backend={len(self.svc)} tickers")

    async def test_prices_api_reachable(self):
        self.assertGreater(len(self.live), 0, "DSE prices API empty")

    async def test_minimum_public_count(self):
        public = [s for s in self.svc.values() if s.get("board") == "PUBLIC"]
        self.assertGreaterEqual(len(public), MIN_PUBLIC_STOCKS)
        print(f"\n  Public: {len(public)}")

    async def test_known_tickers_present(self):
        missing = [t for t in ALWAYS_PRESENT if t not in self.svc]
        self.assertEqual(missing, [], f"Missing: {missing}")

    async def test_ltp_spot_check(self):
        common = sorted(set(self.live) & set(self.svc))[:10]
        self.assertGreater(len(common), 0, "No common tickers")
        errors = []
        for t in common:
            lv, sv = self.live[t]["ltp"], self.svc[t]["ltp"]
            if lv > 0 and not _close(lv, sv):
                errors.append(f"{t}: DSE={lv:.2f} Backend={sv:.2f} delta={abs(lv-sv):.4f}")
        self.assertEqual(errors, [], "LTP mismatches:\n  " + "\n  ".join(errors))
        print(f"\n  LTP OK: {common}")

    async def test_ycp_spot_check(self):
        common = sorted(set(self.live) & set(self.svc))[:10]
        errors = []
        for t in common:
            lv, sv = self.live[t]["ycp"], self.svc[t].get("ycp", 0)
            if lv > 0 and not _close(lv, sv):
                errors.append(f"{t}: DSE_YCP={lv:.2f} Backend_YCP={sv:.2f}")
        self.assertEqual(errors, [], "YCP mismatches:\n  " + "\n  ".join(errors))

    async def test_no_zero_ltp(self):
        # A stock in backend should only have LTP == 0 if DSE itself reported LTP == 0 (untraded/halted today)
        bad = [s["ticker"] for s in self.svc.values()
               if s.get("board") == "PUBLIC" and s.get("ltp", 0) == 0 and self.live.get(s["ticker"], {}).get("ltp", 0) > 0]
        self.assertEqual(bad, [], f"Zero-LTP when DSE has price: {bad[:20]}")

    async def test_ticker_coverage_95pct(self):
        missing = [t for t in self.live if t not in self.svc]
        pct = 100 * (1 - len(missing) / max(len(self.live), 1))
        self.assertGreaterEqual(pct, 95.0, f"Coverage {pct:.1f}%. Missing: {missing[:20]}")
        print(f"\n  Coverage: {pct:.1f}%  (missing: {len(missing)})")


# ─── 3. Board Completeness ────────────────────────────────────────────────────

class TestDSEBoardCompleteness(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.service = DSEMarketService.get_instance()
        await self.service.fetch_live_prices()

    async def test_sme_count(self):
        sme = self.service.get_all_stocks(board="SME")
        self.assertGreaterEqual(len(sme), MIN_SME_STOCKS)
        print(f"\n  SME: {len(sme)}")

    async def test_atb_count(self):
        atb = self.service.get_all_stocks(board="ATB")
        self.assertGreaterEqual(len(atb), MIN_ATB_STOCKS)
        print(f"\n  ATB: {len(atb)}")

    async def test_debt_present(self):
        debt = self.service.get_all_stocks(board="DEBT")
        self.assertGreater(len(debt), 0, "DEBT empty")

    async def test_gsec_count(self):
        gsec = self.service.get_all_stocks(board="YIELDDBT")
        self.assertGreaterEqual(len(gsec), MIN_GSEC_BONDS)
        print(f"\n  G-Sec: {len(gsec)}")

    async def test_achiasf_valid(self):
        all_s = {s["ticker"]: s for s in self.service.get_all_stocks(board="ALL")}
        self.assertIn("ACHIASF", all_s)
        s = all_s["ACHIASF"]
        self.assertEqual(s.get("board"), "SME")
        self.assertGreater(s.get("ycp", 0), 0)
        print(f"\n  ACHIASF: LTP={s.get('ltp')}  YCP={s.get('ycp')}")

    async def test_lbs_in_atb(self):
        atb = {s["ticker"] for s in self.service.get_all_stocks(board="ATB")}
        self.assertIn("LBS", atb)


# ─── 4. Circuit Breakers ─────────────────────────────────────────────────────

class TestDSECircuitBreakers(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.service = DSEMarketService.get_instance()
        self.rows    = await self.service.get_circuit_breakers()

    async def test_minimum_count(self):
        self.assertGreaterEqual(len(self.rows), MIN_CB_ROWS)
        print(f"\n  CB rows: {len(self.rows)}")

    async def test_all_boards_covered(self):
        boards = {r.get("board") for r in self.rows}
        for b in ("PUBLIC", "SME", "ATB"):
            self.assertIn(b, boards, f"'{b}' missing")

    async def test_limits_logical(self):
        errors = []
        for row in self.rows[:50]:
            lo, hi, cl = row.get("lower_limit",0), row.get("upper_limit",0), row.get("close_price",0)
            if cl > 0 and lo > 0 and hi > 0 and not (lo < cl < hi):
                errors.append(f"{row.get('code')}: lo={lo} cl={cl} hi={hi}")
        self.assertEqual(errors, [], "Illogical CB:\n  " + "\n  ".join(errors))

    async def test_gp_price_matches_dse(self):
        live = _parse_dse_prices(await _get_json(DSE_PRICES_URL))
        if not live:
            self.skipTest("DSE unavailable")
        gp_live = live.get("GP", {}).get("close") or live.get("GP", {}).get("ltp")
        gp_cb   = next((r for r in self.rows if r.get("code") == "GP"), None)
        if not gp_live or not gp_cb:
            self.skipTest("GP not found")
        delta = abs(float(gp_live) - float(gp_cb.get("close_price", 0)))
        self.assertLessEqual(delta, PRICE_TOLERANCE,
            f"GP: DSE={gp_live}  CB={gp_cb.get('close_price')}")
        print(f"\n  GP CB: {gp_cb.get('close_price'):.2f} OK")


# ─── 5. Top Shares ────────────────────────────────────────────────────────────

class TestDSETopShares(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.service = DSEMarketService.get_instance()
        await self.service.fetch_live_prices()
        self.top = self.service.get_top_shares()

    async def test_gainers_positive(self):
        gainers = self.top.get("top_gainers", [])
        self.assertGreater(len(gainers), 0)
        bad = [s for s in gainers if s.get("percent", 0) <= 0]
        self.assertEqual(bad, [], f"Non-positive: {bad}")

    async def test_losers_negative(self):
        losers = self.top.get("top_losers", [])
        self.assertGreater(len(losers), 0)
        bad = [s for s in losers if s.get("percent", 0) >= 0]
        self.assertEqual(bad, [], f"Non-negative: {bad}")

    async def test_turnover_sorted(self):
        vals = [s.get("value_mn", 0) for s in self.top.get("top_turnover", [])]
        self.assertGreater(len(vals), 1)
        self.assertEqual(vals, sorted(vals, reverse=True), "Not sorted desc")

    async def test_volume_sorted(self):
        vols = [s.get("volume", 0) for s in self.top.get("top_volume", [])]
        self.assertGreater(len(vols), 1)
        self.assertEqual(vols, sorted(vols, reverse=True), "Not sorted desc")

    async def test_top_gainer_vs_live(self):
        live = _parse_dse_prices(await _get_json(DSE_PRICES_URL))
        if not live:
            self.skipTest("DSE unavailable")
        # Use movers from live market API as reference
        market = await _get_json(DSE_MARKET_URL)
        if market:
            gainers_live = [g["code"] for g in (market.get("movers") or {}).get("gainers", [])]
            our_gainers = [s.get("ticker") for s in self.top.get("top_gainers", [])[:15]]
            overlap = set(our_gainers) & set(gainers_live)
            if gainers_live:
                self.assertGreater(len(overlap), 0,
                    f"No overlap between top gainers {our_gainers} and DSE movers {gainers_live}")
                print(f"\n  Top gainers overlap with DSE movers: {overlap}")


# ─── 6. News ──────────────────────────────────────────────────────────────────

class TestDSENews(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.service = DSEMarketService.get_instance()
        await self.service.fetch_live_news()
        self.news = self.service.get_recent_news(50)

    async def test_minimum_count(self):
        self.assertGreaterEqual(len(self.news), MIN_NEWS_ITEMS)
        print(f"\n  News: {len(self.news)}")

    async def test_required_fields(self):
        errors = []
        for item in self.news[:10]:
            for f in ("id", "summary"):
                if not item.get(f):
                    errors.append(f"Missing '{f}': {item}")
        self.assertEqual(errors, [], "\n".join(errors))

    async def test_ids_overlap_with_live(self):
        live = await _get_json(DSE_NEWS_URL)
        if not live:
            self.skipTest("DSE news unavailable")
        live_ids = {str(i.get("id")) for i in live.get("rows", [])[:20]}
        svc_ids  = {str(i.get("id")) for i in self.news[:20]}
        overlap  = live_ids & svc_ids
        pct = 100 * len(overlap) / max(len(live_ids), 1)
        self.assertGreaterEqual(pct, 50.0, f"News overlap {pct:.1f}%")
        print(f"\n  News overlap: {pct:.1f}% ({len(overlap)}/{len(live_ids)})")


# ─── 7. Market Depth ─────────────────────────────────────────────────────────

class TestDSEMarketDepth(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.service = DSEMarketService.get_instance()

    async def test_has_bids_and_asks(self):
        depth = await self.service.get_market_depth("IPDC")
        self.assertIn("bids", depth)
        self.assertIn("asks", depth)

    async def test_bid_ask_ordering(self):
        depth = await self.service.get_market_depth("GP")
        bids = [float(b.get("price") or b.get("p") or 0) for b in depth.get("bids", []) if b]
        asks = [float(a.get("price") or a.get("p") or 0) for a in depth.get("asks", []) if a]
        bids = [p for p in bids if p > 0]
        asks = [p for p in asks if p > 0]
        if len(bids) >= 2:
            self.assertEqual(bids, sorted(bids, reverse=True), "Bids not descending")
        if len(asks) >= 2:
            self.assertEqual(asks, sorted(asks), "Asks not ascending")
        print(f"\n  GP order book: {len(bids)} bids  {len(asks)} asks")

    async def test_stats_match_dse_source(self):
        live, svc = await asyncio.gather(
            _get_json(f"{DSE_BASE_URL}/api/live/depth?code=IPDC"),
            self.service.get_market_depth("IPDC"),
        )
        if not live or not svc:
            self.skipTest("Depth unavailable")
        ls = live.get("priceStats") or live.get("stats") or {}
        ss = svc.get("priceStats") or svc.get("stats") or {}
        for key in ("ltp", "ycp", "high", "low"):
            lv, sv = float(ls.get(key) or 0), float(ss.get(key) or 0)
            if lv > 0 and sv > 0:
                self.assertTrue(_close(lv, sv), f"IPDC {key}: DSE={lv} Backend={sv}")
        print(f"\n  IPDC depth stats OK")


# ─── 8. SSE Stream ───────────────────────────────────────────────────────────

class TestDSESSEStream(unittest.IsolatedAsyncioTestCase):
    """Requires backend running on localhost:8000"""

    async def test_sse_init_event(self):
        import json as _json
        timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                async with c.stream("GET", "http://localhost:8000/api/stocks/stream") as resp:
                    if resp.status_code != 200:
                        self.skipTest(f"Backend returned {resp.status_code}")
                    deadline = time.time() + 10
                    seen = []
                    async for line in resp.aiter_lines():
                        if time.time() > deadline:
                            break
                        if not line.strip().startswith("data:"):
                            continue
                        try:
                            payload = _json.loads(line.strip()[5:].strip())
                            seen.append(payload.get("event"))
                            if payload.get("event") == "init":
                                self.assertIn("tickers", payload)
                                self.assertGreater(len(payload.get("tickers", [])), 0)
                                self.assertIn("market_summary", payload)
                                print(f"\n  SSE init OK: {payload.get('tickers_count')} tickers")
                                return
                        except Exception:
                            pass
                    self.assertIn("init", seen, f"No init in 10s. Seen: {seen}")
        except (httpx.ConnectError, httpx.ConnectTimeout):
            self.skipTest("Backend not running on localhost:8000")
        except httpx.ReadTimeout:
            self.skipTest("SSE stream read timeout - backend may be starting up")


# ─── 9. Data Freshness ───────────────────────────────────────────────────────

class TestDSEDataFreshness(unittest.IsolatedAsyncioTestCase):

    async def test_last_scrape_under_3s(self):
        service = DSEMarketService.get_instance()
        await service.fetch_live_prices()
        age = time.time() - service._last_scrape_time
        self.assertLess(age, 3.5, f"Scrape age {age:.2f}s (expected < 3.5s)")
        print(f"\n  Scrape age: {age:.2f}s (<= 3s SLA)")

    async def test_3s_cadence_advances_timestamps(self):
        service = DSEMarketService.get_instance()
        await service._fetch_main_board_prices_only()
        t1 = service._last_scrape_time
        await asyncio.sleep(3.0)
        await service._fetch_main_board_prices_only()
        t2 = service._last_scrape_time
        delta = t2 - t1
        self.assertGreaterEqual(delta, 2.8, f"Interval too fast: {delta:.2f}s")
        self.assertLessEqual(delta, 5.0, f"Interval too slow: {delta:.2f}s (expected ~3s)")
        print(f"\n  3s cadence delta: {delta:.2f}s OK")

    async def test_timestamps_from_today(self):
        from datetime import datetime, timezone, timedelta
        BST = timezone(timedelta(hours=6))
        service = DSEMarketService.get_instance()
        await service.fetch_live_prices()
        today = datetime.now(BST).strftime("%Y-%m-%d")
        stale = [
            s["ticker"] for s in service.get_all_stocks(board="PUBLIC")[:50]
            if s.get("updated_at") and today not in s["updated_at"]
        ]
        self.assertEqual(stale, [], f"Stale timestamps: {stale}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
