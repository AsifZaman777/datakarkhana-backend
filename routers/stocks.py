import logging
import time
from typing import Optional, List
from fastapi import APIRouter, Query, HTTPException, BackgroundTasks, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from services.dse_service import DSEMarketService
from services.stock_alert_service import StockAlertService

logger = logging.getLogger("stocks_router")
router = APIRouter(prefix="/api/stocks", tags=["Stock Market Live"])

class CreateAlertRequest(BaseModel):
    whatsapp_number: str
    ticker: str
    alert_type: str # 'PRICE_BELOW', 'PRICE_ABOVE', 'PERCENT_SPIKE', 'PERCENT_DROP', 'CIRCUIT_LIMIT', 'NEWS_DISCLOSURE'
    threshold_value: Optional[float] = 0.0
    is_one_shot: Optional[bool] = True
    user_id: Optional[int] = None

class TestPingRequest(BaseModel):
    whatsapp_number: str

@router.get("/summary")
async def get_market_summary():
    """Get real-time market indices (DSEX, DS30, DSES), turnover, and market breadth"""
    service = DSEMarketService.get_instance()
    return service.get_market_summary()

@router.get("/boards")
async def get_all_boards():
    """Get all trading boards in DSE (Public, SME, ATB, Corporate Debt, Govt Treasury Bonds) with instrument counts"""
    service = DSEMarketService.get_instance()
    return service.get_boards_summary()

@router.get("/all")
async def get_all_stocks(
    search: Optional[str] = Query(None, description="Search ticker symbol e.g. SQURPHARMA, ACHIASF, LBS"),
    sector: Optional[str] = Query(None, description="Filter by sector e.g. Pharmaceuticals, Bank, Food & Allied"),
    category: Optional[str] = Query(None, description="Filter by category e.g. A, B, Z, SME, ATB, G-SEC"),
    board: Optional[str] = Query(None, description="Filter by board: PUBLIC, SME, ATB, DEBT, YIELDDBT (or ALL)"),
    sort_by: str = Query("turnover", description="Sort by 'turnover', 'percent', 'ltp', 'volume', 'code'"),
    sort_order: str = Query("desc", description="'asc' or 'desc'")
):
    """Get all tracked stock prices with real-time quotes and board filters"""
    service = DSEMarketService.get_instance()
    stocks = service.get_all_stocks(
        search=search,
        sector=sector,
        category=category,
        board=board,
        sort_by=sort_by,
        sort_order=sort_order
    )
    return {
        "count": len(stocks),
        "stocks": stocks
    }

@router.get("/sectors")
@router.get("/sector-heatmap")
async def get_sector_heatmap():
    """Get DSE official Sector Heatmap scraped directly from www.dse.com.bd"""
    service = DSEMarketService.get_instance()
    if not service._sector_heatmap_cache or (time.time() - service._sector_heatmap_cache_ts) >= 60:
        await service.fetch_sector_heatmap()
    return service.get_sector_heatmap()

@router.get("/depth")
async def get_market_depth(
    code: Optional[str] = Query(None, description="Stock instrument code e.g. IPDC, GP"),
    ticker: Optional[str] = Query(None, description="Alternative ticker param")
):
    """Get 10-level live order book (bids/asks, queues, priceStats)"""
    target = code or ticker or "IPDC"
    service = DSEMarketService.get_instance()
    return await service.get_market_depth(target)

@router.get("/depth/instruments")
async def get_depth_instruments():
    """Get list of instruments available for market depth"""
    service = DSEMarketService.get_instance()
    return await service.get_depth_instruments()

@router.get("/top-shares")
async def get_top_shares():
    """Get Top 20 by Turnover, Gainers, Losers, and Volume"""
    service = DSEMarketService.get_instance()
    return service.get_top_shares()

@router.get("/circuit-breakers")
async def get_circuit_breakers():
    """Get daily circuit breaker price limits (ceiling, floor, tick size)"""
    service = DSEMarketService.get_instance()
    rows = await service.get_circuit_breakers()
    return {
        "count": len(rows),
        "circuit_breakers": rows
    }

@router.get("/recent-market-info")
async def get_recent_market_info(
    from_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    to_date: Optional[str] = Query(None, description="YYYY-MM-DD")
):
    """Get daily historical market statistics (turnover, DSEX, marketCap, volume)"""
    service = DSEMarketService.get_instance()
    return await service.get_recent_market_info(from_date, to_date)

@router.get("/pe")
async def get_pe_at_a_glance():
    """Get Price to Earnings (P/E) ratios for all traded companies"""
    service = DSEMarketService.get_instance()
    rows = await service.get_pe_at_a_glance()
    return {
        "count": len(rows),
        "pe_data": rows
    }

