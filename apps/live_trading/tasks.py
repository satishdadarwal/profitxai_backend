# apps/live_trading/tasks.py

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
#  TASK 1: Signal Detection (Beat se har 15s trigger hota hai)
# ────────────────────────────────────────────────────────────────────
@shared_task(
    bind=True,
    name="live_trading.detect_signals",
    queue="signals",
    max_retries=0,
    soft_time_limit=12,
    time_limit=14,
)
def detect_signals_task(self):
    """
    Saare active TradingSessions scan karo, ICT engine se signals dhundo,
    aur mode ke hisaab se route karo.

    ✅ FIX: Per-session time budget enforce karo.
    Problem: N sessions x scan_sync() sequentially = N x fetch_time.
    2 sessions x ~2s/TF x 4 TFs = 16s — already exceeds 12s soft limit.
    3+ sessions = guaranteed SoftTimeLimitExceeded, signal silently dropped.

    Solution:
    - Task start time track karo
    - Har session ke baad elapsed check karo
    - 9s ke baad remaining sessions skip karo (3s buffer for analysis + overhead)
    - Skipped sessions next 15s cycle mein process honge
    """
    import time as _time
    from .models import TradingSession
    from .signal_handler import SignalHandler

    # ✅ Per-task budget: 12s soft limit mein se 9s sessions ke liye
    # 3s buffer: ICT analysis + DB ops + task overhead
    TASK_BUDGET_SECONDS = 9.0
    task_start = _time.monotonic()

    active_sessions = TradingSession.objects.filter(is_active=True).select_related("user")

    if not active_sessions.exists():
        return {"skipped": "no active sessions"}

    handler = SignalHandler()
    results = []
    skipped = 0

    for session in active_sessions:
        elapsed = _time.monotonic() - task_start

        # ✅ Budget exhausted — baaki sessions agle cycle mein
        if elapsed >= TASK_BUDGET_SECONDS:
            skipped += 1
            logger.warning(
                "detect_signals: budget exhausted (%.1fs >= %.1fs) | "
                "skipping session=%s — will run next cycle",
                elapsed, TASK_BUDGET_SECONDS, session.id,
            )
            continue

        try:
            result = handler.process_session(session)
            results.append(result)
        except Exception as exc:
            logger.error("detect_signals: session %s failed | %s", session.id, exc)

    total_elapsed = round(_time.monotonic() - task_start, 3)
    logger.info(
        "detect_signals: processed=%d skipped=%d elapsed=%.3fs | results=%s",
        len(results), skipped, total_elapsed, results,
    )
    return {"sessions": len(results), "skipped": skipped, "elapsed": total_elapsed}


