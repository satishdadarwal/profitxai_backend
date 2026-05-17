import logging
from decimal import Decimal
from django.utils import timezone
from .models import PaperAccount, PaperTrade, normalize_symbol, get_lot_size

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 🔍 ASSET TYPE DETECTION
# ─────────────────────────────────────────────
_OPTION_KEYWORDS = ('NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX')
_CRYPTO_KEYWORDS = ('BTC', 'ETH', 'USDT', 'BNB', 'SOL', 'XRP')

def detect_asset_type(raw_asset: str, symbol: str, instrument_type: str = "") -> str:
    """
    Detect asset type from multiple sources.

    Priority (highest → lowest):
    1. instrument_type  — strategy.instrument_type  (most reliable, set by user)
    2. raw_asset        — data["asset_type"] explicit value
    3. Symbol keywords  — NIFTY→option, BTC→crypto
    4. Fallback         — 'option' (Indian market default)

    instrument_type values (from Strategy model):
        'options'  → 'option'
        'futures'  → 'futures'
        'equity'   → 'equity'
        'crypto'   → 'crypto'
        'perp'     → 'futures'
    """
    # 1. instrument_type (Strategy field) — highest priority
    instr = (instrument_type or "").strip().lower()
    if instr == "options":
        return "option"
    if instr in ("futures", "perp"):
        return "futures"
    if instr == "equity":
        return "equity"
    if instr == "crypto":
        return "crypto"

    # 2. Explicit raw_asset value
    raw = (raw_asset or "").strip().lower()
    if raw in ("option", "options"):
        return "option"
    if raw == "crypto":
        return "crypto"
    if raw in ("futures", "perp"):
        return "futures"
    if raw == "equity":
        return "equity"

    # 3. Symbol keyword matching
    symbol_upper = symbol.upper()
    if any(k in symbol_upper for k in _OPTION_KEYWORDS):
        return "option"
    if any(k in symbol_upper for k in _CRYPTO_KEYWORDS):
        return "crypto"

    # 4. Unknown raw_asset — trust it as-is
    if raw:
        return raw

    # 5. Fallback
    return "option"


# ─────────────────────────────────────────────
# 🎯 POSITION SIZING (HYBRID)
# ─────────────────────────────────────────────
def calculate_position_size(
    account: PaperAccount,
    symbol: str,
    asset_type: str,
    entry_price: Decimal,
    side: str,
    stop_loss: Decimal = None,
    leverage: int = 1,

    # User inputs (priority order)
    quantity: Decimal = None,          # 1️⃣ Direct qty
    risk_amount: Decimal = None,       # 2️⃣ Fixed risk amount
    risk_pct: Decimal = None,          # 3️⃣ Risk % of balance
    lot_size_override: int = None,     # For manual lot size
) -> dict:
    """
    Hybrid position sizing calculator

    Priority:
    1. If `quantity` provided → use it directly
    2. If `risk_amount` provided → calculate qty from risk amount
    3. If `risk_pct` provided → calculate qty from % of balance
    4. Fallback → use account's default risk_per_trade_pct

    Returns:
        {
            "quantity": Decimal,
            "lot_size": int,
            "margin": Decimal,
            "max_loss": Decimal,  # Expected loss if SL hit
        }
    """

    # ✅ Get lot size for asset
    if lot_size_override:
        lot_size = lot_size_override
    else:
        lot_size = get_lot_size(symbol, asset_type)

    # ─────────────────────────────────────────────
    # 1️⃣ DIRECT QUANTITY (User Override)
    # ─────────────────────────────────────────────
    if quantity is not None:
        qty = Decimal(str(quantity))

        if asset_type == "option":
            margin = entry_price * lot_size * qty
        elif asset_type == "futures":
            margin = (entry_price * lot_size * qty) / leverage
        else:  # crypto
            margin = (entry_price * qty) / leverage

        max_loss = calculate_max_loss(entry_price, stop_loss, qty, lot_size, side, leverage)

        return {
            "quantity": qty,
            "lot_size": lot_size,
            "margin": margin,
            "max_loss": max_loss,
        }

    # ─────────────────────────────────────────────
    # 2️⃣ RISK-BASED CALCULATION
    # ─────────────────────────────────────────────

    if risk_amount is not None:
        risk = Decimal(str(risk_amount))
    elif risk_pct is not None:
        risk = account.balance * (Decimal(str(risk_pct)) / 100)
    else:
        # Fallback to account default
        risk = account.balance * (account.risk_per_trade_pct / 100)

    logger.info(f"💰 Risk amount: ₹{risk}")

    # ─────────────────────────────────────────────
    # Calculate quantity based on risk & stop loss
    # ─────────────────────────────────────────────
    if stop_loss and entry_price:
        risk_per_unit = abs(entry_price - Decimal(str(stop_loss)))

        if risk_per_unit > 0:
            if asset_type == "option":
                qty = risk / (risk_per_unit * lot_size * leverage)
            elif asset_type == "futures":
                qty = risk / (risk_per_unit * lot_size * leverage)
            else:  # crypto
                qty = risk / (risk_per_unit * leverage)

            qty = qty.quantize(Decimal("0.0001"))
        else:
            raise ValueError("Stop loss too close to entry price")
    else:
        # No stop loss → use risk as position size
        if asset_type == "option":
            qty = risk / (entry_price * lot_size)
        elif asset_type == "futures":
            qty = risk / (entry_price * lot_size)
        else:  # crypto
            qty = risk / entry_price

        qty = qty.quantize(Decimal("0.0001"))

    # Calculate margin
    if asset_type == "option":
        margin = entry_price * lot_size * qty
    elif asset_type == "futures":
        margin = (entry_price * lot_size * qty) / leverage
    else:  # crypto
        margin = (entry_price * qty) / leverage

    max_loss = calculate_max_loss(entry_price, stop_loss, qty, lot_size, side, leverage)

    return {
        "quantity": qty,
        "lot_size": lot_size,
        "margin": margin,
        "max_loss": max_loss,
    }


