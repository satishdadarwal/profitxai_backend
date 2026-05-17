# apps/strategies/signal_router.py
#
# Smart Signal Router:
# Strategy ka broker + instrument_type dekho
# → correct broker pe sahi instrument mein order place karo
#
# Fyers:  options / futures / equity
# Delta:  futures / perp

import logging
from decimal import Decimal
from typing import Optional
from django.conf import settings


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
#  Main router — execute_cycle se yahan aao
# ─────────────────────────────────────────────────────────────────


def route_and_place_order(strategy, signal) -> Optional[object]:
    if strategy.mode == "paper":
        logger.info(
            "Paper mode detected | strategy=%s | instrument=%s",
            strategy.id, strategy.instrument_type
        )
        return _place_paper_trade(strategy, signal)
    
    if not strategy.broker:
        logger.warning(
            "Strategy %s mein broker set nahi hai — skipping order", strategy.id
        )
        return None

    broker_slug = strategy.broker.broker  # 'fyers' ya 'delta'
    instrument_type = strategy.instrument_type  # 'options'/'futures'/'equity'/'perp'

    logger.info(
        "Routing signal | strategy=%s | broker=%s | instrument=%s | symbol=%s | type=%s",
        strategy.id,
        broker_slug,
        instrument_type,
        signal.symbol,
        signal.signal_type,
    )

    try:
        if broker_slug == "fyers":
            return _place_fyers_order(strategy, signal, instrument_type)
        elif broker_slug == "delta":
            return _place_delta_order(strategy, signal, instrument_type)
        else:
            logger.error("Unknown broker slug: %s", broker_slug)
            return None

    except Exception as exc:
        logger.exception(
            "Order placement failed | strategy=%s | broker=%s | %s",
            strategy.id,
            broker_slug,
            exc,
        )
        return None


# ─────────────────────────────────────────────────────────────────
#  FYERS ORDER PLACEMENT
# ─────────────────────────────────────────────────────────────────


def _place_fyers_order(strategy, signal, instrument_type: str):
    """
    Fyers pe order place karo.
    instrument_type:
      - 'options' → CALL/PUT option buy karo
      - 'futures' → F&O future contract
      - 'equity'  → equity (cash) segment
    """
    from fyers_apiv3 import fyersModel

    from apps.brokers.models import BrokerAccount
    from apps.orders.services import create_order

    # Broker credentials fetch karo
    account = (
        BrokerAccount.objects.filter(
            user=strategy.user,
            broker="fyers",
            is_active=True,
            is_verified=True,
        )
        .select_related()
        .first()
    )

    if not account:
        logger.error("Fyers account not connected for user %s", strategy.user.pk)
        return None

    # Fyers client initialize
    fyers = fyersModel.FyersModel(
        client_id=settings.FYERS_APP_ID,
        token=account.access_token,
        log_path="",
        is_async=False,
    )

    # Risk params
    risk = strategy.risk_config
    qty = int(risk.get("qty", 1))

    if instrument_type == "options":
        return _fyers_options_order(strategy, signal, fyers, account, qty, risk)
    elif instrument_type == "futures":
        return _fyers_futures_order(strategy, signal, fyers, account, qty, risk)
    elif instrument_type == "equity":
        return _fyers_equity_order(strategy, signal, fyers, account, qty, risk)
    else:
        logger.error("Unknown instrument_type for Fyers: %s", instrument_type)
        return None


