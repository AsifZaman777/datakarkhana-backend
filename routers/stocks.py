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

class SendOtpRequest(BaseModel):
    whatsapp_number: str

class VerifyOtpRequest(BaseModel):
    whatsapp_number: str
    otp: str


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

@router.get("/tickers")
async def get_tickers(
    exchange: Optional[str] = Query(None, description="Exchange code: DSE or CSE"),
    board: Optional[str] = Query(None, description="Board: ALL, MAIN, SME, DEBT, ATB"),
    category: Optional[str] = Query(None, description="Category: A, B, N, Z"),
    sector: Optional[str] = Query(None, description="Sector name"),
    search: Optional[str] = Query(None, description="Search symbol or name"),
    sort_by: Optional[str] = Query(None, description="Sort field, e.g. turnover, percent, volume, ltp"),
    limit: Optional[int] = Query(None, description="Max results to return"),
    page: Optional[int] = Query(None, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(25, ge=1, le=100, description="Page size, defaults to 25")
):
    """Get all cached stock tickers with flexible multi-board & multi-category screening and 25-stock pagination"""
    service = DSEMarketService.get_instance()
    tickers = service.get_tickers(
        exchange=exchange,
        board=board,
        category=category,
        sector=sector,
        search=search,
        sort_by=sort_by,
        limit=limit
    )

    if page is not None:
        total_filtered = len(tickers)
        total_pages = max(1, (total_filtered + page_size - 1) // page_size)
        start = (page - 1) * page_size
        sliced = tickers[start:start + page_size]
        return {
            "count": len(sliced),
            "total_available": len(service._last_prices),
            "total_filtered": total_filtered,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "tickers": sliced
        }

    return {
        "count": len(tickers),
        "total_available": len(service._last_prices),
        "tickers": tickers
    }

@router.get("/ticker/{ticker}")
async def get_ticker_detail(ticker: str):
    """Get comprehensive pro trading details, intraday ticks, and metrics for a specific stock"""
    service = DSEMarketService.get_instance()
    detail = service.get_ticker_detail(ticker)
    if not detail:
        raise HTTPException(status_code=404, detail=f"Stock ticker '{ticker}' not found")
    return detail

@router.get("/overview/{symbol}")
@router.get("/company/{symbol}/overview")
async def get_company_overview(symbol: str):
    """
    Get comprehensive company overview from LankaBangla matching OverviewV2 specifications.
    Includes profile, statistics, shareholding patterns, financial ratios, dividend history,
    interim reports, board members, auditors, and contacts.
    """
    service = DSEMarketService.get_instance()
    overview = await service.fetch_company_overview(symbol)
    return overview

@router.get("/summary")
async def get_market_summary():
    """Get live market summary: DSEX, DS30, DSES, total turnover, volume, trades, breadth, and live LankaBD market status"""
    service = DSEMarketService.get_instance()
    summary = service._last_market_summary
    if not summary:
        summary = await service.fetch_market_summary()
    exchanges = await service.fetch_exchanges_status()
    dse_status = service.get_market_status("DSE")
    return {
        "summary": summary,
        "is_trading_hour": service.is_trading_hour("DSE"),
        "market_status": dse_status,
        "exchanges": exchanges,
        "total_tracked": len(service._last_prices),
        "last_scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S+06:00", time.localtime(service._last_scrape_time)) if service._last_scrape_time else None
    }

@router.get("/exchanges")
async def get_exchanges():
    """Get all exchanges and their live market status directly synchronized with LankaBangla Portal"""
    service = DSEMarketService.get_instance()
    return await service.fetch_exchanges_status()

@router.get("/status")
async def get_market_status(exchange: str = Query("DSE", description="Exchange symbol: DSE or CSE")):
    """Get real-time market status from LankaBangla for given exchange (Open, Pre-Open, Post-Close, Closed)"""
    service = DSEMarketService.get_instance()
    exchanges = await service.fetch_exchanges_status()
    target = next((e for e in exchanges if e.get("code", "").upper() == exchange.upper()), None)
    status_str = target.get("marketStatus") if target else service.get_market_status(exchange)
    return {
        "exchange": exchange.upper(),
        "market_status": status_str,
        "is_trading_hour": service.is_trading_hour(exchange),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+06:00", time.localtime())
    }

@router.get("/depth")
async def get_market_depth(
    symbol: str = Query("GP", description="Stock symbol, e.g. GP, SQURPHARMA, BEXIMCO"),
    exchange: str = Query("DSE", description="Exchange: DSE or CSE")
):
    """
    Get real-time order book / market depth from LankaBangla for DSE or CSE.
    Returns bids (buy orders), asks (sell orders), buy/sell percentages, and stats.
    """
    service = DSEMarketService.get_instance()
    return await service.fetch_market_depth(symbol=symbol, exchange=exchange)

@router.get("/sectors")
async def get_sector_heatmap():
    """Get all 19 market sectors, turnover contributions, and price changes"""
    service = DSEMarketService.get_instance()
    heatmap = service._sector_heatmap_cache
    if not heatmap:
        heatmap = await service.fetch_sector_heatmap()
    return heatmap

@router.get("/news")
async def get_market_news(
    ticker: Optional[str] = Query(None, description="Filter news by stock ticker"),
    limit: int = Query(25, description="Number of news items to return")
):
    """Get latest corporate announcements, dividend declarations, AGM notices, and market news"""
    service = DSEMarketService.get_instance()
    news = service._recent_news
    if ticker:
        q = ticker.upper().strip()
        news = [n for n in news if n.get("code") == q or q in n.get("summary", "").upper()]
    return {
        "count": len(news[:limit]),
        "news": news[:limit]
    }

@router.get("/block-market")
async def get_block_market():
    """Get latest large block market transactions from LankaBangla"""
    service = DSEMarketService.get_instance()
    deals = await service.fetch_block_market()
    return {
        "count": len(deals),
        "deals": deals
    }

@router.get("/movers")
async def get_index_movers(count: int = Query(15, description="Number of movers to return")):
    """Get market index movers, contribution points, and top stock leaders"""
    service = DSEMarketService.get_instance()
    movers = await service.fetch_index_movers(count=count)
    top_movers = await service.fetch_top_movers()
    return {
        "index_movers": movers,
        "top_lists": top_movers
    }

@router.get("/exchanges")
async def get_exchanges():
    """Get list of supported stock exchanges: Dhaka Stock Exchange (DSE) and Chittagong Stock Exchange (CSE)"""
    service = DSEMarketService.get_instance()
    return {
        "exchanges": service.fetch_exchanges()
    }


@router.post("/alerts/send-otp")
async def send_verification_otp(req: SendOtpRequest):
    """Dispatch a 6-digit verification code to WhatsApp for phone verification"""
    alert_service = StockAlertService.get_instance()
    res = await alert_service.send_verification_otp(req.whatsapp_number)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message"))
    return res

@router.post("/alerts/verify-otp")
async def verify_phone_otp(req: VerifyOtpRequest):
    """Verify the 6-digit OTP code and unlock self-chat stock alert service"""
    alert_service = StockAlertService.get_instance()
    res = await alert_service.verify_phone_otp(req.whatsapp_number, req.otp)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message"))
    return res

@router.get("/alerts/verification-status")
async def check_verification_status(whatsapp_number: str = Query(...)):
    """Check if a phone number is verified for stock alerts"""
    alert_service = StockAlertService.get_instance()
    is_verified = alert_service.is_phone_verified(whatsapp_number)
    return {
        "whatsapp_number": whatsapp_number,
        "is_verified": is_verified
    }

@router.post("/alerts")
async def create_stock_alert(req: CreateAlertRequest):
    """Create a user WhatsApp alert subscription"""
    alert_service = StockAlertService.get_instance()
    try:
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
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

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
    """Dispatch an immediate WhatsApp test alert in self-chat to verify local desktop connectivity"""
    alert_service = StockAlertService.get_instance()
    if not alert_service.is_phone_verified(req.whatsapp_number):
        raise HTTPException(
            status_code=400,
            detail="Phone number not verified. Please click 'Send Verification OTP' to verify your WhatsApp Self-Chat first."
        )
    background_tasks.add_task(alert_service.send_test_alert, req.whatsapp_number)
    return {
        "success": True,
        "message": f"Test alert dispatched to {req.whatsapp_number} via WhatsApp Self-Chat."
    }
