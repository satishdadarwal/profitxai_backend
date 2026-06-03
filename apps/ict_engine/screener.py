# apps/ict_engine/screener.py
#
# Professional ICT Screener
# ─────────────────────────────────────────────────────────────
# 5 named setups, A+/A/B/C grading, dynamic position sizing
# Celery task se call hota hai har 15 min mein market hours mein

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

import pandas as pd

from .base import RiskParameters
from .ict import (
    BreakDirection,
    BreakType,
    FVGStatus,
    FVGType,
    LiqStatus,
    MTFAnalysis,
    OBStatus,
    OBType,
    TFSnapshot,
)

logger = logging.getLogger(__name__)


# ─── Setup Types ──────────────────────────────────────────────
class SetupType(str, Enum):
    LIQUIDITY_RAID = "ICT_RAID"  # A+: Sweep + OB reversal
    BOS_OB_RETEST = "ICT_BOS_PULL"  # A+: BOS + OB pullback
    CHOCH_FIRST_OB = "ICT_CHOCH"  # A:  First CHoCH + OB
    FVG_OB_ZONE = "ICT_FVG_OB"  # A:  FVG + OB confluence
    KZ_INST_OB = "ICT_KZ_OB"  # A:  KZ + Institutional OB
    NO_SETUP = "NONE"


# ─── Signal Grade ─────────────────────────────────────────────
class SignalGrade(str, Enum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    C = "C"  # Skip


# ─── Graded Signal ────────────────────────────────────────────
@dataclass
class GradedSignal:
    # Identity
    symbol: str
    direction: str  # 'long' | 'short'
    setup_type: SetupType
    grade: SignalGrade

    # Levels
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: Optional[float] = None
    take_profit_3: Optional[float] = None

    # Risk metrics
    risk_reward: float = 0.0
    confluence_score: float = 0.0
    position_size: float = 0.0  # units
    risk_amount: float = 0.0  # INR
    risk_pct: float = 0.0  # % of account

    # Context
    tags: list = field(default_factory=list)
    killzone: str = ""
    timeframes: list = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)
    notes: str = ""
    market_type: str = "indian"  # 'indian' | 'crypto'

    # Computed
    @property
    def is_tradeable(self) -> bool:
        return self.grade in (SignalGrade.A_PLUS, SignalGrade.A, SignalGrade.B)

    @property
    def grade_emoji(self) -> str:
        return {
            SignalGrade.A_PLUS: "🟢",
            SignalGrade.A: "🟡",
            SignalGrade.B: "🔵",
            SignalGrade.C: "⚫",
        }.get(self.grade, "⚫")

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "setup_type": self.setup_type.value,
            "grade": self.grade.value,
            "entry_price": round(self.entry_price, 2),
            "stop_loss": round(self.stop_loss, 2),
            "take_profit_1": round(self.take_profit_1, 2),
            "take_profit_2": (
                round(self.take_profit_2, 2) if self.take_profit_2 else None
            ),
            "take_profit_3": (
                round(self.take_profit_3, 2) if self.take_profit_3 else None
            ),
            "risk_reward": round(self.risk_reward, 2),
            "confluence": round(self.confluence_score, 1),
            "position_size": round(self.position_size, 2),
            "risk_amount": round(self.risk_amount, 0),
            "risk_pct": round(self.risk_pct, 2),
            "tags": self.tags,
            "killzone": self.killzone,
            "breakdown": self.breakdown,
            "notes": self.notes,
            "grade_emoji": self.grade_emoji,
            "market_type": self.market_type,
        }

    def summary(self) -> str:
        return (
            f"{self.grade_emoji} {self.grade.value} | {self.symbol} | "
            f"{self.direction.upper()} | {self.setup_type.value}\n"
            f"   Entry: {self.entry_price:.0f} | SL: {self.stop_loss:.0f} | "
            f"TP: {self.take_profit_1:.0f}\n"
            f"   RR: {self.risk_reward:.1f} | Score: {self.confluence_score:.0f} | "
            f"KZ: {self.killzone or 'None'}\n"
            f"   Risk: ₹{self.risk_amount:.0f} ({self.risk_pct:.1f}%) | "
            f"Tags: {', '.join(self.tags)}"
        )