# ────────────────────────────────────────────────────────────────────
#  TASK 2: Execute Trade (AUTO / SEMI_AUTO mode ke liye)
# ────────────────────────────────────────────────────────────────────
@shared_task(
    bind=True,
    name="live_trading.execute_trade",
    queue="orders",
    max_retries=2,
    default_retry_delay=5,
)
def execute_trade_task(self, signal_id: int, mode: str):
    """
    Signal ko broker ke paas bhejo.
    AUTO mode: immediately.
    SEMI_AUTO mode: user confirmation ke baad call hota hai.

    ✅ FIX #C1: Race condition fix — select_for_update(nowait=True) se sirf ek
    worker signal process kar sakta hai. Pehle 2 workers simultaneously PENDING
    dekh ke dono order place kar dete the.

    ✅ FIX #C2: BrokerOrder CheckConstraint fix — pehle Order create karo,
    phir BrokerOrder mein order FK pass karo. Pehle teeno FKs null the →
    IntegrityError guaranteed tha.
    """
    from apps.brokers.tasks import place_broker_order
    from apps.notifications.tasks import send_notification_task
    from apps.orders.models import Order

    from .models import LiveSignal, TradingMode

    # ── ✅ FIX #C1: Atomic lock — race condition prevention ───────────────────
    # select_for_update(nowait=True):
    #   - nowait=True: agar lock nahi mila toh block nahi karta, exception uthata hai
    #   - Isse retry storm se bachte hain
    #   - Transaction ke andar sirf status change karo — broker call transaction ke bahar
    processing_signal = None

    try:
        with transaction.atomic():
            try:
                signal = (
                    LiveSignal.objects
                    .select_for_update(nowait=True)
                    .select_related("session__user")
                    .get(id=signal_id)
                )
            except LiveSignal.DoesNotExist:
                logger.error("execute_trade: signal %s not found", signal_id)
                return
            except Exception:
                # Lock nahi mila — dusra worker process kar raha hai, silently skip
                logger.warning(
                    "execute_trade: signal %s locked by another worker — skipping",
                    signal_id,
                )
                return

            # Guard: Already acted upon?
            if signal.status not in (
                LiveSignal.Status.PENDING,
                LiveSignal.Status.CONFIRMED,
            ):
                logger.warning(
                    "execute_trade: signal %s already %s — skipping",
                    signal_id, signal.status,
                )
                return

            # Guard: Expired?
            if signal.is_expired():
                signal.mark_expired()
                _log_activity(signal, "expired", "Signal timed out before execution")
                return

            # Status lock karo taaki koi aur worker signal na uthaye
            signal.status = LiveSignal.Status.PROCESSING
            signal.save(update_fields=["status"])
            processing_signal = signal

    except Exception as exc:
        logger.error(
            "execute_trade: DB lock/status update failed | signal=%s | %s",
            signal_id, exc,
        )
        return

    # ── Transaction ke bahar broker call karo ───────────────────────────────
    # Long-running broker API calls kabhi bhi DB transaction ke andar nahi honI chahiye.
    # Agar yahan fail hua toh processing → ignored/retry.
    signal = processing_signal
    user = signal.session.user

    # ── Risk Check ─────────────────────────────────────────────────────────
    from apps.risk.manager import RiskManager

    risk_mgr = RiskManager(user)
    allowed, reason = risk_mgr.can_execute_trade(signal)

    if not allowed:
        logger.warning(
            "❌ Risk blocked | user=%s | signal=%s | reason=%s",
            user.id, signal_id, reason,
        )
        signal.mark_ignored()
        _log_activity(signal, "risk_blocked", reason)
        return

    # ── Broker account dhundo ──────────────────────────────────────────────
    strategy_id = signal.session.strategy_id
    broker_account = _get_broker_account(user, strategy_id)
    if not broker_account:
        logger.error("execute_trade: no active broker for user %s", user.id)
        signal.mark_ignored()
        _log_activity(signal, "failed", "No active broker account found")
        return

    # ── Order + BrokerOrder create karo ───────────────────────────────────
    try:
        from apps.brokers.models import BrokerOrder

        # ✅ FIX #C2: CheckConstraint fix — pehle Order banao, phir BrokerOrder.
        # BrokerOrder model mein exactly ONE of (order/option_trade/paper_trade) required hai.
        # Pehle teeno null the → IntegrityError: CHECK constraint failed.
        order_obj = _get_or_create_order(signal, user, broker_account)

        broker_order = BrokerOrder.objects.create(
            broker_account  = broker_account,
            order           = order_obj,          # ✅ CheckConstraint satisfy hogi
            symbol          = signal.symbol,
            direction       = signal.direction.upper(),
            order_type      = "MARKET",
            quantity        = float(signal.lots),
            price           = float(signal.entry_price),
            stop_loss       = float(signal.stop_loss),
            take_profit     = float(signal.take_profit),
            status          = BrokerOrder.Status.PENDING,
            metadata        = {
                "signal_id":    signal_id,
                "trading_mode": mode,
                "rr_ratio":     float(signal.rr_ratio),
                "session_id":   signal.session_id,
            },
        )

        # Broker ko bhejo
        place_broker_order.apply_async(
            args=[str(broker_order.id)],
            queue="orders",
        )

        signal.mark_executed()
        _log_activity(signal, "executed", f"BrokerOrder {broker_order.id} placed")

        send_notification_task.delay(
            user_id  = user.id,
            channel  = "all",
            title    = f"✅ Trade Executed: {signal.symbol}",
            body     = (
                f"{signal.direction.upper()} @ ₹{signal.entry_price} | "
                f"SL: ₹{signal.stop_loss} | TP: ₹{signal.take_profit} | "
                f"RR: 1:{signal.rr_ratio}"
            ),
            level    = "success",
            category = "trade",
            metadata = {
                "signal_id":    signal_id,
                "mode":         mode,
                "broker_order": str(broker_order.id),
            },
        )

        logger.info(
            "execute_trade: SUCCESS | signal=%s | broker_order=%s | mode=%s",
            signal_id, broker_order.id, mode,
        )

    except Exception as exc:
        logger.error("execute_trade: FAILED | signal=%s | %s", signal_id, exc)

        # Sirf last retry pe mark_ignored karo — pehle karne se retry mein
        # guard trigger hota tha aur error silently dab jaata tha.
        if self.request.retries >= self.max_retries:
            signal.mark_ignored()
            _log_activity(signal, "failed", str(exc))
            send_notification_task.delay(
                user_id  = user.id,
                channel  = "ws",
                title    = f"❌ Trade Failed: {signal.symbol}",
                body     = f"Order placement failed after {self.max_retries} retries: {exc}",
                level    = "error",
                category = "trade",
            )
        else:
            # Retry ke liye PENDING pe wapas le jao — warna guard block karega
            try:
                signal.status = LiveSignal.Status.PENDING
                signal.save(update_fields=["status"])
            except Exception:
                pass
            logger.warning(
                "execute_trade: retry %d/%d | signal=%s",
                self.request.retries + 1, self.max_retries, signal_id,
            )

        raise self.retry(exc=exc)


