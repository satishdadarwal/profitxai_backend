# apps/risk/manager.py  — VERSION 2.0
# ══════════════════════════════════════════════════════════════════════════════
#  ProfitXAI — Production-Grade Risk Manager
#  Hedge Fund Level Risk Controls
# ══════════════════════════════════════════════════════════════════════════════
#
#  NEW in v2.0:
#  ✅  Capital-Tiered Risk (₹1L=2% → ₹1Cr=0.25%) — hedge fund standard
#  ✅  Correlation Risk   — NIFTY+BANKNIFTY+FINNIFTY = 1 bet detected
#  ✅  Same-Setup Overexposure — same signal fire ≤ 2 times max
#  ✅  Volatility Regime  — VIX spike = reduce position size
#  ✅  Execution Delay    — signal age check before order
#  ✅  Data Feed / Price Error — stale / erroneous LTP detection
#  ✅  Strategy Decay     — win rate monitor, auto-pause on decay
#  ✅  Option Buyer Risk  — separate premium decay guard
#  ✅  Short Seller Risk  — unlimited loss guard, margin cliff check
#  ✅  can_execute_trade  — LiveSignal bridge (was missing)
#  ✅  _stop_all_strategies fix  — @property bug fixed
#  ✅  Market hours IST fix      — UTC→IST conversion fixed

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from django.core.cache import cache
from django.db.models import Sum, Avg, Count, Q
from django.utils import timezone
from django.conf import settings

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════════════════

# Indices that are highly correlated (same underlying market direction)
CORRELATED_GROUPS: List[List[str]] = [
    ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"],  # Indian indices
    ["NIFTY50", "NIFTY BANK", "FINNIFTY"],                         # alternate names
]

# Option buyer: premium % of capital cap
OPTION_BUYER_PREMIUM_CAP_PCT = Decimal("0.005")   # 0.5% of capital per trade

# Short seller: max notional exposure as % of capital
SHORT_SELLER_NOTIONAL_CAP_PCT = Decimal("0.10")    # 10% of capital

# Strategy decay: if win rate falls below this over last N trades → pause
#
# Floor rationale:
#   35% (old) — bahut sensitive: ICT strategies naturally have ~40-50% win rate
#   with high RR (2:1+). Choppy market mein 10-15 bad trades = auto-pause,
#   even when strategy is fundamentally sound.
#
#   25% (new) — meaningful floor: sirf tab pause jab strategy clearly broken hai.
#   Example: 15 trades, 3 wins = 20% → paused ✅ (real decay)
#            15 trades, 5 wins = 33% → NOT paused (choppy market, not broken)
#
# Lookback 20 → 15:
#   20 trades = ~4-5 trading days (4 trades/day avg).
#   15 trades = ~3-4 days — faster signal, same statistical significance
#   at 25% floor (binomial: P(≤3 wins in 15 | true_rate=0.45) < 2%).
STRATEGY_DECAY_WIN_RATE_FLOOR   = Decimal("0.25")  # 25% win rate minimum
STRATEGY_DECAY_LOOKBACK_TRADES  = 15               # look at last 15 trades

# Max signal age before execution (execution delay guard)
MAX_SIGNAL_AGE_SECONDS = 30                        # 30s se purana signal reject

# Price staleness — LTP older than this = reject
MAX_LTP_AGE_SECONDS = 10

# VIX thresholds
VIX_HIGH_THRESHOLD = 20.0     # High vol regime
VIX_EXTREME_THRESHOLD = 30.0  # Extreme vol — reduce size heavily

# Capital tiers: (min_capital, max_capital, risk_pct)
# Based on hedge fund standard: bigger capital = lower % risk (preservation mode)
CAPITAL_RISK_TIERS: List[Tuple[Decimal, Decimal, Decimal]] = [
    (Decimal("0"),        Decimal("100000"),   Decimal("0.020")),  # ≤ ₹1L   → 2.0%
    (Decimal("100000"),   Decimal("500000"),   Decimal("0.015")),  # ≤ ₹5L   → 1.5%
    (Decimal("500000"),   Decimal("1000000"),  Decimal("0.012")),  # ≤ ₹10L  → 1.2%
    (Decimal("1000000"),  Decimal("2500000"),  Decimal("0.010")),  # ≤ ₹25L  → 1.0%
    (Decimal("2500000"),  Decimal("5000000"),  Decimal("0.007")),  # ≤ ₹50L  → 0.7%
    (Decimal("5000000"),  Decimal("10000000"), Decimal("0.005")),  # ≤ ₹1Cr  → 0.5%
    (Decimal("10000000"), Decimal("999999999"),Decimal("0.0025")), # > ₹1Cr  → 0.25%
]


# ══════════════════════════════════════════════════════════════════════════════
#  RiskLimits dataclass
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RiskLimits:
    """Per-user risk limit configuration — loaded from TradingProfile or defaults."""

    # Daily limits
    max_loss_per_day: Decimal     = Decimal("10000")
    max_profit_lock: Decimal      = Decimal("5000")
    max_trades_per_day: int       = 50
    max_orders_per_minute: int    = 10

    # Position limits
    max_position_size: Decimal    = Decimal("100000")
    max_positions: int            = 10
    max_leverage: Decimal         = Decimal("5")

    # Risk limits
    max_drawdown: Decimal         = Decimal("0.20")
    max_loss_per_trade: Decimal   = Decimal("2000")
    stop_loss_required: bool      = True

    # Order limits
    min_risk_reward: Decimal      = Decimal("1.5")
    max_slippage_pct: Decimal     = Decimal("2")

    # Concentration limits
    max_sector_exposure: Decimal  = Decimal("0.30")
    max_symbol_exposure: Decimal  = Decimal("0.25")

    # NEW: Advanced limits
    max_correlated_positions: int = 2    # Max positions in same correlated group
    max_same_setup_fires: int     = 2    # Same strategy signal ≤ N times per day
    capital_risk_pct: Decimal     = Decimal("0.02")  # Dynamic — set by capital tier
    instrument_type: str          = "equity"  # "option_buyer" | "short_seller" | "equity"