def _fyers_options_order(strategy, signal, fyers, account, qty: int, risk: dict):
    """Fyers options order — CALL (buy) ya PUT (buy)"""
    from apps.orders.services import create_order

    from .fyers_utils import get_atm_option_symbol  # helper — neeche define kiya

    symbol = signal.symbol  # e.g. 'NIFTY'
    signal_type = signal.signal_type  # 'buy' ya 'sell'
    current_price = float(signal.price)

    # ATM option symbol nikalo
    # buy signal → CALL, sell signal → PUT
    option_type = "CE" if signal_type == "buy" else "PE"
    option_symbol = get_atm_option_symbol(symbol, current_price, option_type)

    if not option_symbol:
        logger.error(
            "ATM option symbol nahi mila | symbol=%s | price=%s", symbol, current_price
        )
        return None

    # SL/target calculate karo
    sl_pct = float(risk.get("sl_pct", 0.5))
    target_pct = float(risk.get("target_pct", 1.0))
    sl_price = round(current_price * (1 - sl_pct / 100), 2)
    tgt_price = round(current_price * (1 + target_pct / 100), 2)

    logger.info(
        "Fyers OPTIONS order | symbol=%s | type=%s | qty=%d | price=%s | sl=%s | tgt=%s",
        option_symbol,
        option_type,
        qty,
        current_price,
        sl_price,
        tgt_price,
    )

    # Fyers order data
    order_data = {
        "symbol": option_symbol,
        "qty": qty,
        "type": 2,  # Market order
        "side": 1,  # 1=buy (always buy options)
        "productType": "INTRADAY",
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
    }

    resp = fyers.place_order(data=order_data)

    if resp.get("s") == "ok":
        exchange_order_id = resp.get("id")
        order = create_order(
            strategy=strategy,
            symbol=option_symbol,
            side="buy",
            quantity=qty,
            price=Decimal(str(current_price)),
            sl_price=Decimal(str(sl_price)),
            target_price=Decimal(str(tgt_price)),
            instrument_type="options",
            broker=account, 
            exchange_order_id=exchange_order_id,
            mode=strategy.mode,
        )
        logger.info(
            "Fyers options order placed | order_id=%s | exchange_id=%s",
            order.id if order else None,
            exchange_order_id,
        )
        return order
    else:
        logger.error("Fyers options order FAILED | resp=%s", resp)
        _ws_notify_failure(strategy.user, strategy.algo_name, resp.get("message", "Unknown error")) 
        return None


def _fyers_futures_order(strategy, signal, fyers, account, qty: int, risk: dict):
    """Fyers F&O futures order"""
    from apps.orders.services import create_order

    from .fyers_utils import get_current_futures_symbol

    symbol = signal.symbol
    signal_type = signal.signal_type
    current_price = float(signal.price)

    # Current month futures symbol
    futures_symbol = get_current_futures_symbol(symbol)
    if not futures_symbol:
        logger.error("Futures symbol nahi mila | symbol=%s", symbol)
        return None

    sl_pct = float(risk.get("sl_pct", 0.5))
    target_pct = float(risk.get("target_pct", 1.0))

    if signal_type == "buy":
        sl_price = round(current_price * (1 - sl_pct / 100), 2)
        tgt_price = round(current_price * (1 + target_pct / 100), 2)
        side = 1  # Fyers buy
    else:
        sl_price = round(current_price * (1 + sl_pct / 100), 2)
        tgt_price = round(current_price * (1 - target_pct / 100), 2)
        side = -1  # Fyers sell

    order_data = {
        "symbol": futures_symbol,
        "qty": qty,
        "type": 2,  # Market
        "side": side,
        "productType": "INTRADAY",
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
    }

    resp = fyers.place_order(data=order_data)

    if resp.get("s") == "ok":
        exchange_order_id = resp.get("id")
        order = create_order(
            strategy=strategy,
            symbol=futures_symbol,
            side=signal_type,
            quantity=qty,
            price=Decimal(str(current_price)),
            sl_price=Decimal(str(sl_price)),
            target_price=Decimal(str(tgt_price)),
            instrument_type="futures",
            broker=strategy.broker,
            exchange_order_id=exchange_order_id,
            mode=strategy.mode,
        )
        logger.info(
            "Fyers futures order placed | order_id=%s", order.id if order else None
        )
        return order
    else:
        logger.error("Fyers futures order FAILED | resp=%s", resp)
        _ws_notify_failure(strategy.user, strategy.algo_name, resp.get("message", "Unknown error"))
        return None