# ────────────────────────────────────────────────────────────────────
#  TASK 3: Expire Pending SEMI_AUTO Signals (Beat se har 5s)
# ────────────────────────────────────────────────────────────────────
@shared_task(
    name="live_trading.expire_pending_signals",
    queue="signals",
    soft_time_limit=4,
)
def expire_pending_signals_task():
    """
    1. SEMI_AUTO signals jo 60s mein confirm nahi hue — expire karo.
    2. PROCESSING mein 5+ minute se stuck signals — PENDING pe recover karo.
    """
    from apps.notifications.tasks import send_notification_task

    from .models import LiveSignal, TradingMode

    now = timezone.now()

    # ── 1. Normal SEMI_AUTO expiry ──────────────────────────────
    expired_qs = LiveSignal.objects.filter(
        status=LiveSignal.Status.PENDING,
        expires_at__lte=now,
        mode=TradingMode.SEMI_AUTO,
    ).select_related("session__user")

    expired_count = 0
    for signal in expired_qs:
        signal.mark_expired()
        _log_activity(signal, "expired", "60s timeout — no user confirmation")
        send_notification_task.delay(
            user_id  = signal.session.user_id,
            channel  = "ws",
            title    = f"⏰ Signal Expired: {signal.symbol}",
            body     = f"{signal.direction.upper()} signal expired (no action in 60s)",
            level    = "warning",
            category = "trade",
            metadata = {"signal_id": signal.id, "status": "expired"},
        )
        expired_count += 1

    # ── 2. Stuck PROCESSING recovery ────────────────────────────
    # Agar worker crash hua toh signal "processing" mein reh jaata hai.
    # 5 minute baad wapas PENDING pe le jao aur re-queue karo.
    stuck_cutoff = now - timedelta(minutes=5)
    stuck_qs = LiveSignal.objects.filter(
        status=LiveSignal.Status.PROCESSING,
        detected_at__lte=stuck_cutoff,
    )

    stuck_count = 0
    for signal in stuck_qs:
        logger.warning(
            "expire_pending_signals: recovering stuck signal %s (>5min in processing)",
            signal.id,
        )
        signal.status = LiveSignal.Status.PENDING
        signal.save(update_fields=["status"])
        execute_trade_task.apply_async(args=[signal.id, signal.mode], queue="orders")
        stuck_count += 1

    if expired_count or stuck_count:
        logger.info(
            "expire_pending_signals: expired=%d | recovered_stuck=%d",
            expired_count, stuck_count,
        )
    return {"expired": expired_count, "recovered_stuck": stuck_count}


