# apps/options/urls.py
# ─────────────────────────────────────────────────────────────────────────────
#  COMPLETE URL CONFIGURATION FOR OPTIONS TRADING
#  Includes: Option Chain, Live Trading, Paper Trading, Backtest, Snapshots
# ─────────────────────────────────────────────────────────────────────────────

from django.urls import path
from . import views
from .prediction_views import latest_options_prediction, refresh_options_prediction

app_name = 'options'

urlpatterns = [
    # ══════════════════════════════════════════════════════════════════════════
    #  OPTION CHAIN - Market Data
    # ══════════════════════════════════════════════════════════════════════════
    path(
        "option-chain/",
        views.OptionChainView.as_view(),
        name="option-chain"
    ),
    # GET /api/v1/options/option-chain/?symbol=NIFTY&expiry=2025-05-29
    # Returns: Live option chain with spot, ATM, CE/PE data, OI, IV, Greeks
    
    
    # ══════════════════════════════════════════════════════════════════════════
    #  PAPER TRADING - Risk-Free Practice
    # ══════════════════════════════════════════════════════════════════════════
    path(
        "paper/trades/",
        views.PaperTradeView.as_view(),
        name="paper-trades"
    ),
    # GET  /api/v1/options/paper/trades/?status=open|closed|all
    # POST /api/v1/options/paper/trades/
    # List and create paper trades
    
    path(
        "paper/trades/<uuid:trade_id>/",
        views.PaperTradeView.as_view(),
        name="paper-trade-detail"
    ),
    # DELETE /api/v1/options/paper/trades/<uuid>/
    # Manually close paper trade
    
    path(
        "paper/account/",
        views.PaperAccountView.as_view(),
        name="paper-account"
    ),
    # GET /api/v1/options/paper/account/
    # Get paper trading balance, PnL, margin used
    
    # ── Legacy Paper Trading Endpoints (Backward Compatibility) ──────────────
    path(
        "paper-trade/",
        views.PaperTradeView.as_view(),
        name="paper-trade-legacy"
    ),
    # POST /api/v1/options/paper-trade/
    # Legacy endpoint for placing paper trades
    
    
    # ══════════════════════════════════════════════════════════════════════════
    #  LIVE TRADING - Real Broker Integration
    # ══════════════════════════════════════════════════════════════════════════
    path(
        "live/trades/",
        views.LiveOptionTradeView.as_view(),
        name="live-trades"
    ),
    # GET  /api/v1/options/live/trades/?status=open|closed|all&limit=20
    # POST /api/v1/options/live/trades/
    # List and place live option trades via broker (Fyers/Zerodha)
    
    path(
        "live/trades/<uuid:trade_id>/close/",
        views.LiveOptionTradeCloseView.as_view(),
        name="live-trade-close"
    ),
    # POST /api/v1/options/live/trades/<uuid>/close/
    # Manually close live trade with broker order
    
    # ── Legacy Live Trading Endpoints (Backward Compatibility) ───────────────
    path(
        "live-trade/",
        views.LiveOptionTradeView.as_view(),
        name="live-trade-legacy"
    ),
    # GET  /api/v1/options/live-trade/?status=open|closed|all
    # POST /api/v1/options/live-trade/
    # Legacy endpoint for live trading
    
    
    # ══════════════════════════════════════════════════════════════════════════
    #  UNIVERSAL TRADE MANAGEMENT - Works for Both Live & Paper
    # ══════════════════════════════════════════════════════════════════════════
    path(
        "open-trades/",
        views.OpenTradesView.as_view(),
        name="open-trades"
    ),
    # GET /api/v1/options/open-trades/?mode=live|paper|all
    # Get all open trades (live + paper) with unrealized PnL
    
    path(
        "close-trade/",
        views.CloseTradeView.as_view(),
        name="close-trade"
    ),
    # POST /api/v1/options/close-trade/
    # Body: {"trade_id": "uuid", "exit_price": 150.0}
    # Universal endpoint to close any trade (live or paper)
    
    path(
        "trades/<uuid:trade_id>/close/",
        views.CloseTradeView.as_view(),
        name="trade-close-detail"
    ),
    # POST /api/v1/options/trades/<uuid>/close/
    # RESTful endpoint for closing specific trade
    # Body: {"exit_price": 150.0} (optional)
    
    
    # ══════════════════════════════════════════════════════════════════════════
    #  BACKTEST - Historical Strategy Testing
    # ══════════════════════════════════════════════════════════════════════════
    path(
        "backtest/",
        views.BacktestRunView.as_view(),
        name="backtest-create"
    ),
    # POST /api/v1/options/backtest/
    # Body: {
    #     "symbol": "NIFTY",
    #     "from_date": "2024-01-01",
    #     "to_date": "2024-12-31",
    #     "strategy": "ICT_MTF",
    #     "capital": 500000
    # }
    
    path(
        "backtest/<uuid:run_id>/",
        views.BacktestRunView.as_view(),
        name="backtest-detail"
    ),
    # GET /api/v1/options/backtest/<uuid>/
    # Get backtest results and status
    
    
    # ══════════════════════════════════════════════════════════════════════════
    #  OPTION SNAPSHOTS - Historical Price Data
    # ══════════════════════════════════════════════════════════════════════════
    path(
        "snapshots/",
        views.OptionSnapshotView.as_view(),
        name="snapshots-list"
    ),
    # GET /api/v1/options/snapshots/?symbol=NIFTY&limit=20
    # Get option price snapshots by symbol name
    
    path(
        "snapshots/<uuid:symbol_id>/",
        views.OptionSnapshotView.as_view(),
        name="snapshots-detail"
    ),
    # GET /api/v1/options/snapshots/<symbol_id>/?limit=20
    # Get option price snapshots by symbol ID

    path(
        "predictions/<str:symbol>/",
        latest_options_prediction,
        name="options-prediction-latest",
    ),
    path(
        "predictions/<str:symbol>/refresh/",
        refresh_options_prediction,
        name="options-prediction-refresh",
    ),
]
