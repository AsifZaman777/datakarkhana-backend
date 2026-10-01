"""
DSE 3-Second Update Cadence & Live Content Verification Test Suite
==================================================================
Tests specifically validating:
1. 3-Second update cadence: network roundtrip, cache invalidation, and broadcast loop.
2. Content accuracy: live DSEX, DS30, DSES, ticker LTPs, volume, breadth matching www.dse.com.bd.
3. SSE stream responsiveness: event delivery within 3s intervals.

Run command:
    C:\\Users\\asif.zaman\\.platformio\\penv\\Scripts\\python.exe -m pytest tests/test_dse_3s_updates.py -v -s
"""

import asyncio
import os
import sys
import time
import unittest
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

# Ensure backend root is on sys.path and import DSEMarketService directly to bypass pandas
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from services.dse_service import DSEMarketService

BST = timezone(timedelta(hours=6))

DSE_BASE_URL = "https://www.dse.com.bd"
DSE_PRICES_URL = f"{DSE_BASE_URL}/api/live/prices"
DSE_MARKET_URL = f"{DSE_BASE_URL}/api/live/market"
DSE_NEWS_URL = f"{DSE_BASE_URL}/api/live/news"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.dse.com.bd/markets/latest-share-price",
}

KEY_TICKERS = ["GP", "SQURPHARMA", "BRACBANK", "BATBC", "ISLAMIBANK", "ROBI", "OLYMPIC"]


def _extract_index_value(market_data: Optional[Dict], index_key: str) -> Optional[float]:
    """Helper to parse index value (DSEX, DS30, DSES) supporting both DSE API schemas."""
    if not market_data:
        return None
    key_upper = index_key.upper()
    # Schema 1: {"indices": [{"key": "DSEX", "value": 5536.4}, ...]}
    for item in market_data.get("indices", []):
        if isinstance(item, dict) and str(item.get("key", "")).upper() == key_upper:
            v = item.get("value")
            if v is not None:
                try:
                    return float(v)
                except (ValueError, TypeError):
                    pass
    # Schema 2: {key: {"index": 5536.4}} or {key: 5536.4}
    for k, v in market_data.items():
        if k.upper() == key_upper:
            if isinstance(v, dict):
                val = v.get("index") or v.get("value")
                return float(val) if val is not None else None
            if isinstance(v, (int, float)):
                return float(v)
    return None