def calculate_max_loss(
    entry_price: Decimal,
    stop_loss: Decimal,
    quantity: Decimal,
    lot_size: int,
    side: str,
    leverage: int = 1
) -> Decimal:
    """Calculate expected loss if stop loss hits"""
    if not stop_loss:
        return Decimal("0")

    risk_per_unit = abs(entry_price - stop_loss)
    total_qty = quantity * lot_size
    max_loss = risk_per_unit * total_qty * leverage

    return max_loss


# ─────────────────────────────────────────────
# 📈 OPEN TRADE
# ─────────────────────────────────────────────
def open_trade(user, data: dict):
    """
    Open a new paper trade with proper risk management
    """
    account, _ = PaperAccount.objects.get_or_create(user=user)

    # ─────────────────────────────
    # Extract data
    # ─────────────────────────────
    symbol = normalize_symbol(data.get("symbol", ""))

    # ✅ FIX 1: Smart asset_type detection (symbol से fallback)
    raw_asset = data.get("asset_type", "")
    instrument_type = data.get("instrument_type", "")  # strategy.instrument_type से आता है
    asset_type = detect_asset_type(raw_asset, symbol, instrument_type)

    entry_price = Decimal(str(data["entry_price"]))
    side = data.get("side", "buy").lower()
    leverage = int(data.get("leverage", 1))
    stop_loss = Decimal(str(data["stop_loss"])) if data.get("stop_loss") else None
    target_price = Decimal(str(data["target_price"])) if data.get("target_price") else None

    logger.info(f"🔍 Asset type detected: '{raw_asset}' → '{asset_type}' for symbol '{symbol}'")

    # ─────────────────────────────
    # ✅ BASIC VALIDATION (without position size)
    # ─────────────────────────────
    can_trade, reason = account.can_open_new_trade(
        asset_type=asset_type,
        leverage=leverage
    )
    if not can_trade:
        raise ValueError(reason)

    # ─────────────────────────────
    # 📊 POSITION CALCULATION
    # ─────────────────────────────
    position = calculate_position_size(
        account=account,
        symbol=symbol,
        asset_type=asset_type,
        entry_price=entry_price,
        side=side,
        stop_loss=stop_loss,
        leverage=leverage,
        quantity=data.get("quantity"),
        risk_amount=data.get("risk_amount"),
        risk_pct=data.get("risk_pct"),
        lot_size_override=data.get("lot_size"),
    )

    quantity = position["quantity"]
    lot_size = position["lot_size"]
    margin = position["margin"]
    max_loss = position["max_loss"]

    logger.info(
        f"📊 Position: qty={quantity}, lot={lot_size}, margin=₹{margin}, max_loss=₹{max_loss}"
    )

    # ─────────────────────────────
    # 🔥 FINAL VALIDATION (with position size)
    # ─────────────────────────────
    can_trade, reason = account.can_open_new_trade(
        asset_type=asset_type,
        position_size=margin,
        leverage=leverage
    )
    if not can_trade:
        raise ValueError(reason)

    # ✅ FIX 2: Risk cap — account की setting से लो, hardcoded 2% नहीं
    # account.max_risk_per_trade_pct field होनी चाहिए; fallback 2%
    max_risk_pct = Decimal(str(
        getattr(account, "max_risk_per_trade_pct", None) or "2.0"
    ))
    allowed_loss = account.balance * (max_risk_pct / 100)

    if max_loss > Decimal("0") and max_loss > allowed_loss:
        raise ValueError(
            f"Risk too high. Max allowed: ₹{allowed_loss:.2f} ({max_risk_pct}% of balance), "
            f"Your risk: ₹{max_loss:.2f}"
        )

    # Balance check
    if account.available_balance < margin:
        raise ValueError(
            f"Insufficient balance. Need ₹{margin:.2f}, Available ₹{account.available_balance:.2f}"
        )

    # ─────────────────────────────
    # ✅ CREATE TRADE
    # ─────────────────────────────
    trade = PaperTrade.objects.create(
        account=account,
        symbol=symbol,
        asset_type=asset_type,
        side=side,
        quantity=quantity,
        lot_size=lot_size,
        leverage=leverage,
        entry_price=entry_price,
        current_price=entry_price,
        stop_loss=stop_loss,
        target_price=target_price,
        margin_used=margin,
        display_name=data.get("display_name", symbol),
        setup_type=data.get("setup_type", ""),
        strategy_id=data.get("strategy_id", ""),
        strike_price=data.get("strike_price"),
        option_type=data.get("option_type", ""),
        nifty_spot_at_entry=data.get("nifty_spot_at_entry", 0),
    )

    # Deduct margin
    account.balance -= margin
    account.save()

    logger.info(
        f"✅ Trade opened: {trade.symbol} {trade.side} {trade.quantity} @ ₹{trade.entry_price}"
    )

    return trade