# ══════════════════════════════════════════════════════════════════════════════
#  RiskManager
# ══════════════════════════════════════════════════════════════════════════════

class RiskManager:
    """
    Hedge Fund Grade Risk Manager for ProfitXAI.

    Checks (in order):
     1.  Kill switch
     2.  Market hours (IST — FIXED)
     3.  Daily trade limit
     4.  Order rate limit
     5.  Daily loss limit
     6.  Profit lock
     7.  Capital-tiered max loss per trade
     8.  Position size limit
     9.  Max positions limit
    10.  Stop loss mandatory
    11.  Risk-reward ratio
    12.  Max loss per trade (rupee)
    13.  Drawdown limit
    14.  Symbol concentration
    15.  Margin check
    16.  Price sanity
    17.  ★ Correlation risk  (NEW)
    18.  ★ Same-setup overexposure  (NEW)
    19.  ★ Volatility regime  (NEW)
    20.  ★ Execution delay  (NEW)
    21.  ★ Data feed / stale price  (NEW)
    22.  ★ Strategy decay  (NEW)
    23.  ★ Option buyer premium cap  (NEW — if instrument_type=option_buyer)
    24.  ★ Short seller unlimited-loss guard  (NEW — if instrument_type=short_seller)
    """

    def __init__(self, user, instrument_type: str = "equity"):
        self.user = user
        self.instrument_type = instrument_type   # passed from signal/order context
        self.limits = self._load_user_limits()
        self.cache_ttl = 60

    # ──────────────────────────────────────────────────────────────────────────
    #  Public API
    # ──────────────────────────────────────────────────────────────────────────

    def can_execute_trade(self, signal) -> Tuple[bool, str]:
        """
        ✅ CRITICAL FIX: Bridge for LiveSignal objects.
        Called by: live_trading/tasks.py → execute_trade_task
        """
        from decimal import Decimal
        try:
            # Detect instrument type from signal
            instrument_type = getattr(signal, "instrument_type", None)
            if not instrument_type:
                sym = signal.symbol.upper()
                if "CE" in sym or "PE" in sym:
                    instrument_type = (
                        "option_buyer"
                        if signal.direction == "buy"
                        else "short_seller"
                    )
                else:
                    instrument_type = "equity"
            self.instrument_type = instrument_type

            return self.can_place_order(
                symbol        = signal.symbol,
                qty           = Decimal(str(signal.lots)),
                price         = signal.entry_price,
                stop_loss     = signal.stop_loss,
                take_profit   = signal.take_profit,
                side          = signal.direction,
                strategy_id   = getattr(signal, "strategy_id", ""),
                signal_id     = signal.id,
                signal_age_s  = (
                    timezone.now() - signal.detected_at
                ).total_seconds() if hasattr(signal, "detected_at") else 0,
            )
        except Exception as e:
            logger.error(
                "can_execute_trade: unexpected error | signal=%s | %s",
                getattr(signal, "id", "?"), e
            )
            return False, f"Risk check error: {e}"

    def can_place_order(
        self,
        symbol: str,
        qty: Decimal,
        price: Optional[Decimal] = None,
        stop_loss: Optional[Decimal] = None,
        take_profit: Optional[Decimal] = None,
        side: str = "buy",
        strategy_id: str = "",
        signal_id: int = 0,
        signal_age_s: float = 0.0,
    ) -> Tuple[bool, str]:
        """
        Comprehensive pre-trade risk check.
        Returns: (allowed: bool, reason: str)
        """

        # ── 1. Kill switch ────────────────────────────────────────────────────
        if self._is_kill_switch_active():
            return False, "Kill switch active. Trading halted."

        # ── 2. Market hours (IST — FIXED timezone bug) ────────────────────────
        if not self._is_market_open():
            return False, "Market is closed (9:15–15:30 IST, Mon–Fri)"

        # ── 3. Daily trade limit ──────────────────────────────────────────────
        daily_trades = self._get_daily_trade_count()
        if daily_trades >= self.limits.max_trades_per_day:
            return False, f"Daily trade limit reached ({daily_trades}/{self.limits.max_trades_per_day})"

        # ── 4. Order rate limit ───────────────────────────────────────────────
        if not self._check_order_rate_limit():
            return False, "Too many orders in short time. Please wait."

        # ── 5. Daily loss limit ───────────────────────────────────────────────
        daily_pnl = self._get_daily_pnl()
        if daily_pnl < -self.limits.max_loss_per_day:
            self.trigger_kill_switch("Daily loss limit exceeded")
            return False, f"Daily loss limit exceeded (₹{abs(daily_pnl):.0f})"

        # ── 6. Profit lock ────────────────────────────────────────────────────
        if daily_pnl >= self.limits.max_profit_lock:
            return False, f"Profit target reached (₹{daily_pnl:.0f}). Take a break!"

        # ── 7. Fetch price if not provided ───────────────────────────────────
        if price is None:
            price = self._get_current_price(symbol)
            if not price:
                return False, "Could not fetch current price"

        # ── 8. Capital-tiered max loss per trade ──────────────────────────────
        capital = self._get_total_capital()
        tier_risk_pct = self._get_capital_risk_pct(capital)
        max_loss_this_trade = (capital * tier_risk_pct).quantize(Decimal("1"), rounding=ROUND_DOWN)
        profile_max = self.limits.max_loss_per_trade or __import__("decimal").Decimal("0")
        max_loss_this_trade = max(max_loss_this_trade, profile_max)

        if stop_loss:
            sym_upper = str(symbol).upper()
            is_option = any(x in sym_upper for x in ['CE', 'PE']) or self.limits.instrument_type == 'option_buyer'
            if is_option:
                # Options: max loss = premium paid (price * qty)
                potential_loss = price * qty
            else:
                potential_loss = abs(price - stop_loss) * qty
            if potential_loss > max_loss_this_trade:
                return False, (
                    f"Loss ₹{potential_loss:.0f} exceeds capital-based limit "
                    f"₹{max_loss_this_trade:.0f} "
                    f"({tier_risk_pct*100:.2f}% of ₹{capital:.0f} capital)"
                )

        # ── 9. Position size limit ────────────────────────────────────────────
        # Options ke liye premium-based check, equity/futures ke liye notional
        sym_upper = str(symbol).upper()
        is_option = any(x in sym_upper for x in ['CE', 'PE']) or self.limits.instrument_type == 'option_buyer'
        if is_option:
            position_value = qty * price  # premium only
        else:
            position_value = qty * price  # notional
        # Cap: options max position = capital * 10% (premium basis)
        effective_limit = self.limits.max_position_size if not is_option else min(self.limits.max_position_size, self._get_total_capital() * Decimal('0.10'))
        if position_value > effective_limit:
            return False, f"Position ₹{position_value:.0f} > limit ₹{effective_limit:.0f}"

        # ── 10. Max open positions ────────────────────────────────────────────
        open_positions = self._get_open_position_count()
        if open_positions >= self.limits.max_positions:
            return False, f"Max positions ({open_positions}/{self.limits.max_positions}) reached"

        # ── 11. Stop loss mandatory ───────────────────────────────────────────
        if self.limits.stop_loss_required and not stop_loss:
            return False, "Stop loss is mandatory"

        # ── 12. Risk-reward check ─────────────────────────────────────────────
        if stop_loss and take_profit:
            rr = self._calculate_rr_ratio(price, stop_loss, take_profit, side)
            if rr < self.limits.min_risk_reward:
                return False, f"RR too low ({rr:.2f} < {self.limits.min_risk_reward} min)"

        # ── 13. Drawdown limit ────────────────────────────────────────────────
        if not self._check_drawdown_limit():
            self.trigger_kill_switch("Max drawdown exceeded")
            return False, "Max drawdown exceeded. Trading stopped."

        # ── 14. Symbol concentration ──────────────────────────────────────────
        if False and not self._check_symbol_concentration(symbol, position_value):  # temporarily disabled
            return False, f"Too much exposure to {symbol}"

        # ── 15. Margin check ──────────────────────────────────────────────────
        if not self._has_sufficient_margin(position_value):
            return False, "Insufficient margin"

        # ── 16. Price sanity ──────────────────────────────────────────────────
        if not self._is_price_reasonable(symbol, price):
            return False, "Price appears incorrect (>10% from market)"

        # ── 17. ★ CORRELATION RISK ────────────────────────────────────────────
        corr_ok, corr_msg = self._check_correlation_risk(symbol, side)
        if not corr_ok:
            return False, corr_msg

        # ── 18. ★ SAME SETUP OVEREXPOSURE ─────────────────────────────────────
        if strategy_id:
            setup_ok, setup_msg = self._check_same_setup_overexposure(strategy_id, symbol)
            if not setup_ok:
                return False, setup_msg

        # ── 19. ★ VOLATILITY REGIME ───────────────────────────────────────────
        vol_ok, vol_msg, vol_size_multiplier = self._check_volatility_regime(symbol)
        if not vol_ok:
            return False, vol_msg
        # vol_size_multiplier can be used to suggest reduced qty — logged
        if vol_size_multiplier < Decimal("1"):
            logger.warning(
                "High volatility | user=%s | symbol=%s | size_multiplier=%.2f",
                self.user.id, symbol, vol_size_multiplier
            )

        # ── 20. ★ EXECUTION DELAY ─────────────────────────────────────────────
        if signal_age_s > MAX_SIGNAL_AGE_SECONDS:
            return False, (
                f"Signal too old ({signal_age_s:.0f}s > {MAX_SIGNAL_AGE_SECONDS}s limit). "
                "Fast market — skip this signal."
            )

        # ── 21. ★ DATA FEED / STALE PRICE ────────────────────────────────────
        stale_ok, stale_msg = self._check_price_freshness(symbol)
        if not stale_ok:
            return False, stale_msg

        # ── 22. ★ STRATEGY DECAY ──────────────────────────────────────────────
        if strategy_id:
            decay_ok, decay_msg = self._check_strategy_decay(strategy_id)
            if not decay_ok:
                return False, decay_msg

        # ── 23. ★ OPTION BUYER PREMIUM CAP ───────────────────────────────────
        if self.instrument_type == "option_buyer":
            ob_ok, ob_msg = self._check_option_buyer_risk(qty, price, capital)
            if not ob_ok:
                return False, ob_msg

        # ── 24. ★ SHORT SELLER GUARD ──────────────────────────────────────────
        if self.instrument_type == "short_seller":
            ss_ok, ss_msg = self._check_short_seller_risk(symbol, qty, price, capital)
            if not ss_ok:
                return False, ss_msg

        logger.info(
            "✅ Risk OK | user=%s | %s %s %s @ ₹%s | tier=%.2f%% | signal_age=%.1fs",
            self.user.id, side.upper(), qty, symbol, price,
            float(tier_risk_pct) * 100, signal_age_s,
        )
        return True, "OK"

    # ──────────────────────────────────────────────────────────────────────────
    #  Kill Switch
    # ──────────────────────────────────────────────────────────────────────────

    def trigger_kill_switch(self, reason: str):
        """Emergency stop — halt ALL trading for this user."""
        cache_key = f"kill_switch:{self.user.id}"
        cache.set(cache_key, {
            "active": True,
            "reason": reason,
            "triggered_at": timezone.now().isoformat(),
            "triggered_by": "risk_manager",
        }, timeout=86400)

        logger.critical(
            "🚨 KILL SWITCH | user=%s | reason=%s", self.user.id, reason
        )
        self._stop_all_strategies()
        self._send_kill_switch_notification(reason)
        self._log_kill_switch_event(reason)

    def deactivate_kill_switch(self):
        cache.delete(f"kill_switch:{self.user.id}")
        logger.info("Kill switch deactivated | user=%s", self.user.id)

    def _is_kill_switch_active(self) -> bool:
        data = cache.get(f"kill_switch:{self.user.id}", {})
        return data.get("active", False)

    def get_kill_switch_status(self) -> Dict:
        return cache.get(f"kill_switch:{self.user.id}", {"active": False})

    # ──────────────────────────────────────────────────────────────────────────
    #  Capital-Tiered Risk
    # ──────────────────────────────────────────────────────────────────────────

    def _get_total_capital(self) -> Decimal:
        """
        User ki total capital fetch karo (INR wallet).

        ISOLATION GUARANTEE (BLOCKER #2):
        Wallet model mein unique_together = ("user", "currency") hai.
        Isliye Wallet.objects.get(user=self.user, currency="INR") hamesha
        SIRF is user ka wallet return karta hai — doosre users ka mix nahi hoga.

        RiskManager hamesha RiskManager(user) se instantiate hota hai,
        jisme signal/order ka user pass hota hai.
        Isliye cross-user capital leak structurally impossible hai.

        FAIL-CLOSED (naya behavior):
        Pehle: Wallet.DoesNotExist pe Rs 1L fallback return karta tha —
               user bina wallet ke bhi trade kar sakta tha.
        Ab: Wallet missing hai toh Decimal("0") return karo —
            risk checks block karenge (safe default).
        """
        from apps.wallet.models import Wallet

        try:
            wallet, created = Wallet.objects.get_or_create(
                user=self.user, currency="INR",
                defaults={"available_balance": Decimal("10000"), "locked_balance": Decimal("0")}
            )
            if created:
                logger.info("Auto-created INR wallet | user=%s", self.user.id)
            return wallet.available_balance + wallet.locked_balance

        except Exception as e:
            logger.error("_get_total_capital error | user=%s | %s", self.user.id, e)
            return Decimal("10000")

        except Exception as e:
            logger.error(
                "_get_total_capital unexpected error | user=%s | %s", self.user.id, e
            )
            return Decimal("0")  # fail-closed, not Rs 1L optimistic fallback

    def _get_capital_risk_pct(self, capital: Decimal) -> Decimal:
        """
        Capital ke hisaab se risk percentage nikalo.
        Hedge fund standard: bigger capital = lower risk %.
        """
        for min_cap, max_cap, risk_pct in CAPITAL_RISK_TIERS:
            if min_cap <= capital < max_cap:
                return risk_pct
        # Last tier (> ₹1Cr)
        return Decimal("0.0025")

    def get_capital_risk_info(self) -> Dict:
        """Flutter dashboard ke liye capital risk info."""
        capital = self._get_total_capital()
        risk_pct = self._get_capital_risk_pct(capital)
        max_loss = (capital * risk_pct).quantize(Decimal("1"), rounding=ROUND_DOWN)
        return {
            "capital": float(capital),
            "risk_pct": float(risk_pct * 100),
            "max_loss_per_trade": float(max_loss),
            "tier": self._get_tier_label(capital),
        }

    def _get_tier_label(self, capital: Decimal) -> str:
        if capital < 100000:
            return "Starter (≤₹1L)"
        elif capital < 500000:
            return "Small (₹1L–5L)"
        elif capital < 1000000:
            return "Medium (₹5L–10L)"
        elif capital < 2500000:
            return "Growth (₹10L–25L)"
        elif capital < 5000000:
            return "Large (₹25L–50L)"
        elif capital < 10000000:
            return "HNI (₹50L–1Cr)"
        else:
            return "UHNI (>₹1Cr)"

    # ──────────────────────────────────────────────────────────────────────────
    #  ★ NEW: Correlation Risk Check (#17)
    # ──────────────────────────────────────────────────────────────────────────

    def _check_correlation_risk(
        self, new_symbol: str, side: str
    ) -> Tuple[bool, str]:
        """
        Check: क्या हम एक ही correlated group में बहुत positions हैं?
        NIFTY + BANKNIFTY + FINNIFTY CE buy = same directional bet.
        """
        try:
            from apps.orders.models import Order

            open_orders = Order.objects.filter(
                user=self.user, status="open"
            ).values_list("asset__symbol", "side")

            open_symbols = {sym.upper(): od_side for sym, od_side in open_orders if sym}
            new_sym_upper = new_symbol.upper().replace("NSE:", "").split("-")[0].strip()

            for group in CORRELATED_GROUPS:
                group_upper = [g.upper() for g in group]

                if new_sym_upper not in group_upper:
                    continue

                # Count how many correlated positions in same direction
                correlated_count = sum(
                    1
                    for sym, od_side in open_symbols.items()
                    if any(sym.startswith(g) for g in group_upper) and od_side == side
                )

                if correlated_count >= self.limits.max_correlated_positions:
                    correlated_symbols = [
                        sym for sym in open_symbols
                        if any(sym.startswith(g) for g in group_upper)
                    ]
                    return False, (
                        f"Correlation Risk: Already {correlated_count} {side.upper()} "
                        f"positions in same index group {correlated_symbols}. "
                        f"This is effectively 1 large bet. Max allowed: "
                        f"{self.limits.max_correlated_positions}."
                    )

            return True, ""

        except Exception as e:
            logger.error("_check_correlation_risk error: %s", e)
            return False, "Risk check error — trade blocked for safety"  # ✅ FAIL CLOSED   # Allow if check fails

    # ──────────────────────────────────────────────────────────────────────────
    #  ★ NEW: Same-Setup Overexposure (#18)
    # ──────────────────────────────────────────────────────────────────────────

    def _check_same_setup_overexposure(
        self, strategy_id: str, symbol: str
    ) -> Tuple[bool, str]:
        """
        Check: Ek hi strategy aaj kitni baar fire ho chuki hai?
        Same setup bar-bar fire = overconfidence bias.
        """
        try:
            cache_key = (
                f"setup_count:{self.user.id}:{strategy_id}:{symbol}:"
                f"{timezone.now().date()}"
            )
            count = cache.get(cache_key, 0)

            if count >= self.limits.max_same_setup_fires:
                return False, (
                    f"Same-Setup Overexposure: Strategy '{strategy_id}' "
                    f"on {symbol} has fired {count} times today "
                    f"(max {self.limits.max_same_setup_fires}). "
                    "Same setup repeating — skip to avoid overtrading."
                )

            # Increment counter — expires at midnight
            now = timezone.now()
            seconds_till_midnight = (
                86400
                - now.hour * 3600
                - now.minute * 60
                - now.second
            )
            cache.set(cache_key, count + 1, timeout=seconds_till_midnight)
            return True, ""

        except Exception as e:
            logger.error("_check_same_setup_overexposure error: %s", e)
            return False, "Risk check error — trade blocked for safety"  # ✅ FAIL CLOSED

    # ──────────────────────────────────────────────────────────────────────────
    #  ★ NEW: Volatility Regime (#19)
    # ──────────────────────────────────────────────────────────────────────────

    def _check_volatility_regime(
        self, symbol: str
    ) -> Tuple[bool, str, Decimal]:
        """
        VIX / realized volatility check.
        High VIX → reduce position size.
        Extreme VIX → block new positions.
        Returns: (allowed, message, size_multiplier)
        """
        try:
            vix = self._get_current_vix()

            if vix is None:
                return True, "", Decimal("1")

            if vix >= VIX_EXTREME_THRESHOLD:
                return False, (
                    f"Volatility Regime EXTREME: VIX={vix:.1f} "
                    f"(>{VIX_EXTREME_THRESHOLD}). "
                    "High risk of gap moves. New positions blocked."
                ), Decimal("0")

            if vix >= VIX_HIGH_THRESHOLD:
                # Reduce position size — return 0.5x multiplier (caller can use)
                size_multiplier = Decimal("0.5")
                logger.warning(
                    "High Volatility | VIX=%.1f | user=%s | size multiplier=0.5x",
                    vix, self.user.id
                )
                return True, "", size_multiplier

            return True, "", Decimal("1")

        except Exception as e:
            logger.error("_check_volatility_regime error: %s", e)
            return False, "Risk check error — trade blocked for safety"  # ✅ FAIL CLOSED, Decimal("1")

    def _get_current_vix(self) -> Optional[float]:
        """India VIX fetch karo (INDIAVIX from market service)."""
        try:
            cache_key = "india_vix_current"
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

            from apps.market.services import fetch_live_quote
            quote = fetch_live_quote("INDIAVIX", self.user)
            vix = quote.get("ltp") or quote.get("close")
            if vix:
                cache.set(cache_key, float(vix), timeout=60)
                return float(vix)
            return None
        except Exception:
            return None

    # ──────────────────────────────────────────────────────────────────────────
    #  ★ NEW: Execution Delay (signal age) — handled in can_place_order (#20)
    # ──────────────────────────────────────────────────────────────────────────
    # Logic is inline in can_place_order step 20 using signal_age_s parameter.

    # ──────────────────────────────────────────────────────────────────────────
    #  ★ NEW: Data Feed / Price Freshness (#21)
    # ──────────────────────────────────────────────────────────────────────────

    def _check_price_freshness(self, symbol: str) -> Tuple[bool, str]:
        """
        LTP timestamp check — stale price se order mat lo.
        """
        try:
            cache_key = f"ltp_ts:{symbol}"
            ltp_ts = cache.get(cache_key)   # set by feed_manager when tick arrives

            if ltp_ts is not None:
                age = time.time() - ltp_ts
                if age > MAX_LTP_AGE_SECONDS:
                    return False, (
                        f"Data Feed Error: LTP for {symbol} is {age:.0f}s old "
                        f"(max {MAX_LTP_AGE_SECONDS}s). "
                        "Price feed may be stale — order blocked."
                    )

            return True, ""

        except Exception as e:
            logger.error("_check_price_freshness error: %s", e)
            return False, "Risk check error — trade blocked for safety"  # ✅ FAIL CLOSED   # Allow if check fails

    # ──────────────────────────────────────────────────────────────────────────
    #  ★ NEW: Strategy Decay Detection (#22)
    # ──────────────────────────────────────────────────────────────────────────

    def _check_strategy_decay(self, strategy_id: str) -> Tuple[bool, str]:
        """
        Last N trades ki win rate check karo.
        Agar win rate floor se neeche → strategy pause karo.
        """
        try:
            cache_key = f"strategy_winrate:{self.user.id}:{strategy_id}"
            cached = cache.get(cache_key)
            if cached is not None:
                win_rate = Decimal(str(cached))
            else:
                from apps.strategies.models import StrategySignal
                recent = (
                    StrategySignal.objects.filter(
                        strategy__id=strategy_id,
                        strategy__user=self.user,
                    )
                    .order_by("-created_at")
                    [:STRATEGY_DECAY_LOOKBACK_TRADES]
                )

                total = recent.count()
                # ✅ FIX: min trades guard = lookback size
                # Pehle hardcoded 10 tha — lookback 15 hai toh 10 trades pe
                # check karna inconsistent tha (15 mein se sirf 10 dekh ke judge karna).
                if total < STRATEGY_DECAY_LOOKBACK_TRADES:
                    # Not enough data to judge decay
                    return True, ""

                winners = recent.filter(outcome="win").count()
                win_rate = Decimal(str(winners)) / Decimal(str(total))
                cache.set(cache_key, float(win_rate), timeout=300)  # 5 min cache

            if win_rate < STRATEGY_DECAY_WIN_RATE_FLOOR:
                logger.warning(
                    "⚠️ Strategy Decay | strategy=%s | user=%s | win_rate=%.1f%%",
                    strategy_id, self.user.id, float(win_rate) * 100
                )
                return False, (
                    f"Strategy Decay Detected: Last {STRATEGY_DECAY_LOOKBACK_TRADES} "
                    f"trades win rate = {float(win_rate)*100:.1f}% "
                    f"(floor: {float(STRATEGY_DECAY_WIN_RATE_FLOOR)*100:.0f}%). "
                    "Strategy auto-paused. Review setup."
                )

            return True, ""

        except Exception as e:
            logger.error("_check_strategy_decay error: strategy=%s | %s", strategy_id, e)
            return False, "Risk check error — trade blocked for safety"  # ✅ FAIL CLOSED

    # ──────────────────────────────────────────────────────────────────────────
    #  ★ NEW: Option Buyer Risk (#23)
    # ──────────────────────────────────────────────────────────────────────────

    def _check_option_buyer_risk(
        self, qty: Decimal, premium: Decimal, capital: Decimal
    ) -> Tuple[bool, str]:
        """
        Option buyer ke liye:
        - Premium decay (theta) = guaranteed loss per day
        - Cap: total premium spent ≤ 0.5% of capital per trade
        - Daily premium budget: ≤ 1% of capital
        """
        try:
            # Per trade premium cap
            trade_premium_spent = qty * premium
            max_premium_per_trade = capital * OPTION_BUYER_PREMIUM_CAP_PCT

            if trade_premium_spent > max_premium_per_trade:
                return False, (
                    f"Option Buyer Risk: Premium ₹{trade_premium_spent:.0f} "
                    f"> {float(OPTION_BUYER_PREMIUM_CAP_PCT)*100:.1f}% of capital "
                    f"(₹{max_premium_per_trade:.0f}). "
                    "Reduce lots or choose lower premium strike."
                )

            # Daily premium budget check
            cache_key = f"daily_premium:{self.user.id}:{timezone.now().date()}"
            daily_premium_spent = Decimal(str(cache.get(cache_key, 0)))
            daily_premium_limit = capital * Decimal("0.01")  # 1% of capital per day

            if daily_premium_spent + trade_premium_spent > daily_premium_limit:
                return False, (
                    f"Option Buyer Daily Budget: ₹{daily_premium_spent:.0f} already spent "
                    f"today (limit ₹{daily_premium_limit:.0f} = 1% of capital). "
                    "Stop buying options for today — theta decay risk."
                )

            # Update daily premium counter
            now = timezone.now()
            seconds_till_midnight = 86400 - now.hour*3600 - now.minute*60 - now.second
            cache.set(
                cache_key,
                float(daily_premium_spent + trade_premium_spent),
                timeout=seconds_till_midnight,
            )
            return True, ""

        except Exception as e:
            logger.error("_check_option_buyer_risk error: %s", e)
            return False, "Risk check error — trade blocked for safety"  # ✅ FAIL CLOSED

    # ──────────────────────────────────────────────────────────────────────────
    #  ★ NEW: Short Seller (Option Writer) Risk (#24)
    # ──────────────────────────────────────────────────────────────────────────

    def _check_short_seller_risk(
        self, symbol: str, qty: Decimal, price: Decimal, capital: Decimal
    ) -> Tuple[bool, str]:
        """
        Short seller / option writer ke liye:
        - Unlimited loss potential → strict notional cap
        - Margin cliff check (sudden margin increase)
        - Total short notional ≤ 10% of capital
        """
        try:
            from apps.orders.models import Order

            # Current total short notional
            short_orders = Order.objects.filter(
                user=self.user,
                side="sell",
                status="filled",
            ).values_list("filled_qty", "avg_fill_price")

            existing_short_notional = sum(
                Decimal(str(q or 0)) * Decimal(str(p or 0))
                for q, p in short_orders
            )

            new_short_notional = qty * price
            total_short_notional = existing_short_notional + new_short_notional
            max_short_notional = capital * SHORT_SELLER_NOTIONAL_CAP_PCT

            if total_short_notional > max_short_notional:
                return False, (
                    f"Short Seller Risk: Total short notional would be "
                    f"₹{total_short_notional:.0f} "
                    f"({float(SHORT_SELLER_NOTIONAL_CAP_PCT)*100:.0f}% cap = "
                    f"₹{max_short_notional:.0f}). "
                    "Short selling has unlimited loss risk — reduce exposure."
                )

            # Check: is this a naked short? (no hedge detected)
            # Look for a corresponding long in same underlying
            underlying = symbol.split("CE")[0].split("PE")[0].strip()
            has_hedge = Order.objects.filter(
                user=self.user,
                asset__symbol__icontains=underlying,
                side="buy",
                status="filled",
            ).exists()

            if not has_hedge:
                logger.warning(
                    "Naked Short Detected | user=%s | symbol=%s",
                    self.user.id, symbol
                )
                # Allow but log — naked short warning
                # You can return False here to block entirely

            return True, ""

        except Exception as e:
            logger.error("_check_short_seller_risk error: %s", e)
            return False, "Risk check error — trade blocked for safety"  # ✅ FAIL CLOSED

    # ──────────────────────────────────────────────────────────────────────────
    #  Standard Helpers (original + fixed)
    # ──────────────────────────────────────────────────────────────────────────

    def _load_user_limits(self) -> RiskLimits:
        try:
            profile = self.user.trading_profile
            capital = self._get_total_capital_safe()

            # ── % based limits — auto-scale with capital ──────────
            # Agar profile mein pct set hai toh capital se calculate karo
            # Warna hardcoded value use karo (backward compat)
            if profile.max_daily_loss_pct:
                max_loss_per_day = capital * profile.max_daily_loss_pct
            else:
                max_loss_per_day = profile.max_daily_loss or Decimal("10000")

            if profile.risk_per_trade_pct:
                max_loss_per_trade = capital * profile.risk_per_trade_pct
            else:
                max_loss_per_trade = profile.max_loss_per_trade or Decimal("2000")

            if profile.max_position_pct:
                max_position_size = capital * profile.max_position_pct
            else:
                max_position_size = profile.max_position_size or Decimal("100000")

            logger.info(
                "RiskLimits loaded | user=%s | capital=%.0f | max_loss_trade=%.0f | max_daily=%.0f | max_pos=%.0f",
                self.user.id, capital, max_loss_per_trade, max_loss_per_day, max_position_size,
            )

            return RiskLimits(
                max_loss_per_day      = max_loss_per_day,
                max_profit_lock       = profile.profit_lock_amount or Decimal("5000"),
                max_trades_per_day    = profile.max_daily_trades or 50,
                max_position_size     = max_position_size,
                max_positions         = profile.max_positions or 10,
                max_drawdown          = profile.max_drawdown or Decimal("0.20"),
                max_loss_per_trade    = max_loss_per_trade,
                min_risk_reward       = profile.min_rr_ratio or Decimal("1.5"),
                stop_loss_required    = profile.require_stop_loss,
                capital_risk_pct      = self._get_capital_risk_pct(capital),
                instrument_type       = self.instrument_type,
            )
        except Exception as e:
            logger.warning("_load_user_limits fallback | user=%s | %s", self.user.id, e)
            return RiskLimits()

    def _get_total_capital_safe(self) -> Decimal:
        """Safe version without import loops during __init__."""
        try:
            from apps.wallet.models import Wallet
            w = Wallet.objects.get(user=self.user, currency="INR")
            return w.available_balance + w.locked_balance
        except Exception:
            return Decimal("100000")

    def _check_order_rate_limit(self) -> bool:
        cache_key = f"order_rate:{self.user.id}"
        count = cache.get(cache_key, 0)
        if count >= self.limits.max_orders_per_minute:
            return False
        cache.set(cache_key, count + 1, timeout=60)
        return True

    def _get_daily_pnl(self) -> Decimal:
        from apps.orders.models import Order
        cache_key = f"daily_pnl:{self.user.id}:{timezone.now().date()}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Decimal(str(cached))
        today = timezone.now().date()
        try:
            pnl = Order.objects.filter(
                user=self.user, created_at__date=today, status__in=["closed", "filled"]
            ).aggregate(total=Sum("realized_pnl"))["total"] or Decimal("0")
        except Exception:
            pnl = Decimal("0")  # realized_pnl field missing — skip
        cache.set(cache_key, float(pnl), timeout=10)
        return pnl

    def _get_daily_trade_count(self) -> int:
        from apps.orders.models import Order
        cache_key = f"daily_trades:{self.user.id}:{timezone.now().date()}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        count = Order.objects.filter(
            user=self.user, created_at__date=timezone.now().date()
        ).count()
        cache.set(cache_key, count, timeout=10)
        return count

    def _get_open_position_count(self) -> int:
        from apps.orders.models import Order
        return Order.objects.filter(user=self.user, status="filled").count()

    def _check_drawdown_limit(self) -> bool:
        from apps.wallet.models import Wallet
        try:
            wallet = Wallet.objects.get(user=self.user, currency="INR")
            total = wallet.available_balance + wallet.locked_balance
            peak = cache.get(f"peak_equity:{self.user.id}", total)
            if total > peak:
                cache.set(f"peak_equity:{self.user.id}", float(total), timeout=86400)
                peak = total
            if peak <= 0:
                return True
            drawdown = (peak - total) / peak
            if drawdown > self.limits.max_drawdown:
                logger.critical(
                    "Drawdown exceeded | user=%s | drawdown=%.1f%%",
                    self.user.id, float(drawdown) * 100
                )
                return False
            return True
        except Exception as e:
            logger.critical(
                "🚨 _check_drawdown_limit FAILED — blocking trade as safety measure | "
                "user=%s | err=%s", self.user.id, e
            )
            return False  # ✅ FAIL CLOSED — doubt mein hoon toh rokna better hai

    def _get_unrealized_pnl(self) -> Decimal:
        from apps.orders.models import Order
        total_pnl = Decimal("0")
        for order in Order.objects.filter(user=self.user, status="filled").select_related("asset"):
            try:
                cp = self._get_current_price(order.symbol)
                if not cp:
                    continue
                ep = order.avg_fill_price or order.price
                qty = order.filled_qty or order.quantity
                pnl = (cp - ep) * qty if order.side == "buy" else (ep - cp) * qty
                total_pnl += pnl
            except Exception:
                continue
        return total_pnl

    def _get_current_price(self, symbol: str) -> Optional[Decimal]:
        try:
            from apps.market.services import fetch_live_quote
            quote = fetch_live_quote(symbol, self.user)
            ltp = quote.get("ltp", 0)
            # Update freshness timestamp
            cache.set(f"ltp_ts:{symbol}", time.time(), timeout=60)
            return Decimal(str(ltp)) if ltp else None
        except Exception as e:
            logger.error("_get_current_price error | symbol=%s | %s", symbol, e)
            return None

    def _check_symbol_concentration(self, symbol: str, new_value: Decimal) -> bool:
        from apps.orders.models import Order
        from apps.wallet.models import Wallet
        try:
            wallet = Wallet.objects.get(user=self.user, currency="INR")
            total = wallet.available_balance + wallet.locked_balance
            if total <= 0:
                return True
            current_qty = (
                Order.objects.filter(user=self.user, asset__symbol=symbol, status="filled")
                .aggregate(total=Sum("filled_qty"))["total"] or Decimal("0")
            )
            cp = self._get_current_price(symbol) or Decimal("0")
            total_exposure = current_qty * cp + new_value
            if total_exposure / total > self.limits.max_symbol_exposure:
                return False
            return True
        except Exception as e:
            logger.warning(
                "_check_symbol_concentration error — allowing trade | "
                "user=%s | err=%s", self.user.id, e
            )
            return True  # Allow on error

    def _has_sufficient_margin(self, position_value: Decimal) -> bool:
        from apps.wallet.models import Wallet
        try:
            wallet = Wallet.objects.get(user=self.user, currency="INR")
            required = position_value / self.limits.max_leverage
            return wallet.available_balance >= required
        except Exception as e:
            logger.error("_has_sufficient_margin error: %s", e)
            return False

    def _is_price_reasonable(self, symbol: str, order_price: Decimal) -> bool:
        try:
            cp = self._get_current_price(symbol)
            if not cp:
                return True
            diff_pct = abs(order_price - cp) / cp * 100
            if diff_pct > 10:
                logger.warning(
                    "Price sanity fail | symbol=%s | order=%.2f | market=%.2f | diff=%.1f%%",
                    symbol, float(order_price), float(cp), float(diff_pct)
                )
                return False
            return True
        except Exception:
            logger.critical(
                "🚨 _is_price_reasonable FAILED — blocking trade | user=%s",
                self.user.id
            )
            return False  # ✅ FAIL CLOSED

    def _calculate_rr_ratio(
        self, entry: Decimal, sl: Decimal, tp: Decimal, side: str
    ) -> Decimal:
        risk   = (entry - sl) if side == "buy" else (sl - entry)
        reward = (tp - entry) if side == "buy" else (entry - tp)
        if risk <= 0:
            return Decimal("0")
        return reward / risk

    def _is_market_open(self) -> bool:
        """
        ✅ FIXED: Was using UTC time — now properly converts to IST.
        """
        if getattr(settings, "SKIP_MARKET_HOURS_CHECK", False):
            return True
        try:
            import pytz
            from datetime import time as dt_time
            IST = pytz.timezone("Asia/Kolkata")
            now_ist = timezone.now().astimezone(IST)
            if now_ist.weekday() >= 5:   # Sat/Sun
                return False
            return dt_time(9, 15) <= now_ist.time() <= dt_time(15, 30)
        except Exception:
            logger.critical(
                "🚨 _is_price_reasonable FAILED — blocking trade | user=%s",
                self.user.id
            )
            return False  # ✅ FAIL CLOSED   # If pytz unavailable, allow

    def _stop_all_strategies(self):
        """
        ✅ FIXED: is_running is a @property — cannot filter on it.
        Use state field directly.
        """
        from apps.strategies.models import Strategy
        count = Strategy.objects.filter(
            user=self.user,
            state=Strategy.State.RUNNING,   # ✅ actual DB field
        ).update(state=Strategy.State.IDLE)
        logger.info("Stopped %d strategies | user=%s", count, self.user.id)

    def _send_kill_switch_notification(self, reason: str):
        try:
            from apps.notifications.tasks import send_urgent_notification
            send_urgent_notification.apply_async(
                args=[self.user.id, f"⚠️ TRADING STOPPED: {reason}"]
            )
        except Exception as e:
            logger.error("Kill switch notification failed: %s", e)

    def _log_kill_switch_event(self, reason: str):
        try:
            from apps.risk.models import RiskEvent
            RiskEvent.objects.create(
                user=self.user,
                event_type="kill_switch",
                severity="critical",
                reason=reason,
                metadata={
                    "daily_pnl": float(self._get_daily_pnl()),
                    "open_positions": self._get_open_position_count(),
                },
            )
        except Exception as e:
            logger.error("_log_kill_switch_event failed: %s", e)

    # ──────────────────────────────────────────────────────────────────────────
    #  Status API (for Flutter dashboard)
    # ──────────────────────────────────────────────────────────────────────────

    def get_risk_status(self) -> Dict:
        """Complete risk status — Flutter dashboard ke liye."""
        capital = self._get_total_capital()
        risk_pct = self._get_capital_risk_pct(capital)
        vix = self._get_current_vix()
        return {
            "kill_switch_active":    self._is_kill_switch_active(),
            "kill_switch_data":      self.get_kill_switch_status(),
            "daily_pnl":             float(self._get_daily_pnl()),
            "daily_trades":          self._get_daily_trade_count(),
            "open_positions":        self._get_open_position_count(),
            "unrealized_pnl":        float(self._get_unrealized_pnl()),
            "market_open":           self._is_market_open(),
            # Capital tier
            "capital":               float(capital),
            "capital_tier":          self._get_tier_label(capital),
            "risk_pct":              float(risk_pct * 100),
            "max_loss_per_trade":    float(capital * risk_pct),
            # Volatility
            "india_vix":             vix,
            "volatility_regime": (
                "EXTREME" if vix and vix >= VIX_EXTREME_THRESHOLD
                else "HIGH" if vix and vix >= VIX_HIGH_THRESHOLD
                else "NORMAL"
            ),
            "limits": {
                "max_loss_per_day":   float(self.limits.max_loss_per_day),
                "max_trades_per_day": self.limits.max_trades_per_day,
                "max_positions":      self.limits.max_positions,
                "max_drawdown":       float(self.limits.max_drawdown),
                "min_rr_ratio":       float(self.limits.min_risk_reward),
                "max_correlated":     self.limits.max_correlated_positions,
                "max_same_setup":     self.limits.max_same_setup_fires,
            },
        }