# ─── Setup Detector ───────────────────────────────────────────
class SetupDetector:
    """
    5 ICT setups identify karo MTFAnalysis se.
    Priority order: A+ first, A second, B last.
    """

    def detect(self, mtf: MTFAnalysis) -> tuple[SetupType, str]:
        """
        Returns (SetupType, notes).
        """
        # A+ Setup 1: Liquidity Raid → OB → Reversal
        result = self._check_liquidity_raid(mtf)
        if result:
            return SetupType.LIQUIDITY_RAID, result

        # A+ Setup 2: BOS + OB Retest
        result = self._check_bos_ob_retest(mtf)
        if result:
            return SetupType.BOS_OB_RETEST, result

        # A Setup 3: First CHoCH + OB
        result = self._check_choch_first_ob(mtf)
        if result:
            return SetupType.CHOCH_FIRST_OB, result

        # A Setup 4: FVG + OB Confluence Zone
        result = self._check_fvg_ob_zone(mtf)
        if result:
            return SetupType.FVG_OB_ZONE, result

        # A Setup 5: Kill Zone + Institutional OB
        result = self._check_kz_inst_ob(mtf)
        if result:
            return SetupType.KZ_INST_OB, result

        return SetupType.NO_SETUP, ""

    def _check_liquidity_raid(self, mtf: MTFAnalysis) -> Optional[str]:
        """
        A+ Setup: Liquidity sweep → immediate reversal → Institutional OB + FVG
        Most reliable ICT setup — institutions hunt stops then reverse.
        """
        confluence = mtf.confluence
        if not confluence or confluence.direction is None:
            return None

        direction = confluence.direction
        notes = []

        # Check liquidity sweep in execution TF
        exec_snap = mtf.snapshots.get(mtf.execution_tf)
        if exec_snap is None:
            return None

        # Recent sweep in correct direction
        sweeps = exec_snap.liquidity.recent_sweeps
        correct_sweep = False
        for sweep in sweeps[:3]:
            if (
                direction == "long"
                and sweep.liq_type.value == "SSL"
                and sweep.is_stop_hunt
            ):
                correct_sweep = True
                notes.append("SSL_SWEPT")
                break
            if (
                direction == "short"
                and sweep.liq_type.value == "BSL"
                and sweep.is_stop_hunt
            ):
                correct_sweep = True
                notes.append("BSL_SWEPT")
                break

        if not correct_sweep:
            return None

        # Institutional OB must be present
        ob = exec_snap.nearest_ob
        if ob is None or not ob.is_institutional:
            return None
        if ob.status not in {OBStatus.PRISTINE, OBStatus.TESTED}:
            return None

        # Direction must match OB
        if direction == "long" and ob.ob_type != OBType.BULLISH:
            return None
        if direction == "short" and ob.ob_type != OBType.BEARISH:
            return None

        notes.append("INST_OB")

        # FVG bonus (not required but adds confidence)
        fvg = exec_snap.nearest_fvg
        if fvg and fvg.status in {FVGStatus.OPEN, FVGStatus.PARTIAL}:
            notes.append("FVG")

        # HTF bias must agree
        anchor_snap = mtf.snapshots.get(mtf.anchor_tf)
        if anchor_snap:
            if direction == "long" and anchor_snap.bias != BreakDirection.BULLISH:
                return None
            if direction == "short" and anchor_snap.bias != BreakDirection.BEARISH:
                return None
            notes.append("HTF_ALIGNED")

        return "Liquidity raid → OB reversal | " + " + ".join(notes)

    def _check_bos_ob_retest(self, mtf: MTFAnalysis) -> Optional[str]:
        """
        A+ Setup: Clear BOS on 1H, pristine OB that caused BOS, price retests.
        Second most reliable — smart money leaves footprint at BOS origin.
        """
        confluence = mtf.confluence
        if not confluence or confluence.direction is None:
            return None

        direction = confluence.direction
        notes = []

        # Need BOS on intermediate TF (1H preferred)
        bos_snap = None
        for tf_label in ["1H", "30m", "15m"]:
            snap = mtf.snapshots.get(tf_label)
            if snap and snap.last_bos:
                bos_snap = snap
                notes.append(f"BOS_{tf_label}")
                break

        if bos_snap is None:
            return None

        # BOS direction must match trade direction
        bos = bos_snap.last_bos
        if direction == "long" and bos.direction != BreakDirection.BULLISH:
            return None
        if direction == "short" and bos.direction != BreakDirection.BEARISH:
            return None

        # Pristine OB must exist in execution TF
        exec_snap = mtf.snapshots.get(mtf.execution_tf)
        if exec_snap is None:
            return None

        ob = exec_snap.nearest_ob
        if ob is None or ob.status != OBStatus.PRISTINE:
            return None

        if direction == "long" and ob.ob_type != OBType.BULLISH:
            return None
        if direction == "short" and ob.ob_type != OBType.BEARISH:
            return None

        notes.append("PRISTINE_OB")

        # FVG inside OB zone — adds confidence
        fvg = exec_snap.nearest_fvg
        if fvg and fvg.status in {FVGStatus.OPEN, FVGStatus.PARTIAL}:
            notes.append("FVG_IN_OB")

        # All TFs aligned is ideal
        if mtf.aligned:
            notes.append("MTF_ALIGNED")

        return "BOS + OB retest | " + " + ".join(notes)

    def _check_choch_first_ob(self, mtf: MTFAnalysis) -> Optional[str]:
        """
        A Setup: First CHoCH after extended trend — market structure shift.
        Only FIRST CHoCH reliable. Subsequent ones are weaker.
        """
        confluence = mtf.confluence
        if not confluence or confluence.direction is None:
            return None

        direction = confluence.direction
        notes = []

        # CHoCH must exist on intermediate TF
        choch_found = False
        for tf_label in ["1H", "4H", "30m"]:
            snap = mtf.snapshots.get(tf_label)
            if snap and snap.last_choch:
                choch = snap.last_choch
                # CHoCH direction must match new trade direction
                if direction == "long" and choch.direction == BreakDirection.BULLISH:
                    choch_found = True
                    notes.append(f"CHOCH_{tf_label}")
                    break
                if direction == "short" and choch.direction == BreakDirection.BEARISH:
                    choch_found = True
                    notes.append(f"CHOCH_{tf_label}")
                    break

        if not choch_found:
            return None

        # OB after CHoCH
        exec_snap = mtf.snapshots.get(mtf.execution_tf)
        if exec_snap is None:
            return None

        ob = exec_snap.nearest_ob
        if ob is None:
            return None

        correct_ob = (direction == "long" and ob.ob_type == OBType.BULLISH) or (
            direction == "short" and ob.ob_type == OBType.BEARISH
        )
        if not correct_ob:
            return None

        notes.append("POST_CHOCH_OB")

        if ob.is_institutional:
            notes.append("INST")

        return "CHoCH + First OB | " + " + ".join(notes)

    def _check_fvg_ob_zone(self, mtf: MTFAnalysis) -> Optional[str]:
        """
        A Setup: Significant FVG + OB in same price zone.
        FVG alone is B grade. FVG + OB = A grade.
        """
        confluence = mtf.confluence
        if not confluence or confluence.direction is None:
            return None

        direction = confluence.direction
        exec_snap = mtf.snapshots.get(mtf.execution_tf)
        if exec_snap is None:
            return None

        ob = exec_snap.nearest_ob
        fvg = exec_snap.nearest_fvg

        # Both must be present
        if ob is None or fvg is None:
            return None

        # Direction check
        ob_correct = (direction == "long" and ob.ob_type == OBType.BULLISH) or (
            direction == "short" and ob.ob_type == OBType.BEARISH
        )
        fvg_correct = (direction == "long" and fvg.fvg_type == FVGType.BULLISH) or (
            direction == "short" and fvg.fvg_type == FVGType.BEARISH
        )

        if not ob_correct or not fvg_correct:
            return None

        # FVG must be significant
        if not fvg.is_significant:
            return None

        # OB must be active
        if ob.status not in {OBStatus.PRISTINE, OBStatus.TESTED}:
            return None

        # Check overlap — FVG and OB in same zone
        fvg_mid = fvg.mid
        ob_in_fvg = ob.bottom <= fvg_mid <= ob.top
        fvg_in_ob = fvg.bottom <= ob.mid <= fvg.top

        if not (ob_in_fvg or fvg_in_ob):
            return None  # Not overlapping — weak signal

        notes = ["FVG+OB_CONFLUENCE"]
        if ob.is_institutional:
            notes.append("INST")
        if fvg.fill_pct == 0:
            notes.append("FRESH_FVG")

        return "FVG + OB zone | " + " + ".join(notes)

    def _check_kz_inst_ob(self, mtf: MTFAnalysis) -> Optional[str]:
        """
        A Setup: Kill Zone timing + Institutional OB.
        Time-based — KZ + smart money footprint.
        """
        # KZ must be active
        if not mtf.killzone or not mtf.killzone.in_any_killzone:
            return None

        kz = mtf.killzone
        if kz.combined_priority_score < 6.0:
            return None  # Only high-priority KZs

        confluence = mtf.confluence
        if not confluence or confluence.direction is None:
            return None

        direction = confluence.direction
        exec_snap = mtf.snapshots.get(mtf.execution_tf)
        if exec_snap is None:
            return None

        ob = exec_snap.nearest_ob
        if ob is None or not ob.is_institutional:
            return None

        ob_correct = (direction == "long" and ob.ob_type == OBType.BULLISH) or (
            direction == "short" and ob.ob_type == OBType.BEARISH
        )
        if not ob_correct:
            return None

        kz_name = (
            kz.highest_priority_zone.name.value if kz.highest_priority_zone else "KZ"
        )
        notes = [f"KZ_{kz_name.upper()}", "INST_OB"]

        if kz.is_preferred_day:
            notes.append("PREF_DAY")

        return f"KZ + Institutional OB | " + " + ".join(notes)