def _fyers_equity_order(strategy, signal, fyers, account, qty: int, risk: dict):
    """Fyers equity (cash) segment order"""
    from apps.orders.services import create_order

    symbol = signal.symbol
    signal_type = signal.signal_type
    current_price = float(signal.price)

    fyers_symbol = f"NSE:{symbol}-EQ"

    sl_pct = float(risk.get("sl_pct", 0.5))
    target_pct = float(risk.get("target_pct", 1.0))

    if signal_type == "buy":
        sl_price = round(current_price * (1 - sl_pct / 100), 2)
        tgt_price = round(current_price * (1 + target_pct / 100), 2)
        side = 1
    else:
        sl_price = round(current_price * (1 + sl_pct / 100), 2)
        tgt_price = round(current_price * (1 - target_pct / 100), 2)
        side = -1

    order_data = {
        "symbol": fyers_symbol,
        "qty": qty,
        "type": 2,
        "side": side,
        "productType": "CNC",  # Cash and carry for equity
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
    }

    resp = fyers.place_order(data=order_data)

    if resp.get("s") == "ok":
        exchange_order_id = resp.get("id")
        order = create_order(
            strategy=strategy,
            symbol=fyers_symbol,
            side=signal_type,
            quantity=qty,
            price=Decimal(str(current_price)),
            sl_price=Decimal(str(sl_price)),
            target_price=Decimal(str(tgt_price)),
            instrument_type="equity",
            broker=strategy.broker,
            exchange_order_id=exchange_order_id,
            mode=strategy.mode,
        )
        return order
    else:
        logger.error("Fyers equity order FAILED | resp=%s", resp)
        _ws_notify_failure(strategy.user, strategy.algo_name, resp.get("message", "Unknown error"))
        return None


# ─────────────────────────────────────────────────────────────────
#  DELTA EXCHANGE ORDER PLACEMENT
# ─────────────────────────────────────────────────────────────────


def _place_delta_order(strategy, signal, instrument_type: str):
    """
    Delta Exchange pe order place karo.
    instrument_type:
      - 'futures' → futures contract
      - 'perp'    → perpetual contract (most common)
    """
    from apps.brokers.models import BrokerAccount
    from apps.orders.services import create_order

    account = BrokerAccount.objects.filter(
        user=strategy.user,
        broker="delta",
        is_active=True,
        is_verified=True,
    ).first()

    if not account:
        logger.error("Delta account not connected for user %s", strategy.user.pk)
        return None

    symbol = signal.symbol  # e.g. 'BTCUSDT'
    signal_type = signal.signal_type  # 'buy' ya 'sell'
    current_price = float(signal.price)

    risk = strategy.risk_config
    qty = int(risk.get("qty", 1))
    sl_pct = float(risk.get("sl_pct", 0.5))
    target_pct = float(risk.get("target_pct", 1.0))

    if signal_type == "buy":
        sl_price = round(current_price * (1 - sl_pct / 100), 2)
        tgt_price = round(current_price * (1 + target_pct / 100), 2)
        side = "buy"
    else:
        sl_price = round(current_price * (1 + sl_pct / 100), 2)
        tgt_price = round(current_price * (1 - target_pct / 100), 2)
        side = "sell"

    # Delta product type
    product_type = "perpetual_futures" if instrument_type == "perp" else "futures"

    try:
        # Delta API call
        delta_resp = _call_delta_api(
            account=account,
            symbol=symbol,
            side=side,
            qty=qty,
            product_type=product_type,
            current_price=current_price,
        )

        if delta_resp.get("success"):
            exchange_order_id = str(delta_resp.get("result", {}).get("id", ""))
            order = create_order(
                strategy=strategy,
                symbol=symbol,
                side=signal_type,
                quantity=qty,
                price=Decimal(str(current_price)),
                sl_price=Decimal(str(sl_price)),
                target_price=Decimal(str(tgt_price)),
                instrument_type=instrument_type,
                broker=strategy.broker,
                exchange_order_id=exchange_order_id,
                mode=strategy.mode,
            )
            logger.info(
                "Delta order placed | symbol=%s | side=%s | qty=%d | price=%s",
                symbol,
                side,
                qty,
                current_price,
            )
            return order
        else:
            logger.error("Delta order FAILED | resp=%s", delta_resp)
            _ws_notify_failure(strategy.user, strategy.algo_name, delta_resp.get("error", {}).get("message", "Unknown error"))
            return None

    except Exception as exc:
        logger.exception("Delta order exception | %s", exc)
        return None

