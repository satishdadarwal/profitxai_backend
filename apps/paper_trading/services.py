import logging
from decimal import Decimal
from django.utils import timezone
from .models import PaperAccount, normalize_symbol, get_lot_size

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
    """
    instr = (instrument_type or "").strip().lower()
    if instr == "options":
        return "option"
    if instr in ("futures", "perp"):
        return "futures"
    if instr == "equity":
        return "equity"
    if instr == "crypto":
        return "crypto"

    raw = (raw_asset or "").strip().lower()
    if raw in ("option", "options"):
        return "option"
    if raw == "crypto":
        return "crypto"
    if raw in ("futures", "perp"):
        return "futures"
    if raw == "equity":
        return "equity"

    symbol_upper = symbol.upper()
    if any(k in symbol_upper for k in _OPTION_KEYWORDS):
        return "option"
    if any(k in symbol_upper for k in _CRYPTO_KEYWORDS):
        return "crypto"

    if raw:
        return raw

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
    quantity: Decimal = None,          # Direct qty
    risk_amount: Decimal = None,       # Fixed risk amount
    risk_pct: Decimal = None,          # Risk % of balance
    lot_size_override: int = None,     # For manual lot size
) -> dict:
    """
    Hybrid position sizing calculator. Returns dict with quantity/lot_size/margin/max_loss.
    """
    if lot_size_override:
        lot_size = lot_size_override
    else:
        lot_size = get_lot_size(symbol, asset_type)

    if quantity is not None:
        qty = Decimal(str(quantity))
        if asset_type == "option":
            margin = entry_price * lot_size * qty
        elif asset_type == "futures":
            margin = (entry_price * lot_size * qty) / leverage
        else:
            margin = (entry_price * qty) / leverage
        max_loss = calculate_max_loss(entry_price, stop_loss, qty, lot_size, side, leverage)
        return {"quantity": qty, "lot_size": lot_size, "margin": margin, "max_loss": max_loss}

    if risk_amount is not None:
        risk = Decimal(str(risk_amount))
    elif risk_pct is not None:
        risk = account.balance * (Decimal(str(risk_pct)) / 100)
    else:
        risk = account.balance * (account.risk_per_trade_pct / 100)

    logger.info(f"Risk amount: {risk}")

    if stop_loss and entry_price:
        risk_per_unit = abs(entry_price - Decimal(str(stop_loss)))
        if risk_per_unit > 0:
            if asset_type == "option":
                qty = risk / (risk_per_unit * lot_size * leverage)
            elif asset_type == "futures":
                qty = risk / (risk_per_unit * lot_size * leverage)
            else:
                qty = risk / (risk_per_unit * leverage)
            qty = qty.quantize(Decimal("0.0001"))
        else:
            raise ValueError("Stop loss too close to entry price")
    else:
        if asset_type == "option":
            qty = risk / (entry_price * lot_size)
        elif asset_type == "futures":
            qty = risk / (entry_price * lot_size)
        else:
            qty = risk / entry_price
        qty = qty.quantize(Decimal("0.0001"))

    if asset_type == "option":
        margin = entry_price * lot_size * qty
    elif asset_type == "futures":
        margin = (entry_price * lot_size * qty) / leverage
    else:
        margin = (entry_price * qty) / leverage

    max_loss = calculate_max_loss(entry_price, stop_loss, qty, lot_size, side, leverage)

    return {"quantity": qty, "lot_size": lot_size, "margin": margin, "max_loss": max_loss}


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
    return risk_per_unit * total_qty * leverage


# ─────────────────────────────────────────────
# 📈 OPEN TRADE
# ─────────────────────────────────────────────
def open_trade(user, data: dict):
    """
    Open a new paper trade (creates an Order with mode=paper).
    """
    from apps.orders.models import Order, Asset

    account, _ = PaperAccount.objects.get_or_create(user=user)

    symbol = normalize_symbol(data.get("symbol", ""))
    raw_asset = data.get("asset_type", "")
    instrument_type = data.get("instrument_type", "")
    asset_type = detect_asset_type(raw_asset, symbol, instrument_type)

    entry_price = Decimal(str(data["entry_price"]))
    side = data.get("side", "buy").lower()
    leverage = int(data.get("leverage", 1))
    stop_loss = Decimal(str(data["stop_loss"])) if data.get("stop_loss") else None
    target_price = Decimal(str(data["target_price"])) if data.get("target_price") else None

    logger.info(f"Asset type detected: '{raw_asset}' -> '{asset_type}' for symbol '{symbol}'")

    # Validate (without position size first)
    can_trade, reason = account.can_open_new_trade(asset_type=asset_type, leverage=leverage)
    if not can_trade:
        raise ValueError(reason)

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

    # Final validation with position size
    can_trade, reason = account.can_open_new_trade(
        asset_type=asset_type,
        position_size=margin,
        leverage=leverage
    )
    if not can_trade:
        raise ValueError(reason)

    max_risk_pct = Decimal(str(getattr(account, "max_risk_per_trade_pct", None) or "2.0"))
    allowed_loss = account.balance * (max_risk_pct / 100)
    if max_loss > Decimal("0") and max_loss > allowed_loss:
        raise ValueError(
            f"Risk too high. Max allowed: {allowed_loss:.2f} ({max_risk_pct}% of balance), "
            f"Your risk: {max_loss:.2f}"
        )

    if account.available_balance < margin:
        raise ValueError(
            f"Insufficient balance. Need {margin:.2f}, Available {account.available_balance:.2f}"
        )

    # Map asset_type to Order.InstrumentType
    instrument_map = {
        "option": Order.InstrumentType.OPTIONS,
        "options": Order.InstrumentType.OPTIONS,
        "futures": Order.InstrumentType.FUTURES,
        "crypto": Order.InstrumentType.FUTURES,
        "equity": Order.InstrumentType.EQUITY,
    }
    order_instrument_type = instrument_map.get(asset_type, Order.InstrumentType.OPTIONS)

    # Get or create asset
    asset, _ = Asset.objects.get_or_create(
        symbol=symbol,
        defaults={
            "name": data.get("display_name", symbol),
            "instrument_type": order_instrument_type,
        },
    )

    # Map side
    order_side = Order.Side.BUY if side in ("buy", "long") else Order.Side.SELL

    metadata = {
        "setup_type": data.get("setup_type", ""),
        "strategy_id": data.get("strategy_id", ""),
        "nifty_spot_at_entry": data.get("nifty_spot_at_entry", 0),
        "strike_price": data.get("strike_price"),
        "leverage": leverage,
    }

    order = Order.objects.create(
        user=user,
        asset=asset,
        mode=Order.Mode.PAPER,
        instrument_type=order_instrument_type,
        side=order_side,
        quantity=int(quantity * lot_size),
        lots=int(quantity),
        entry_price=entry_price,
        current_price=entry_price,
        sl_price=stop_loss,
        target_price=target_price,
        option_type=data.get("option_type", ""),
        symbol_display=data.get("display_name", symbol),
        status=Order.Status.OPEN,
        entry_time=timezone.now(),
        metadata=metadata,
    )

    # Deduct margin from paper account balance
    account.balance -= margin
    account.save()

    logger.info(f"Trade opened: {symbol} {side} {quantity} @ {entry_price}")
    return order


# ─────────────────────────────────────────────
# 📉 CLOSE TRADE
# ─────────────────────────────────────────────
def close_trade(trade_id: str, exit_price: Decimal, reason: str = "manual"):
    """Close a paper trade Order and update balance."""
    from apps.orders.models import Order

    order = Order.objects.get(id=trade_id, mode=Order.Mode.PAPER)

    if order.status == Order.Status.FILLED:
        logger.warning(f"Order {trade_id} already closed")
        return order

    account, _ = PaperAccount.objects.get_or_create(user=order.user)

    entry = Decimal(str(order.entry_price or 0))
    qty = Decimal(str(order.quantity or 0))
    xp = Decimal(str(exit_price))

    if order.side == Order.Side.BUY:
        pnl = (xp - entry) * qty
    else:
        pnl = (entry - xp) * qty

    order.status = Order.Status.FILLED
    order.exit_price = xp
    order.exit_reason = reason
    order.realized_pnl = pnl.quantize(Decimal("0.01"))
    order.exit_time = timezone.now()
    order.save(update_fields=["status", "exit_price", "exit_reason", "realized_pnl", "exit_time", "updated_at"])

    # Return margin + PnL to account (margin = entry * qty)
    margin = entry * qty
    account.balance += margin + pnl
    account.save()

    logger.info(f"Trade closed: {order.symbol_display} | PnL: {pnl}")
    return order


# ─────────────────────────────────────────────
# 🔄 ACCOUNT MANAGEMENT
# ─────────────────────────────────────────────
def reset_account(user, capital: float = 100000):
    """Reset account to initial state"""
    from apps.orders.models import Order

    account, _ = PaperAccount.objects.get_or_create(user=user)

    # Close all open paper orders
    open_orders = Order.objects.filter(user=user, mode=Order.Mode.PAPER, status=Order.Status.OPEN)
    for order in open_orders:
        ep = order.current_price or order.entry_price or Decimal("0")
        close_trade(str(order.id), Decimal(str(ep)), "reset")

    capital = Decimal(str(capital))
    account.balance = capital
    account.initial_capital = capital
    account.total_withdrawn = Decimal("0")
    account.save()

    logger.info(f"Account reset: {user} | {capital}")
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

    logger.info(f"Account topped up: {user} | +{amount}")
    return account