# ─── Grade Calculator ─────────────────────────────────────────
class GradeCalculator:
    """
    Setup type + confluence + conditions → A+/A/B/C grade.
    """

    def calculate(
        self,
        setup_type: SetupType,
        confluence: float,
        rr: float,
        mtf: MTFAnalysis,
        direction: str,
    ) -> SignalGrade:

        # C grade — always skip
        if confluence < 55 or rr < 2.0:
            return SignalGrade.C

        # A+ conditions
        a_plus_setups = {SetupType.LIQUIDITY_RAID, SetupType.BOS_OB_RETEST}
        if setup_type in a_plus_setups:
            if confluence >= 75 and rr >= 2.8 and mtf.aligned:
                return SignalGrade.A_PLUS
            if confluence >= 70 and rr >= 2.5:
                return SignalGrade.A

        # A conditions
        a_setups = {
            SetupType.CHOCH_FIRST_OB,
            SetupType.FVG_OB_ZONE,
            SetupType.KZ_INST_OB,
        }
        if setup_type in a_setups:
            if confluence >= 68 and rr >= 2.3:
                return SignalGrade.A
            if confluence >= 60 and rr >= 2.0:
                return SignalGrade.B

        # Generic fallback
        if confluence >= 70 and rr >= 2.5:
            return SignalGrade.A
        if confluence >= 60 and rr >= 2.0:
            return SignalGrade.B

        return SignalGrade.C