def _call_delta_api(account, symbol, side, qty, product_type, current_price):
    import hashlib, hmac, json, time
    import requests

    api_key    = account.api_key
    api_secret = account.api_secret

    method    = "POST"
    path      = "/v2/orders"
    timestamp = str(int(time.time()))

    payload = {
        "product_id": _get_delta_product_id(_to_delta_symbol(symbol, product_type)),
        "size":       qty,
        "side":       side,
        "order_type": "market_order",
        "time_in_force": "gtc",
    }

    body_str = json.dumps(payload)
    signature_data = method + timestamp + path + body_str

    signature = hmac.new(
        key=api_secret.encode(),
        msg=signature_data.encode(),
        digestmod=hashlib.sha256
    ).hexdigest()

    headers = {
        "api-key":      api_key,
        "timestamp":    timestamp,
        "signature":    signature,
        "Content-Type": "application/json",
        "User-Agent":   "python-rest-client",
    }

    # ✅ India URL + data= use karo
    resp = requests.post(
        "https://api.india.delta.exchange" + path,
        data=body_str,
        headers=headers,
        timeout=10,
    )
    return resp.json()


def _get_delta_product_id(delta_symbol: str) -> int:
    # ✅ India pe BTCUSD ID = 27
    product_ids = {
        "BTCUSD": 27,
        "ETHUSD": 28,   # verify karo
        "SOLUSD": 29, 
        "BNBUSD": 30,  # verify karo
    }
    product_id = product_ids.get(delta_symbol)
    if product_id is None:
        raise ValueError(f"Delta product ID not found: {delta_symbol}")
    return product_id

def _to_delta_symbol(symbol: str, product_type: str) -> str:
    """BTCUSDT → Delta ke format mein convert karo"""
    mapping = {
        "BTCUSDT": {"perp": "BTCUSD", "futures": "BTCUSD"},
        "ETHUSDT": {"perp": "ETHUSD", "futures": "ETHUSD"},
        "SOLUSDT": {"perp": "SOLUSD", "futures": "SOLUSD"},
        "BNBUSDT": {"perp": "BNBUSD", "futures": "BNBUSD"},
    }
    return mapping.get(symbol, {}).get(product_type, symbol.replace("USDT", "USD"))




# ─────────────────────────────────────────────────────────────────
#  PAPER TRADING
# ─────────────────────────────────────────────────────────────────


def _place_paper_trade(strategy, signal):
    """
    Convert signal → PaperTrade
    Supports: options, futures, equity, crypto
    """
    from apps.paper_trading.services import open_trade

    # Extract signal data
    meta = signal.metadata or {}
    symbol = signal.symbol
    direction = signal.signal_type  # "buy" / "sell"
    entry_price = float(signal.price)

    # ── FIX 2: Position size from metadata (in lots) ──────────────
    # Silver Bullet / ICT signals send position_size in lots
    position_size = meta.get("position_size") or float(
        strategy.risk_config.get("qty", 1)
    )
    position_size = float(position_size)

    logger.info(
        "Position sizing | lots=%.4f | from_metadata=%s",
        position_size,
        bool(meta.get("position_size")),
    )

    # SL/TP from signal metadata (ICT signals have these as spot prices)
    # Note: for options these will be RECALCULATED based on option premium below
    sl_price = meta.get("stop_loss")
    tp_price = meta.get("take_profit_2") or meta.get("take_profit_1")

    # Fallback to risk_config if metadata missing
    if not sl_price:
        sl_price = _calculate_sl(entry_price, direction, strategy)
    if not tp_price:
        tp_price = _calculate_tp(entry_price, direction, strategy)

    # Route by instrument type
    instrument = strategy.instrument_type

    logger.info(
        "Creating paper trade | instrument=%s | symbol=%s | direction=%s | lots=%.4f",
        instrument, symbol, direction, position_size,
    )

    if instrument == "options":
        return _paper_option_trade(
            strategy, signal, symbol, direction,
            entry_price, sl_price, tp_price, position_size,
        )
    elif instrument in ["futures", "equity", "crypto"]:
        return _paper_generic_trade(
            strategy, symbol, direction,
            entry_price, sl_price, tp_price, position_size,
            asset_type=instrument,
        )
    else:
        logger.error("Unknown instrument type: %s", instrument)
        return None