# ────────────────────────────────────────────────────────────────────
#  TASK 4: Manual Order Place (MANUAL mode FAB se)
# ────────────────────────────────────────────────────────────────────
@shared_task(
    bind=True,
    name="live_trading.manual_order_place",
    queue="orders",
    max_retries=2,
    default_retry_delay=5,
)
def manual_order_place_task(self, manual_order_id: int):
    """
    MANUAL mode: user ne FAB se order place kiya.
    ManualOrder → Order → BrokerOrder → place_broker_order
    """
    from apps.brokers.tasks import place_broker_order
    from apps.notifications.tasks import send_notification_task

    from .models import ManualOrder

    try:
        mo = ManualOrder.objects.select_related("session__user").get(id=manual_order_id)
    except ManualOrder.DoesNotExist:
        logger.error("manual_order_place: ManualOrder %s not found", manual_order_id)
        return

    if mo.status != ManualOrder.Status.DRAFT:
        logger.warning("manual_order_place: order %s already %s", manual_order_id, mo.status)
        return

    try:
        from apps.brokers.models import BrokerOrder
        from apps.orders.models import Order

        broker_account = _get_broker_account(mo.session.user, mo.session.strategy_id)
        if not broker_account:
            raise ValueError("No active broker account")

        # ✅ FIX: Order field names corrected (same as _get_or_create_order)
        order_obj = Order.objects.create(
            user           = mo.session.user,
            asset          = _get_or_create_asset(mo.symbol),
            side           = mo.direction,
            order_type     = mo.order_type.lower(),
            quantity       = mo.lots,
            limit_price    = mo.price,          # ✅ FIX: was `price`
            sl_price       = mo.stop_loss,       # ✅ FIX: was `stop_loss`
            target_price   = mo.take_profit,     # ✅ FIX: was `take_profit`
            status         = Order.Status.OPEN,
            mode           = Order.Mode.LIVE,
            broker_account = broker_account,
        )

        broker_order = BrokerOrder.objects.create(
            broker_account  = broker_account,
            order           = order_obj,          # ✅ CheckConstraint satisfy
            symbol          = mo.symbol,
            direction       = mo.direction.upper(),
            order_type      = mo.order_type,
            quantity        = float(mo.lots),
            price           = float(mo.price) if mo.price else None,
            stop_loss       = float(mo.stop_loss) if mo.stop_loss else None,
            take_profit     = float(mo.take_profit) if mo.take_profit else None,
            status          = BrokerOrder.Status.PENDING,
            metadata        = {
                "manual_order_id": manual_order_id,
                "trading_mode":    "manual",
            },
        )

        place_broker_order.apply_async(args=[str(broker_order.id)], queue="orders")

        mo.status          = ManualOrder.Status.PLACED
        mo.placed_at       = timezone.now()
        mo.broker_order_id = str(broker_order.id)
        mo.save(update_fields=["status", "placed_at", "broker_order_id"])

        send_notification_task.delay(
            user_id  = mo.session.user_id,
            channel  = "all",
            title    = f"📋 Manual Order Placed: {mo.symbol}",
            body     = (
                f"{mo.direction.upper()} {mo.lots} lots @ "
                f"{mo.price or 'MARKET'} | RR: 1:{mo.rr_ratio}"
            ),
            level    = "success",
            category = "trade",
            metadata = {"manual_order_id": manual_order_id},
        )

    except Exception as exc:
        mo.status = ManualOrder.Status.REJECTED
        mo.save(update_fields=["status"])
        logger.error("manual_order_place: FAILED | %s | %s", manual_order_id, exc)

        send_notification_task.delay(
            user_id  = mo.session.user_id,
            channel  = "ws",
            title    = f"❌ Manual Order Failed: {mo.symbol}",
            body     = str(exc),
            level    = "error",
            category = "trade",
        )
        raise self.retry(exc=exc)