# ─────────────────────────────────────────────
# 📉 CLOSE TRADE
# ─────────────────────────────────────────────
def close_trade(trade_id: str, exit_price: Decimal, reason: str = "manual"):
    """Close a trade and calculate PnL"""
    trade = PaperTrade.objects.get(id=trade_id)

    if trade.status == "closed":
        logger.warning(f"⚠️ Trade {trade_id} already closed")
        return trade

    account = trade.account

    # Calculate PnL
    qty = trade.quantity * trade.lot_size
    ep = trade.entry_price
    xp = exit_price

    if trade.side in ['buy', 'long']:
        raw_pnl = (xp - ep) * qty
    else:  # sell or short
        raw_pnl = (ep - xp) * qty

    if trade.asset_type == "option":
        pnl = raw_pnl   # Options में leverage नहीं लगता
    else:
        pnl = raw_pnl * trade.leverage

    # Update trade
    trade.status = "closed"
    trade.exit_price = exit_price
    trade.exit_reason = reason
    trade.pnl = pnl
    trade.closed_at = timezone.now()
    trade.save()

    # Return margin + PnL to account
    account.balance += trade.margin_used + pnl
    account.save()

    logger.info(f"✅ Trade closed: {trade.symbol} | PnL: ₹{pnl}")
    return trade


# ─────────────────────────────────────────────
# 🔄 ACCOUNT MANAGEMENT
# ─────────────────────────────────────────────
def reset_account(user, capital: float = 100000):
    """Reset account to initial state"""
    account, _ = PaperAccount.objects.get_or_create(user=user)

    # Close all open trades
    for trade in account.trades.filter(status="open"):
        close_trade(trade.id, trade.current_price or trade.entry_price, "reset")

    # Reset balance
    capital = Decimal(str(capital))
    account.balance = capital
    account.initial_capital = capital
    account.total_withdrawn = Decimal("0")
    account.save()

    logger.info(f"✅ Account reset: {user} | ₹{capital}")
    return account


def topup_account(user, amount: float):
    """Add funds to account"""
    if not amount or amount <= 0:
        raise ValueError("Invalid topup amount")

    account, _ = PaperAccount.objects.get_or_create(user=user)
    amount = Decimal(str(amount))

    account.balance += amount
    account.total_topup += amount
    account.save()

    logger.info(f"✅ Account topped up: {user} | +₹{amount}")
    return account