def _paper_option_trade(
    strategy, signal, base_symbol, direction,
    entry, sl, tp, lots,
):
    """
    Paper options trade - Supports BUYER and SELLER both.

    KEY FIXES:
    1. entry/sl/tp from caller are SPOT prices → we recalculate based on option premium
    2. SELLER logic: inverted SL/TP + higher margin
    3. Updated NSE lot sizes (April 2025 changes)
    """
    import re

    from apps.paper_trading.services import open_trade
    from apps.strategies.fyers_utils import get_atm_option_symbol

    # ══════════════════════════════════════════
    # UPDATED NSE LOT SIZES (April 2025)
    # ══════════════════════════════════════════
    LOT_SIZES = {
        "NIFTY": 65,
        "BANKNIFTY": 30,
        "FINNIFTY": 60,
        "MIDCPNIFTY": 120,
        "SENSEX": 10,
    }

    # ══════════════════════════════════════════
    # 1. DETERMINE OPTION TYPE & ACTION
    # ══════════════════════════════════════════
    
    trader_type = strategy.risk_config.get("trader_type", "buyer")

    if direction == "buy":  # Bullish signal
        if trader_type == "buyer":
            option_type = "CE"
            action = "buy"
            strategy_name = "Long Call"
        else:  # seller
            option_type = "PE"
            action = "sell"
            strategy_name = "Short Put (Bullish)"
    else:  # Bearish signal
        if trader_type == "buyer":
            option_type = "PE"
            action = "buy"
            strategy_name = "Long Put"
        else:  # seller
            option_type = "CE"
            action = "sell"
            strategy_name = "Short Call (Bearish)"

    logger.info(
        "Strategy: %s | Type: %s | Action: %s | Trader: %s",
        strategy_name, option_type, action, trader_type
    )

    # ══════════════════════════════════════════
    # 2. GET ATM OPTION SYMBOL
    # ══════════════════════════════════════════
    
    spot_price = entry  # Used only for strike selection

    option_symbol = get_atm_option_symbol(base_symbol, spot_price, option_type)
    if not option_symbol:
        logger.error("Failed to get ATM option symbol for %s", base_symbol)
        return None

    logger.info("Option symbol: %s", option_symbol)

    # ══════════════════════════════════════════
    # 3. FETCH REAL OPTION PREMIUM (LTP)
    # ══════════════════════════════════════════
    
    option_entry = spot_price * 0.01  # Fallback estimate (1% of spot)
    
    try:
        from apps.market.models import Asset
        asset = Asset.objects.get(symbol=option_symbol)
        real_ltp = float(asset.last_price)
        
        if real_ltp > 0:
            option_entry = real_ltp
            logger.info(
                "✅ Using real option LTP: ₹%.2f (spot was ₹%.2f)",
                option_entry, spot_price
            )
        else:
            logger.warning(
                "LTP is zero for %s — using estimate ₹%.2f",
                option_symbol, option_entry
            )
    except Exception as e:
        logger.warning(
            "Asset not found: %s — using estimate ₹%.2f | Error: %s",
            option_symbol, option_entry, e
        )

    # ══════════════════════════════════════════
    # 4. CALCULATE SL/TP (DIFFERENT FOR BUY VS SELL!)
    # ══════════════════════════════════════════
    
    sl_pct = float(strategy.risk_config.get("sl_pct", 25))
    tp_pct = float(strategy.risk_config.get("target_pct", 50))
    
    if action == "buy":
        # BUYER: Profit when price ↑, Loss when price ↓
        option_sl = round(option_entry * (1 - sl_pct / 100), 2)
        option_tp = round(option_entry * (1 + tp_pct / 100), 2)
        
        logger.info(
            "BUYER levels | Entry=₹%.2f | SL=₹%.2f (%.1f%% below) | TP=₹%.2f (%.1f%% above)",
            option_entry, option_sl, sl_pct, option_tp, tp_pct
        )
        
    else:  # action == "sell"
        # SELLER: Profit when price ↓, Loss when price ↑
        # SL is HIGHER (if price goes up, we lose)
        # TP is LOWER (if price goes down, we profit)
        option_sl = round(option_entry * (1 + sl_pct / 100), 2)  # Higher = Loss
        option_tp = round(option_entry * (1 - tp_pct / 100), 2)  # Lower = Profit
        
        logger.info(
            "SELLER levels | Entry=₹%.2f | SL=₹%.2f (%.1f%% above) | TP=₹%.2f (%.1f%% below)",
            option_entry, option_sl, sl_pct, option_tp, tp_pct
        )

    # ══════════════════════════════════════════
    # 5. EXTRACT STRIKE & LOT SIZE
    # ══════════════════════════════════════════
    
    match = re.search(r'(\d{5})(CE|PE)$', option_symbol)
    strike = int(match.group(1)) if match else 0
    
    lot_size = LOT_SIZES.get(base_symbol.upper(), 25)  # Default 25 if not found
    
    logger.info(
        "Symbol breakdown | Strike: %d | Type: %s | Lot Size: %d",
        strike, option_type, lot_size
    )

    # ══════════════════════════════════════════
    # 6. CALCULATE MARGIN (DIFFERENT FOR BUY VS SELL!)
    # ══════════════════════════════════════════
    
    if action == "buy":
        # BUYER: Margin = Premium paid
        # Formula: Premium × Lots × Lot Size
        margin_calculation = "Premium × Lots × Lot Size"
        # Note: margin will be calculated by open_trade() service
        
        logger.info(
            "BUYER margin formula: %s = %.2f × %.2f × %d",
            margin_calculation, option_entry, lots, lot_size
        )
        
    else:  # action == "sell"
        # SELLER: Margin = SPAN + Exposure (NSE requirement)
        # Approximate formula: ~15-20% of (Spot × Lot Size)
        # This is MUCH higher than buyer's premium
        
        # Note: Real SPAN margin varies by volatility
        # Using conservative estimate: 15% of notional
        logger.info(
            "SELLER margin: Higher than buyer (~1.5-2x premium based on SPAN)"
        )
        logger.warning(
            "⚠️ Paper trading uses simplified margin. "
            "Real broker margin will vary based on SPAN calculator."
        )

    # ══════════════════════════════════════════
    # 7. CREATE TRADE DATA
    # ══════════════════════════════════════════
    
    trade_data = {
        "symbol": option_symbol,
        "asset_type": "option",
        "instrument_type": strategy.instrument_type,
        "display_name": f"{base_symbol} {strike}{option_type}",
        "side": action,
        "quantity": lots,           # Lots from signal (e.g., 1.5)
        "lot_size": lot_size,       # NSE lot size (e.g., 25)
        "leverage": 1,
        "entry_price": option_entry,    # ✅ Option premium (e.g., ₹220.90)
        "stop_loss": option_sl,         # ✅ Correct for buy/sell
        "target_price": option_tp,      # ✅ Correct for buy/sell
        "strike_price": strike,
        "option_type": option_type,
        "setup_type": f"Auto-{strategy.algo_name}-{strategy_name}",
        "strategy_id": str(strategy.id),
        "nifty_spot_at_entry": spot_price,  # Store spot for reference
    }

    # ══════════════════════════════════════════
    # 8. LOG & CREATE TRADE
    # ══════════════════════════════════════════
    
    logger.info(
        "Creating %s: %s %s%s @ ₹%.2f | SL=₹%.2f | TP=₹%.2f | Lots=%.2f × %d",
        strategy_name, action.upper(), strike, option_type,
        option_entry, option_sl, option_tp, lots, lot_size
    )

    try:
        trade = open_trade(strategy.user, trade_data)
        
        logger.info(
            "✅ %s created | Trade ID: %s | Margin: ₹%.2f",
            strategy_name, trade.id, float(trade.margin_used)
        )
        
        # WebSocket notification
        _ws_notify_trade(strategy.user, trade, option_entry, option_sl, option_tp)
        
        return trade
        
    except Exception as e:
        logger.error(
            "❌ Failed to create %s: %s", strategy_name, e, exc_info=True
        )
        return None