# ────────────────────────────────────────────────────────────────────
#  TASK 5: Session Summary (algo stop ke baad)
# ────────────────────────────────────────────────────────────────────
@shared_task(
    name="live_trading.close_session_summary",
    queue="default",
)
def close_session_summary_task(session_id: int):
    """
    Algo stop hone par call karo. PnL, win rate, drawdown calculate karo
    aur Flutter ko WebSocket se bhejo.
    """
    from apps.notifications.tasks import send_notification_task

    from .models import LiveSignal, TradingSession

    try:
        session = TradingSession.objects.get(id=session_id)
    except TradingSession.DoesNotExist:
        logger.error("close_session_summary: session %s not found", session_id)
        return

    executed_signals = LiveSignal.objects.filter(
        session=session, status=LiveSignal.Status.EXECUTED
    )
    total = executed_signals.count()

    # ── PnL: BrokerOrder.realized_pnl se lo ─────────────────────────────────
    # BUY (entry) orders: realized_pnl = None — isliye exclude karte hain.
    # SELL (exit) orders: realized_pnl = net INR PnL after fees.
    # metadata mein session_id store hota hai execute_trade_task mein.
    from apps.brokers.models import BrokerOrder

    fills = (
        BrokerOrder.objects.filter(
            metadata__session_id=session_id,
            status=BrokerOrder.Status.COMPLETE,
        )
        .exclude(realized_pnl=None)
        .values_list("realized_pnl", flat=True)
    )

    parsed_pnl = []
    for p in fills:
        try:
            parsed_pnl.append(float(p))
        except (TypeError, ValueError):
            logger.warning(
                "close_session_summary: invalid pnl value=%s | session=%s", p, session_id
            )

    total_pnl      = sum(parsed_pnl)
    wins           = sum(1 for p in parsed_pnl if p > 0)
    peak, drawdown = _calc_drawdown(parsed_pnl)

    session.total_trades   = total
    session.winning_trades = wins
    session.total_pnl      = total_pnl
    session.max_drawdown   = drawdown
    session.peak_equity    = peak
    session.close()

    send_notification_task.delay(
        user_id  = session.user_id,
        channel  = "all",
        title    = "📊 Session Summary",
        body     = (
            f"Trades: {total} | PnL: ₹{total_pnl:.0f} | "
            f"Win Rate: {session.win_rate:.0f}% | Drawdown: ₹{drawdown:.0f}"
        ),
        level    = "info",
        category = "trade",
        metadata = {
            "session_id":   session_id,
            "total_trades": total,
            "total_pnl":    total_pnl,
            "win_rate":     session.win_rate,
            "max_drawdown": float(drawdown),
            "peak_equity":  float(peak),
            "type":         "session_summary",
        },
    )

    logger.info(
        "close_session_summary: session=%s | trades=%d | pnl=%.2f | wr=%.1f%%",
        session_id, total, total_pnl, session.win_rate,
    )


# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────
def _get_broker_account(user, strategy_id: str):
    """
    User ke active, verified broker account dhundo — strategy ke saath strictly bound.

    ✅ FIX (BLOCKER #2 — Multiple Account Wallet Isolation):
    ─────────────────────────────────────────────────────────
    PEHLE (bug):
        specific = qs.filter(strategies__id=strategy_id).first()
        return specific or qs.first()   ← PROBLEM

        "qs.first()" fallback:
        - Symbol overlap hone pe (e.g. NIFTY User-A aur User-B dono ke strategy mein)
          wrong broker account se order ja sakta tha.
        - Multi-user setup mein User-B ka order User-A ke broker account se place ho
          sakta tha — capital/wallet completely mixed up.

    AB (fixed):
        - Strategy.broker FK directly check karo — yahi ground truth hai.
        - Agar strategy pe broker assign nahi hai → explicit error.
        - Agar broker account is_active=False ya is_verified=False hai → explicit error.
        - Silent qs.first() fallback HATA DIYA — ambiguity zero.

    Testing guidance:
        - Pehle sirf ek account (apna) se test karo.
        - Multiple users ke liye: har Strategy ka `broker` FK alag BrokerAccount
          pe point karna chahiye. Overlap check: do strategies same symbol trade
          kar rahi hain? Tab har ek ka broker explicitly alag hona chahiye.
    """
    from apps.strategies.models import Strategy

    try:
        strategy = Strategy.objects.select_related("broker").get(
            id=strategy_id,
            user=user,  # ← ownership guard: dusre user ki strategy nahi
        )
    except Strategy.DoesNotExist:
        logger.error(
            "_get_broker_account: strategy %s not found or does not belong to user %s",
            strategy_id, user.id,
        )
        return None

    account = strategy.broker

    if account is None:
        logger.error(
            "_get_broker_account: strategy %s has no broker assigned | user=%s | "
            "Fix: Admin panel ya Flutter mein strategy ke saath broker account bind karo.",
            strategy_id, user.id,
        )
        return None

    if not account.is_active:
        logger.error(
            "_get_broker_account: broker account %s (strategy=%s) is_active=False | user=%s",
            account.id, strategy_id, user.id,
        )
        return None

    if not account.is_verified:
        logger.error(
            "_get_broker_account: broker account %s (strategy=%s) is_verified=False | user=%s | "
            "Token refresh required.",
            account.id, strategy_id, user.id,
        )
        return None

    # Ownership double-check: strategy.user aur broker account.user same hone chahiye
    if account.user_id != user.id:
        logger.critical(
            "🚨 SECURITY: broker account %s belongs to user %s but strategy %s is for user %s — "
            "BLOCKING order to prevent cross-user capital leak.",
            account.id, account.user_id, strategy_id, user.id,
        )
        return None

    return account


def _get_or_create_order(signal, user, broker_account):
    """
    ✅ FIX: Order field names corrected.
    Order model: limit_price, sl_price, target_price
    Pehle:       price,       stop_loss, take_profit  → TypeError
    """
    from apps.orders.models import Order

    return Order.objects.create(
        user           = user,
        asset          = _get_or_create_asset(signal.symbol),
        side           = signal.direction,
        order_type     = "market",
        quantity       = signal.lots,
        limit_price    = signal.entry_price,    # ✅ FIX: was `price`
        sl_price       = signal.stop_loss,       # ✅ FIX: was `stop_loss`
        target_price   = signal.take_profit,     # ✅ FIX: was `take_profit`
        status         = Order.Status.OPEN,
        mode           = Order.Mode.LIVE,
        broker_account = broker_account,
    )


def _get_or_create_asset(symbol: str):
    """
    ✅ FIX: Crypto symbols ke liye asset_type="crypto", Indian ke liye "equity".
    Pehle sab "equity" tha → Delta Exchange crypto assets galat classify hote the.
    """
    try:
        from apps.market.models import Asset

        _CRYPTO_KW = {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "USDT"}
        upper     = symbol.upper()
        is_crypto = any(kw in upper for kw in _CRYPTO_KW) or upper.startswith("DELTA:")

        asset, _ = Asset.objects.get_or_create(
            symbol=symbol,
            defaults={
                "name":       symbol,
                "asset_type": "crypto" if is_crypto else "equity",  # ✅ FIX
            },
        )
        return asset
    except Exception as exc:
        logger.error("_get_or_create_asset: %s | %s", symbol, exc)
        raise


def _log_activity(signal, status: str, note: str = ""):
    from .models import ActivityLog

    ActivityLog.objects.create(
        session     = signal.session,
        signal      = signal,
        user        = signal.session.user,
        status      = status,
        mode        = signal.mode,
        symbol      = signal.symbol,
        direction   = signal.direction,
        entry_price = signal.entry_price,
        note        = note,
        metadata    = {"signal_id": signal.id, "rr_ratio": float(signal.rr_ratio)},
    )


def _calc_drawdown(pnl_list: list) -> tuple[float, float]:
    """Peak equity aur max drawdown calculate karo."""
    if not pnl_list:
        return 0.0, 0.0
    equity = 0.0
    peak   = 0.0
    max_dd = 0.0
    for p in pnl_list:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return peak, max_dd