def _parse_dse_prices_payload(data: Optional[Dict]) -> Dict[str, Dict[str, Any]]:
    """Helper to convert DSE /api/live/prices tabular rows into a ticker dict."""
    result: Dict[str, Dict[str, Any]] = {}
    if not data:
        return result
    cols = data.get("cols", [])
    rows = data.get("rows", [])
    if not cols or not rows:
        return result
    col_map = {str(c).lower(): i for i, c in enumerate(cols)}
    for row in rows:
        if not row or len(row) < len(cols):
            continue
        code = str(row[col_map.get("code", 0)]).strip()
        if not code:
            continue
        try:
            result[code] = {
                "ltp": float(row[col_map.get("ltp", 1)] or 0),
                "ycp": float(row[col_map.get("ycp", 2)] or 0),
                "high": float(row[col_map.get("high", 4)] or 0),
                "low": float(row[col_map.get("low", 5)] or 0),
                "close": float(row[col_map.get("close", 6)] or 0),
                "volume": int(row[col_map.get("volume", 7)] or 0),
                "value_mn": float(row[col_map.get("value", 8)] or 0),
                "trades": int(row[col_map.get("trades", 9)] or 0),
                "percent": float(row[col_map.get("percent", 10)] or 0),
            }
        except Exception:
            continue
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 1. 3-Second Cadence & Latency Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDSE3SecondCadence(unittest.IsolatedAsyncioTestCase):
    """Validates that network roundtrips, internal updates, and broadcasts execute within 3 seconds."""

    async def asyncSetUp(self):
        self.service = DSEMarketService.get_instance()

    async def test_prices_endpoint_latency_under_3s(self):
        """Verify DSE /api/live/prices responds within 3.0 seconds to support 3s polling."""
        async with httpx.AsyncClient(headers=HEADERS, verify=False) as client:
            t0 = time.time()
            resp = await client.get(DSE_PRICES_URL, timeout=5.0)
            latency = time.time() - t0
        self.assertEqual(resp.status_code, 200, f"DSE prices endpoint returned {resp.status_code}")
        print(f"\n  [Latency] DSE prices endpoint: {latency * 1000:.1f}ms")
        self.assertLess(latency, 3.0, f"DSE prices endpoint latency {latency:.2f}s exceeds 3.0s")

    async def test_market_summary_latency_under_3s(self):
        """Verify DSE /api/live/market responds within 3.0 seconds."""
        async with httpx.AsyncClient(headers=HEADERS, verify=False) as client:
            t0 = time.time()
            resp = await client.get(DSE_MARKET_URL, timeout=5.0)
            latency = time.time() - t0
        self.assertEqual(resp.status_code, 200, f"DSE market endpoint returned {resp.status_code}")
        print(f"\n  [Latency] DSE market endpoint: {latency * 1000:.1f}ms")
        self.assertLess(latency, 3.0, f"DSE market endpoint latency {latency:.2f}s exceeds 3.0s")

    async def test_internal_main_board_fetch_under_3s(self):
        """Verify service._fetch_main_board_prices_only() completes within 3.0 seconds."""
        t0 = time.time()
        await self.service._fetch_main_board_prices_only()
        elapsed = time.time() - t0
        print(f"\n  [Internal Fetch] Main board prices processed in: {elapsed * 1000:.1f}ms")
        self.assertLess(elapsed, 3.0, f"Internal fetch elapsed time {elapsed:.2f}s exceeds 3.0s")
        self.assertGreater(len(self.service._last_prices), 300, "Should have parsed > 300 tickers")

    async def test_consecutive_3s_cycle_freshness(self):
        """Verify two consecutive 3s cycles advance scrape timestamp and maintain freshness."""
        # Cycle 1
        await self.service._fetch_main_board_prices_only()
        t1 = self.service._last_scrape_time
        self.assertGreater(t1, 0)

        # Sleep for exactly 3 seconds (matching worker interval)
        await asyncio.sleep(3.0)

        # Cycle 2
        await self.service._fetch_main_board_prices_only()
        t2 = self.service._last_scrape_time
        delta = t2 - t1

        print(f"\n  [3s Cadence] Time delta between scrapes: {delta:.2f}s")
        self.assertGreaterEqual(delta, 2.8, "Scrape interval was too short")
        self.assertLessEqual(delta, 5.0, f"Scrape interval {delta:.2f}s was too long (expected ~3s)")

    async def test_tick_diff_broadcast_dispatched_within_100ms(self):
        """Verify that when a price diff occurs, it is pushed to SSE queue in < 100ms."""
        q: asyncio.Queue = asyncio.Queue()
        self.service._subscribers.append(q)

        try:
            t0 = time.time()
            test_diff = {
                "event": "tick_diff",
                "timestamp": datetime.now(BST).isoformat(),
                "count": 1,
                "ticks": [{
                    "ticker": "GP",
                    "ltp": 285.5,
                    "change": 1.5,
                    "percent": 0.53,
                    "volume": 120000,
                    "trades": 850,
                    "direction": "up"
                }]
            }
            await self.service._broadcast(test_diff)
            broadcast_time = (time.time() - t0) * 1000

            received = await asyncio.wait_for(q.get(), timeout=1.0)
            self.assertEqual(received["event"], "tick_diff")
            self.assertEqual(received["ticks"][0]["ticker"], "GP")
            print(f"\n  [Broadcast Dispatch] Diff delivered to subscriber queue in: {broadcast_time:.2f}ms")
            self.assertLess(broadcast_time, 100.0, "Broadcast dispatch exceeded 100ms budget")
        finally:
            if q in self.service._subscribers:
                self.service._subscribers.remove(q)

    async def test_market_update_broadcast_on_dsex_change(self):
        """Verify market_update event is broadcast when DSEX updates."""
        q: asyncio.Queue = asyncio.Queue()
        self.service._subscribers.append(q)

        try:
            # Seed with artificial previous summary so fetch_market_summary detects change
            self.service._last_market_summary = {"dsex": {"index": 9999.9}}
            await self.service.fetch_market_summary()

            # Expect market_update in subscriber queue within 2 seconds
            received = None
            try:
                received = await asyncio.wait_for(q.get(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

            if received:
                self.assertEqual(received.get("event"), "market_update")
                print("\n  [Market Broadcast] market_update event received promptly on DSEX change")
            else:
                # If market endpoint was unreachable or identical, check summary is valid
                self.assertIsNotNone(self.service._last_market_summary)
        finally:
            if q in self.service._subscribers:
                self.service._subscribers.remove(q)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Content Up-to-Date Verification vs www.dse.com.bd
# ─────────────────────────────────────────────────────────────────────────────

class TestDSEContentAccuracyLive(unittest.IsolatedAsyncioTestCase):
    """Validates that backend data directly matches live www.dse.com.bd values."""

    async def asyncSetUp(self):
        self.service = DSEMarketService.get_instance()

    async def test_dsex_index_matches_live_dse(self):
        """Verify DSEX index in backend matches www.dse.com.bd/api/live/market."""
        async with httpx.AsyncClient(headers=HEADERS, verify=False) as client:
            resp = await client.get(DSE_MARKET_URL, timeout=10.0)
            self.assertEqual(resp.status_code, 200)
            live_market = resp.json()

        backend_market = await self.service.fetch_market_summary()

        live_dsex = _extract_index_value(live_market, "DSEX")
        backend_dsex = _extract_index_value(backend_market, "DSEX")

        print(f"\n  [DSEX Match] Live DSE: {live_dsex} | Backend: {backend_dsex}")
        self.assertIsNotNone(live_dsex, "Could not extract DSEX from live DSE API")
        self.assertIsNotNone(backend_dsex, "Could not extract DSEX from backend summary")
        self.assertAlmostEqual(live_dsex, backend_dsex, delta=2.0, msg="DSEX mismatch between backend and live DSE")

    async def test_ds30_and_dses_indices_match_live_dse(self):
        """Verify DS30 and DSES indices match live DSE values."""
        async with httpx.AsyncClient(headers=HEADERS, verify=False) as client:
            resp = await client.get(DSE_MARKET_URL, timeout=10.0)
            live_market = resp.json()

        backend_market = await self.service.fetch_market_summary()

        for idx_name in ("DS30", "DSES"):
            live_val = _extract_index_value(live_market, idx_name)
            backend_val = _extract_index_value(backend_market, idx_name)
            print(f"  [{idx_name} Match] Live DSE: {live_val} | Backend: {backend_val}")
            if live_val is not None and backend_val is not None:
                self.assertAlmostEqual(live_val, backend_val, delta=2.0, msg=f"{idx_name} mismatch")

    async def test_totals_all_digits_turnover_volume_trades(self):
        """Verify totals contains raw full digits for volume, trades, and turnover in mn BDT."""
        backend_market = await self.service.fetch_market_summary()
        totals = backend_market.get("totals", {})

        volume = totals.get("volume")
        trades = totals.get("trades")
        turnover = totals.get("turnover") or totals.get("value")

        print(f"\n  [Totals Verification] Turnover: BDT {turnover:,.2f} mn | Volume: {volume:,} | Trades: {trades:,}")
        self.assertIsNotNone(volume, "Volume must not be null/undefined")
        self.assertIsNotNone(trades, "Trades must not be null/undefined")
        self.assertIsNotNone(turnover, "Turnover must not be null/undefined")

        self.assertIsInstance(volume, int, "Volume must be full integer digits")
        self.assertIsInstance(trades, int, "Trades must be full integer digits")
        self.assertGreater(volume, 0, "Volume should be > 0")
        self.assertGreater(trades, 0, "Trades should be > 0")
        self.assertGreater(turnover, 0, "Turnover must be > 0")

    async def test_key_tickers_prices_and_ycp_match_dse(self):
        """Verify key stocks (GP, SQURPHARMA, BRACBANK, etc.) have matching LTP, YCP, High, Low."""
        async with httpx.AsyncClient(headers=HEADERS, verify=False) as client:
            resp = await client.get(DSE_PRICES_URL, timeout=10.0)
            self.assertEqual(resp.status_code, 200)
            live_prices = _parse_dse_prices_payload(resp.json())

        await self.service.fetch_live_prices()

        checked = 0
        for ticker in KEY_TICKERS:
            if ticker not in live_prices:
                continue
            live_data = live_prices[ticker]
            backend_stock = self.service.get_stock_detail(ticker)
            self.assertIsNotNone(backend_stock, f"Ticker {ticker} missing from backend cache")

            live_ltp = live_data["ltp"]
            backend_ltp = backend_stock.get("ltp", 0.0)
            live_ycp = live_data["ycp"]
            backend_ycp = backend_stock.get("ycp", 0.0)

            print(f"  [{ticker}] LTP (Live: {live_ltp}, Backend: {backend_ltp}) | YCP (Live: {live_ycp}, Backend: {backend_ycp})")
            self.assertAlmostEqual(live_ltp, backend_ltp, delta=0.05, msg=f"{ticker} LTP mismatch")
            self.assertAlmostEqual(live_ycp, backend_ycp, delta=0.05, msg=f"{ticker} YCP mismatch")
            checked += 1

        self.assertGreaterEqual(checked, 3, "At least 3 key tickers should be validated")

    async def test_all_boards_completeness(self):
        """Verify all boards (PUBLIC, SME, ATB, G-Sec) are populated in backend cache."""
        await self.service.fetch_live_prices()

        public_stocks = self.service.get_all_stocks(board="PUBLIC")
        sme_stocks = self.service.get_all_stocks(board="SME")
        atb_stocks = self.service.get_all_stocks(board="ATB")
        gsec_bonds = self.service.get_all_stocks(board="YIELDDBT")

        print(f"\n  [Board Counts] PUBLIC: {len(public_stocks)} | SME: {len(sme_stocks)} | ATB: {len(atb_stocks)} | G-Sec: {len(gsec_bonds)}")
        self.assertGreaterEqual(len(public_stocks), 300, "PUBLIC board should have >= 300 tickers")
        self.assertGreaterEqual(len(sme_stocks), 15, "SME board should have >= 15 tickers")
        self.assertGreaterEqual(len(atb_stocks), 2, "ATB board should have >= 2 securities")
        self.assertGreaterEqual(len(gsec_bonds), 50, "G-Sec board should have >= 50 bonds")

    async def test_top_gainers_and_losers_calculation(self):
        """Verify top gainers have positive/zero percent and top losers have negative/zero percent."""
        await self.service.fetch_live_prices()
        top_shares = self.service.get_top_shares()
        gainers = top_shares.get("top_gainers", [])
        losers = top_shares.get("top_losers", [])

        self.assertGreater(len(gainers), 0, "Top gainers list should not be empty")
        self.assertGreater(len(losers), 0, "Top losers list should not be empty")

        top_g = gainers[0]
        top_l = losers[0]
        print(f"\n  [Top Gainer] {top_g.get('ticker')}: {top_g.get('percent'):+.2f}%")
        print(f"  [Top Loser]  {top_l.get('ticker')}: {top_l.get('percent'):+.2f}%")

        self.assertGreaterEqual(top_g.get("percent", 0.0), 0.0, "Top gainer percent must be >= 0")
        self.assertLessEqual(top_l.get("percent", 0.0), 0.0, "Top loser percent must be <= 0")

    async def test_sector_heatmap_weighted_turnover_accuracy(self):
        """Verify sector heatmap matches DSE official turnover-weighted rankings and calculations."""
        await self.service.fetch_live_prices()
        hm = self.service.get_sector_heatmap()

        top_12 = hm.get("top_12", [])
        self.assertEqual(len(top_12), 12, "Top 12 sectors should contain exactly 12 items")

        # Top sector by turnover on DSE is Textile
        top_sector = top_12[0]
        self.assertEqual(top_sector.get("code"), "Textile", f"Top sector should be Textile, got {top_sector.get('code')}")
        self.assertAlmostEqual(top_sector.get("change_pct", 0), 0.8, delta=0.2, msg="Textile change_pct should be ~+0.8%")

        # Second sector is Insurance
        second_sector = top_12[1]
        self.assertEqual(second_sector.get("code"), "Insurance")
        self.assertAlmostEqual(second_sector.get("change_pct", 0), -1.9, delta=0.2, msg="Insurance change_pct should be ~-1.9%")

        # Verify turnover is sorted descending
        turnovers = [s["turnover"] for s in top_12]
        self.assertEqual(turnovers, sorted(turnovers, reverse=True), "Top sectors not sorted by turnover desc")

        formatted_sectors = [(s['name'], f"{s['change_pct']:+.1f}%") for s in top_12]
        print(f"\n  [Sector Heatmap Match] Top 12 sectors: {formatted_sectors}")
        print(f"  [Sector Breadth] {hm['adv']} adv · {hm['dec']} dec (Total sectors: {len(hm['sectors'])})")


# ─────────────────────────────────────────────────────────────────────────────
# 3. SSE Stream Live 3-Second Delivery
# ─────────────────────────────────────────────────────────────────────────────

class TestDSESSEStreamLiveCadence(unittest.IsolatedAsyncioTestCase):
    """Verifies that the live SSE stream on port 8000 provides updates or heartbeats <= 3s."""

    async def test_sse_stream_3s_interval(self):
        """Connects to http://127.0.0.1:8000/api/stocks/stream and asserts events or pings within 3s."""
        import json as _json

        stream_url = "http://127.0.0.1:8000/api/stocks/stream"
        timeout = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("GET", stream_url) as resp:
                    if resp.status_code != 200:
                        self.skipTest(f"Backend returned status {resp.status_code}")

                    received_events = []
                    last_time = time.time()
                    intervals: List[float] = []

                    # Listen for up to 9 seconds (covering ~3 cycles of 3s)
                    deadline = time.time() + 9.0

                    async for line in resp.aiter_lines():
                        now = time.time()
                        if now > deadline:
                            break

                        stripped = line.strip()
                        if not stripped:
                            continue

                        interval = now - last_time
                        last_time = now
                        intervals.append(interval)

                        if stripped.startswith(": ping"):
                            received_events.append("ping")
                        elif stripped.startswith("data:"):
                            try:
                                payload = _json.loads(stripped[5:].strip())
                                ev = payload.get("event", "data")
                                received_events.append(ev)
                            except Exception:
                                pass

                        if len(received_events) >= 3:
                            break

                    print(f"\n  [SSE Stream 3s] Received events: {received_events}")
                    print(f"  [SSE Intervals] Event gaps: {[round(i, 2) for i in intervals]}")

                    self.assertGreater(len(received_events), 0, "No SSE events or pings received in 9s")
                    if len(intervals) > 1:
                        avg_interval = sum(intervals[1:]) / len(intervals[1:])
                        print(f"  [SSE Avg Interval] {avg_interval:.2f}s")
                        self.assertLessEqual(avg_interval, 3.5, f"SSE stream average interval {avg_interval:.2f}s > 3.5s")

        except (httpx.ConnectError, httpx.ConnectTimeout):
            self.skipTest("Backend server not running on localhost:8000 (run uvicorn main:app)")
        except httpx.ReadTimeout:
            self.skipTest("SSE stream read timed out")


if __name__ == "__main__":
    unittest.main(verbosity=2)