def _paper_generic_trade(
    strategy, symbol, direction,
    entry, sl, tp, qty, asset_type,
):
    """
    Generic paper trade (futures/equity/crypto)
    """
    from apps.paper_trading.services import open_trade

    trade_data = {
        "symbol": symbol,
        "asset_type": asset_type,
        "instrument_type": strategy.instrument_type,
        "display_name": symbol,
        "side": direction,
        "quantity": qty,
        "lot_size": 1,
        "leverage": 1,
        "entry_price": entry,
        "stop_loss": sl,
        "target_price": tp,
        "setup_type": f"Auto-{strategy.algo_name}",
        "strategy_id": str(strategy.id),
    }

    logger.info(
        "Creating paper %s trade: %s %s @ %.2f | SL=%.2f | TP=%.2f | qty=%.4f",
        asset_type, direction.upper(), symbol, entry, sl, tp, qty,
    )

    try:
        trade = open_trade(strategy.user, trade_data)
        logger.info("✅ Paper trade created: %s", trade.id)
        _ws_notify_trade(strategy.user, trade, entry, sl, tp)
        return trade
    except Exception as e:
        logger.error("Failed to create paper trade: %s", e, exc_info=True)
        return None
    
def _calculate_sl(price: float, action: str, strategy) -> float:
    """
    Calculate stop loss based on action (buy vs sell)
    
    BUYER (action="buy"): SL is BELOW entry (price drops)
    SELLER (action="sell"): SL is ABOVE entry (price rises)
    """
    sl_pct = float(strategy.risk_config.get("sl_pct", 25))
    
    if action == "buy":
        # Buyer loses when price goes DOWN
        sl = price * (1 - sl_pct / 100)
    else:  # sell
        # Seller loses when price goes UP
        sl = price * (1 + sl_pct / 100)
    
    return round(sl, 2)