@router.get("/at-a-glance")
async def get_at_a_glance():
    """Get Market at a Glance multi-year comparison stats"""
    service = DSEMarketService.get_instance()
    rows = await service.get_at_a_glance()
    return {
        "count": len(rows),
        "at_a_glance": rows
    }

@router.get("/detail/{ticker}")
async def get_stock_detail(ticker: str):
    """Get live details and intraday tick points for TradingView chart canvas"""
    service = DSEMarketService.get_instance()
    detail = service.get_stock_detail(ticker)
    if not detail:
        raise HTTPException(status_code=404, detail=f"Stock ticker '{ticker}' not found.")
    return detail

@router.get("/news")
async def get_stock_news(
    ticker: Optional[str] = Query(None, description="Filter news by ticker symbol"),
    limit: int = Query(50, ge=1, le=100)
):
    """Get latest corporate announcements from DSE"""
    service = DSEMarketService.get_instance()
    news = service.get_recent_news(limit=limit)
    if ticker:
        t_clean = ticker.upper().strip()
        news = [n for n in news if str(n.get("code", "")).upper() == t_clean]
    return {
        "count": len(news),
        "news": news
    }

@router.websocket("/ws")
async def websocket_live_prices(websocket: WebSocket):
    """
    WebSocket endpoint for real-time price streaming.
    Emits initial snapshot on connect, then pushes tick_diff events as they arrive.
    Client can send 'ping' text to keep connection alive; server responds with 'pong'.
    """
    service = DSEMarketService.get_instance()
    try:
        await service.subscribe_websocket(websocket)
    except WebSocketDisconnect:
        pass

@router.get("/ws/info")
async def websocket_info():
    """Get WebSocket connection stats"""
    service = DSEMarketService.get_instance()
    return {
        "ws_endpoint": "/api/stocks/ws",
        "active_ws_clients": service.get_ws_client_count(),
        "protocol": "WebSocket",
        "description": "Connect to ws://localhost:8000/api/stocks/ws for real-time price streaming"
    }

@router.get("/stream")
async def stream_live_prices(request: Request):
    """
    Server-Sent Events (SSE) stream endpoint.
    Emits initial snapshot upon connection, then streams real-time price diffs every 20 seconds.
    """
    service = DSEMarketService.get_instance()
    return StreamingResponse(
        service.subscribe_stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@router.post("/alerts")
async def create_stock_alert(req: CreateAlertRequest):
    """Create a user WhatsApp alert subscription"""
    alert_service = StockAlertService.get_instance()
    alert = alert_service.create_alert(
        whatsapp_number=req.whatsapp_number,
        ticker=req.ticker,
        alert_type=req.alert_type,
        threshold_value=req.threshold_value or 0.0,
        is_one_shot=req.is_one_shot if req.is_one_shot is not None else True,
        user_id=req.user_id
    )
    return {
        "success": True,
        "message": f"Alert created for {req.ticker} ({req.alert_type})",
        "alert": alert
    }

@router.get("/alerts")
async def get_stock_alerts(
    whatsapp_number: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None)
):
    """List configured user stock alerts"""
    alert_service = StockAlertService.get_instance()
    alerts = alert_service.get_alerts(whatsapp_number=whatsapp_number, user_id=user_id)
    return {
        "count": len(alerts),
        "alerts": alerts
    }

@router.delete("/alerts/{alert_id}")
async def delete_stock_alert(alert_id: str):
    """Delete a stock alert rule"""
    alert_service = StockAlertService.get_instance()
    deleted = alert_service.delete_alert(alert_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Alert ID not found")
    return {"success": True, "message": "Alert deleted"}

@router.post("/alerts/{alert_id}/rearm")
async def rearm_stock_alert(alert_id: str):
    """Re-arm a triggered or paused alert"""
    alert_service = StockAlertService.get_instance()
    rearmed = alert_service.rearm_alert(alert_id)
    if not rearmed:
        raise HTTPException(status_code=404, detail="Alert ID not found")
    return {"success": True, "message": "Alert re-armed and set to ACTIVE"}

@router.post("/alerts/test-ping")
async def send_test_ping(req: TestPingRequest, background_tasks: BackgroundTasks):
    """Dispatch an immediate WhatsApp test alert to verify local desktop connectivity"""
    alert_service = StockAlertService.get_instance()
    background_tasks.add_task(alert_service.send_test_alert, req.whatsapp_number)
    return {
        "success": True,
        "message": f"Test alert queued for {req.whatsapp_number}. Check your WhatsApp in a few seconds."
    }