# ─── Position Sizer ───────────────────────────────────────────
class PositionSizer:
    """
    Grade → risk % → position size calculator.

    NIFTY:      lot size = 75
    BANKNIFTY:  lot size = 15
    FINNIFTY:   lot size = 40
    MIDCPNIFTY: lot size = 75
    SENSEX:     lot size = 10
    """

    LOT_SIZES = {
        "NIFTY": 65,
        "BANKNIFTY": 30,
        "FINNIFTY": 60,
        "MIDCPNIFTY": 120,
        "SENSEX": 10,
    }

    GRADE_RISK_PCT = {
        SignalGrade.A_PLUS: 2.0,
        SignalGrade.A: 1.5,
        SignalGrade.B: 0.75,
        SignalGrade.C: 0.0,
    }

    def calculate(
        self,
        grade: SignalGrade,
        entry: float,
        stop: float,
        symbol: str,
        account_balance: float = 100_000,
    ) -> tuple[float, float, float]:
        """
        Returns (position_size_units, risk_amount_inr, risk_pct).
        """
        risk_pct = self.GRADE_RISK_PCT.get(grade, 0.0)
        risk_amount = account_balance * (risk_pct / 100)
        risk_per_pt = abs(entry - stop)

        if risk_per_pt == 0 or risk_amount == 0:
            return 0.0, 0.0, risk_pct

        lot_size = self.LOT_SIZES.get(symbol.upper(), 75)
        risk_per_lot = risk_per_pt * lot_size

        if risk_per_lot == 0:
            return 0.0, 0.0, risk_pct

        lots = risk_amount / risk_per_lot
        lots = max(0.25, round(lots * 4) / 4)  # round to nearest 0.25

        actual_risk = lots * risk_per_lot

        return round(lots, 2), round(actual_risk, 0), risk_pct