def _calculate_tp(price: float, action: str, strategy) -> float:
    """
    Calculate take profit based on action (buy vs sell)
    
    BUYER (action="buy"): TP is ABOVE entry (price rises)
    SELLER (action="sell"): TP is BELOW entry (price drops)
    """
    tp_pct = float(strategy.risk_config.get("target_pct", 50))
    
    if action == "buy":
        # Buyer profits when price goes UP
        tp = price * (1 + tp_pct / 100)
    else:  # sell
        # Seller profits when price goes DOWN
        tp = price * (1 - tp_pct / 100)
    
    return round(tp, 2)


def _ws_notify_trade(user, trade, entry, sl, tp):
    """Send trade notification via WebSocket"""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if not layer:
            logger.warning("Channel layer not available")
            return

        message = (
            f"✅ {trade.side.upper()} {trade.display_name} @ ₹{entry:.1f} | "
            f"SL: ₹{sl:.1f} | TP: ₹{tp:.1f}"
        )

        async_to_sync(layer.group_send)(
            f"user_{user.id}",
            {
                "type": "trade.placed",
                "message": message,
                "trade_id": str(trade.id),
                "is_error": False,
            },
        )

        logger.info("WS notification sent to user %s", user.id)

    except Exception as e:
        logger.warning("WS notification failed: %s", e)


def _ws_notify_failure(user, strategy_name: str, reason: str):
    """Order failure ka WebSocket notification user ke browser pe bhejo."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if not layer:
            logger.warning("Channel layer not available")
            return

        async_to_sync(layer.group_send)(
            f"user_{user.id}",
            {
                "type": "trade.placed",
                "message": f"❌ Order failed | {strategy_name} | {reason}",
                "is_error": True,
            },
        )
        logger.info("Failure WS notification sent | user=%s", user.id)

    except Exception as e:
        logger.warning("WS failure notification failed: %s", e)