# ─── Main Screener ────────────────────────────────────────────
class ICTScreener:
    """
    Professional ICT screener — multi-symbol, graded signals.

    Usage:
        screener = ICTScreener(account_balance=100000)
        signals  = await screener.scan_all(['NIFTY', 'BANKNIFTY'])
    """

    SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
    CRYPTO_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

    TF_CONFIG = {
        "timeframes": ["1D", "1H", "15m"],
        "anchor_tf": "1D",
        "execution_tf": "15m",
        "bars_per_tf": 200,
    }

    def __init__(
        self,
        account_balance: float = 100_000,
        min_grade: str = "B",
        max_signals: int = 5,
    ):
        self.account_balance = account_balance
        self.min_grade = SignalGrade(min_grade)
        self.max_signals = max_signals

        self._detector = SetupDetector()
        self._grader = GradeCalculator()
        self._sizer = PositionSizer()

    def is_market_time(self) -> bool:
        try:
            import pytz
            tz = pytz.timezone("Asia/Kolkata")
            now = datetime.datetime.now(tz)
            t = now.time()
            # Monday=0, Sunday=6
            if now.weekday() >= 5:
                return False
            return datetime.time(9, 15) <= t <= datetime.time(15, 30)
        except Exception:
            now = datetime.datetime.now()
            t = now.time()
            return datetime.time(9, 15) <= t <= datetime.time(15, 30)

    async def scan_all(
        self,
        user,
        symbols: Optional[list] = None,
    ) -> list[GradedSignal]:
        """
        Sabhi symbols scan karo — best graded signals return karo.
        """
        # Indian market time check (crypto 24x7 chal sakta hai)
        market_open = self.is_market_time()
        if not market_open:
            logger.info("ICTScreener: Indian market closed — crypto only scan")
            # Sirf crypto scan karo
            if symbols is None:
                symbols = []  # Indian symbols skip

        from apps.strategies.ict_integration import FyersDataProvider, DeltaDataProvider
        from .ict import run_mtf_analysis
        from .runner import DataProvider

        # Indian symbols
        indian_symbols = (symbols or self.SYMBOLS) if market_open else []
        fyers_provider = FyersDataProvider(user=user, days_back=60)

        # Crypto symbols (market time check bypass — 24x7)
        crypto_provider = DeltaDataProvider()

        indian_results = await asyncio.gather(
            *[self._scan_symbol(sym, fyers_provider) for sym in indian_symbols],
            return_exceptions=True,
        )
        crypto_results = await asyncio.gather(
            *[self._scan_symbol(sym, crypto_provider, is_crypto=True) for sym in self.CRYPTO_SYMBOLS],
            return_exceptions=True,
        )
        results = list(indian_results) + list(crypto_results)
        symbols = indian_symbols + self.CRYPTO_SYMBOLS

        signals = []
        for sym, result in zip(symbols, results):
            if isinstance(result, Exception):
                logger.error("Screener error [%s]: %s", sym, result)
                continue
            if result is not None:
                signals.append(result)

        # Sort by grade then confluence
        grade_order = {
            SignalGrade.A_PLUS: 0,
            SignalGrade.A: 1,
            SignalGrade.B: 2,
            SignalGrade.C: 3,
        }
        signals.sort(key=lambda s: (grade_order[s.grade], -s.confluence_score))

        # Filter by min grade
        min_order = grade_order[self.min_grade]
        signals = [s for s in signals if grade_order[s.grade] <= min_order]

        # Cap
        signals = signals[: self.max_signals]

        for sig in signals:
            logger.info(sig.summary())

        return signals

    async def _scan_symbol(
        self,
        symbol: str,
        provider,
        is_crypto: bool = False,
    ) -> Optional[GradedSignal]:
        """Single symbol ICT scan → GradedSignal."""
        from .ict import run_mtf_analysis

        try:
            tf_data = await provider.fetch_many(
                symbol,
                self.TF_CONFIG["timeframes"],
                self.TF_CONFIG["bars_per_tf"],
            )

            valid = {k: v for k, v in tf_data.items() if not v.empty}
            if len(valid) < 2:
                logger.warning("Screener [%s]: insufficient data", symbol)
                return None

            mtf = run_mtf_analysis(
                symbol=symbol,
                tf_data=valid,
                anchor_tf=self.TF_CONFIG["anchor_tf"],
                execution_tf=self.TF_CONFIG["execution_tf"],
            )

        except Exception as e:
            logger.error("Screener._scan_symbol [%s]: %s", symbol, e)
            return None

        confluence = mtf.confluence
        if confluence is None or confluence.direction is None:
            return None

        direction = confluence.direction

        # Setup detection
        setup_type, notes = self._detector.detect(mtf)
        if setup_type == SetupType.NO_SETUP:
            return None

        # Entry / SL / TP from execution TF
        exec_snap = mtf.snapshots.get(self.TF_CONFIG["execution_tf"])
        if exec_snap is None:
            return None

        entry, stop, tp1, tp2, tp3 = self._calculate_levels(direction, exec_snap, mtf)
        if entry is None:
            return None

        risk = abs(entry - stop)
        if risk == 0:
            return None

        rr = round(abs(tp1 - entry) / risk, 2)

        # Grade
        grade = self._grader.calculate(setup_type, confluence.total, rr, mtf, direction)

        if grade == SignalGrade.C:
            return None

        # Position size
        lots, risk_amount, risk_pct = self._sizer.calculate(
            grade, entry, stop, symbol, self.account_balance
        )

        # Tags
        tags = self._build_tags(direction, exec_snap, mtf, setup_type)

        # KZ name
        kz_name = ""
        if mtf.killzone and mtf.killzone.highest_priority_zone:
            kz_name = mtf.killzone.highest_priority_zone.name.value

        return GradedSignal(
            symbol=symbol,
            direction=direction,
            setup_type=setup_type,
            grade=grade,
            entry_price=round(entry, 2),
            stop_loss=round(stop, 2),
            take_profit_1=round(tp1, 2),
            take_profit_2=round(tp2, 2) if tp2 else None,
            take_profit_3=round(tp3, 2) if tp3 else None,
            risk_reward=rr,
            confluence_score=confluence.total,
            position_size=lots,
            risk_amount=risk_amount,
            risk_pct=risk_pct,
            tags=tags,
            killzone=kz_name,
            timeframes=list(mtf.snapshots.keys()),
            breakdown=confluence.breakdown,
            notes=notes,
            market_type="crypto" if is_crypto else "indian",
        )

    def _calculate_levels(
        self,
        direction: str,
        snap: TFSnapshot,
        mtf: MTFAnalysis,
    ):
        """Entry, SL, TP levels calculate karo."""
        import numpy as np

        ob = snap.nearest_ob
        fvg = snap.nearest_fvg

        # Current price from last candle — not stored in snap directly
        # Use OB proximal as entry
        if ob and ob.status in {OBStatus.PRISTINE, OBStatus.TESTED}:
            if direction == "long" and ob.ob_type == OBType.BULLISH:
                entry = ob.proximal
                stop = ob.distal * 0.999
            elif direction == "short" and ob.ob_type == OBType.BEARISH:
                entry = ob.proximal
                stop = ob.distal * 1.001
            else:
                return None, None, None, None, None
        elif fvg and fvg.status in {FVGStatus.OPEN, FVGStatus.PARTIAL}:
            entry = fvg.mid
            if direction == "long":
                stop = fvg.bottom * 0.999
            else:
                stop = fvg.top * 1.001
        else:
            return None, None, None, None, None

        # Validate
        if direction == "long" and stop >= entry:
            return None, None, None, None, None
        if direction == "short" and stop <= entry:
            return None, None, None, None, None

        risk = abs(entry - stop)

        # Targets from liquidity levels
        liq = snap.liquidity
        tp1 = tp2 = tp3 = None

        if direction == "long":
            bsl = sorted(
                [
                    l
                    for l in liq.bsl_levels
                    if l.price > entry and l.status == LiqStatus.INTACT
                ],
                key=lambda x: x.price,
            )
            tp1 = bsl[0].price if bsl else entry + risk * 2.0
            tp2 = bsl[1].price if len(bsl) > 1 else entry + risk * 3.0
            tp3 = bsl[2].price if len(bsl) > 2 else entry + risk * 4.5
        else:
            ssl = sorted(
                [
                    l
                    for l in liq.ssl_levels
                    if l.price < entry and l.status == LiqStatus.INTACT
                ],
                key=lambda x: x.price,
                reverse=True,
            )
            tp1 = ssl[0].price if ssl else entry - risk * 2.0
            tp2 = ssl[1].price if len(ssl) > 1 else entry - risk * 3.0
            tp3 = ssl[2].price if len(ssl) > 2 else entry - risk * 4.5

        # Ensure minimum RR
        actual_rr = abs(tp1 - entry) / risk if risk else 0
        if actual_rr < 2.0:
            tp1 = entry + risk * 2.0 if direction == "long" else entry - risk * 2.0

        return entry, stop, tp1, tp2, tp3

    def _build_tags(
        self,
        direction: str,
        snap: TFSnapshot,
        mtf: MTFAnalysis,
        setup_type: SetupType,
    ) -> list:
        tags = [direction.upper(), setup_type.value]

        ob = snap.nearest_ob
        if ob:
            tags.append("OB")
            if ob.is_institutional:
                tags.append("INST_OB")
            if ob.status == OBStatus.PRISTINE:
                tags.append("PRISTINE")

        fvg = snap.nearest_fvg
        if fvg:
            tags.append("FVG")
            if fvg.is_significant:
                tags.append("SIG_FVG")

        if snap.last_choch:
            tags.append("CHoCH")
        if snap.last_bos:
            tags.append("BOS")

        sweeps = snap.liquidity.recent_sweeps
        for s in sweeps[:2]:
            if s.is_stop_hunt:
                tags.append("STOP_HUNT")
                break

        if mtf.killzone and mtf.killzone.in_any_killzone:
            if mtf.killzone.highest_priority_zone:
                kz = mtf.killzone.highest_priority_zone.name.value.upper()
                tags.append(f"KZ_{kz}")

        if mtf.aligned:
            tags.append("MTF_ALIGNED")

        return list(dict.fromkeys(tags))  # deduplicate


# ─── Celery Task Integration ──────────────────────────────────
def run_screener_sync(user, strategy=None) -> list[dict]:
    """
    Sync wrapper — Celery task se call karo.
    Returns list of signal dicts for WS push.
    """
    account_balance = 100_000
    if strategy:
        account_balance = float(strategy.parameters.get("capital", 100_000))

    screener = ICTScreener(
        account_balance=account_balance,
        min_grade="B",
        max_signals=5,
    )

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        signals = loop.run_until_complete(screener.scan_all(user))
        loop.close()
    except Exception as e:
        logger.error("run_screener_sync error: %s", e, exc_info=True)
        return []

    return [s.to_dict() for s in signals]


def push_screener_signals(user, strategy=None):
    signals = run_screener_sync(user, strategy)
    if not signals:
        return

    # DB mein save karo
    try:
        from apps.live_trading.models import LiveSignal, TradingSession
        from decimal import Decimal
        session, _ = TradingSession.objects.get_or_create(
            user=user,
            is_active=True,
            defaults={"mode": "paper"},
        )
        for sig in signals:
            LiveSignal.objects.get_or_create(
                user=user,
                symbol=sig["symbol"],
                direction=sig["direction"],
                entry_price=Decimal(str(sig["entry_price"])),
                defaults=dict(
                    session=session,
                    signal_type=sig.get("setup_type", "ICT"),
                    strength=min(100, int(sig.get("confluence", 60))),
                    stop_loss=Decimal(str(sig["stop_loss"])),
                    take_profit=Decimal(str(sig["take_profit_1"])),
                    rr_ratio=Decimal(str(sig.get("risk_reward", 2.0))),
                    lots=Decimal(str(sig.get("position_size", 1.0))),
                    mode="paper",
                    status="active",
                    raw_payload=sig,
                ),
            )
        logger.info("DB: %d signals saved", len(signals))
    except Exception as db_err:
        logger.error("DB save failed: %s", db_err)

    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        group_name = f"user_{user.id}"

        CRYPTO_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"}

        for sig in signals:
            symbol_upper = sig["symbol"].upper()

            # ✅ Symbol type detect karo
            is_crypto = symbol_upper in CRYPTO_SYMBOLS

            # ✅ Delta-compatible symbol format
            delta_symbol = None
            if is_crypto:
                delta_symbol = symbol_upper.replace(
                    "USDT", "-USDT"
                )  # BTCUSDT → BTC-USDT

            async_to_sync(layer.group_send)(
                group_name,
                {
                    "type": "new_signal",
                    "direction": sig["direction"],
                    "symbol": sig["symbol"],
                    "delta_symbol": delta_symbol,  # ✅ NEW
                    "market_type": "crypto" if is_crypto else "indian",  # ✅ NEW
                    "entry": sig["entry_price"],
                    "sl": sig["stop_loss"],
                    "target1": sig["take_profit_1"],
                    "tp": sig["take_profit_1"],  # adapter ke liye
                    "confidence": sig["confluence"],
                    "reason": sig["notes"],
                    "grade": sig["grade"],
                    "setup": sig["setup_type"],
                    "rr": sig["risk_reward"],
                    "position": sig["position_size"],
                    "risk_inr": sig["risk_amount"],
                    "tags": sig["tags"],
                    "breakdown": sig["breakdown"],
                    "grade_emoji": sig["grade_emoji"],
                    "strategy": "ICT",
                    "qty": sig.get("position_size", 0.01),
                    "leverage": 10,
                },
            )
    except Exception as e:
        logger.error("push_screener_signals error: %s", e)
