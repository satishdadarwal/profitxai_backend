# apps/live_trading/screener_views.py
#
# ICT Screener REST API — 4 endpoints
# ─────────────────────────────────────────────────────────────────
#
#   GET  /api/live-trading/screener/signals/
#        → DB mein saved recent signals return karo (paginated)
#        → Flutter WebSocket ke saath parallel: initial load REST se,
#          phir WS se live updates
#
#   POST /api/live-trading/screener/scan/
#        → Turant ek on-demand ICT scan trigger karo
#        → Plan check: Pro = 5 scans/day, Elite = unlimited
#        → Result WS se push hoga + response mein bhi milega
#
#   GET  /api/live-trading/screener/stats/
#        → Aaj ka screener stats: total signals, A+ count, scan count
#
#   GET  /api/live-trading/screener/performance/
#        → Weekly historical ICT signal performance (day-wise)
#
# Plan gating (backend side):
#   Free  → sirf recent 5 signals, no manual scan
#   Pro   → 50 signals, 5 manual scans/day, A+ filter, 3 days history
#   Elite → unlimited signals, unlimited scans, live scan rate, 7 days
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

import datetime
import logging

from django.core.cache import cache
from django.db.models import Avg, Sum
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import LiveSignal

logger = logging.getLogger(__name__)

# ── Plan tier constants — sync with subscriptions/models.py ──────
_TIER_FREE  = 0
_TIER_BASIC = 1
_TIER_PRO   = 2
_TIER_ELITE = 3


# ── Plan tier helper ──────────────────────────────────────────────
def _user_tier(user) -> int:
    """
    User ka current subscription tier return karo (int).
    0=free, 1=basic, 2=pro, 3=elite
    Fallback: user.plan string field se
    """
    try:
        sub = user.subscription
        if sub and sub.is_access_granted and sub.plan:
            return sub.plan.tier
    except Exception:
        pass

    _MAP = {"free": 0, "basic": 1, "pro": 2, "elite": 3}
    return _MAP.get(getattr(user, "plan", "free"), 0)


def _plan_name(tier: int) -> str:
    return {0: "free", 1: "basic", 2: "pro", 3: "elite"}.get(tier, "free")


# ═════════════════════════════════════════════════════════════════
#  1. GET /api/live-trading/screener/signals/
#     Recent ICT signals — plan ke hisaab se limit
# ═════════════════════════════════════════════════════════════════
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def screener_signals(request):
    """
    GET /api/live-trading/screener/signals/

    Query params:
        grade     → filter by grade: A+, A, B  (optional)
        strategy  → filter by signal_type       (optional)
        limit     → override limit (max capped by plan)
        hours     → signals from last N hours (default 24)

    Response:
        {
          "success": true,
          "signals": [...],
          "count": 12,
          "plan": "pro",
          "limit_info": {
            "max_signals": 50,
            "next_scan_in_sec": null,
            "can_manual_scan": true,
            "a_plus_access": true
          }
        }
    """
    tier = _user_tier(request.user)

    _LIMITS = {
        _TIER_FREE:  5,
        _TIER_BASIC: 20,
        _TIER_PRO:   50,
        _TIER_ELITE: 200,
    }
    max_signals = _LIMITS.get(tier, 5)

    grade    = request.query_params.get("grade")
    strategy = request.query_params.get("strategy")
    hours    = int(request.query_params.get("hours", 24))
    limit    = min(int(request.query_params.get("limit", max_signals)), max_signals)

    since = timezone.now() - datetime.timedelta(hours=hours)

    qs = LiveSignal.objects.filter(
        user=request.user,
        detected_at__gte=since,
    ).order_by("-detected_at")

    if grade:
        qs = qs.filter(raw_payload__grade=grade)

    if strategy:
        qs = qs.filter(signal_type__icontains=strategy)

    # Free/Basic users: A+ signals nahi milenge
    if tier < _TIER_PRO:
        qs = qs.exclude(raw_payload__grade="A+")

    signals = qs[:limit]

    signal_list = []
    for s in signals:
        raw = s.raw_payload or {}
        signal_list.append({
            "signal_id":    s.id,
            "symbol":       s.symbol,
            "direction":    s.direction,
            "signal_type":  s.signal_type,
            "strength":     s.strength,
            "entry":        float(s.entry_price),
            "sl":           float(s.stop_loss),
            "target1":      float(s.take_profit),
            "rr":           float(s.rr_ratio),
            "lots":         float(s.lots),
            "grade":        raw.get("grade", "B"),
            "grade_emoji":  raw.get("grade_emoji", "🔵"),
            "setup":        raw.get("setup", s.signal_type),
            "confidence":   raw.get("confluence", raw.get("confidence", 60)),
            "risk_inr":     raw.get("risk_inr", 0),
            "position":     raw.get("position", float(s.lots)),
            "tags":         raw.get("tags", []),
            "reason":       raw.get("reason", ""),
            "market_type":  raw.get("market_type", "indian"),
            "delta_symbol": raw.get("delta_symbol"),
            "breakdown":    raw.get("breakdown", {}),
            "killzone":     raw.get("killzone", ""),
            "strategy":     raw.get("strategy", "ICT"),
            "qty":          raw.get("qty", float(s.lots)),
            "leverage":     raw.get("leverage", 10),
            "status":       s.status,
            "mode":         s.mode,
            "received_at":  s.detected_at.isoformat(),
        })

    next_scan_in = _get_next_scan_allowed_in(request.user, tier)

    return Response({
        "success":    True,
        "signals":    signal_list,
        "count":      len(signal_list),
        "plan":       _plan_name(tier),
        "limit_info": {
            "max_signals":      max_signals,
            "next_scan_in_sec": next_scan_in,
            "can_manual_scan":  tier >= _TIER_PRO,
            "a_plus_access":    tier >= _TIER_PRO,
        },
    })


# ═════════════════════════════════════════════════════════════════
#  2. POST /api/live-trading/screener/scan/
#     On-demand ICT scan trigger
# ═════════════════════════════════════════════════════════════════
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def screener_scan(request):
    """
    POST /api/live-trading/screener/scan/

    Body (optional):
        { "symbols": ["NIFTY", "BANKNIFTY"] }   ← Pro can specify
        { "min_grade": "A" }                     ← Elite can set

    Plan limits:
        Free/Basic → 403 Forbidden
        Pro        → 5 scans per day, 5 min cooldown
        Elite      → unlimited, 1 min cooldown

    Response:
        {
          "success": true,
          "message": "2 signal(s) mila",
          "signals_found": 2,
          "signals": [...],
          "scans_remaining_today": 4
        }
    """
    tier = _user_tier(request.user)

    # Plan gate
    if tier < _TIER_PRO:
        return Response({
            "success":    False,
            "error":      "manual_scan_not_allowed",
            "message":    "Manual scan Pro plan mein available hai. Upgrade karein.",
            "upgrade_to": "pro",
        }, status=403)

    # Cooldown check
    cooldown = _get_next_scan_allowed_in(request.user, tier)
    if cooldown is not None:
        return Response({
            "success":          False,
            "error":            "scan_cooldown",
            "message":          f"Scan {cooldown} seconds mein available hoga.",
            "next_scan_in_sec": cooldown,
        }, status=429)

    body      = request.data or {}
    symbols   = body.get("symbols")
    min_grade = body.get("min_grade", "B")

    # Non-Elite users can't request A+ only
    if tier < _TIER_ELITE and min_grade == "A+":
        min_grade = "A"

    # ── Run scan ─────────────────────────────────────────────────
    try:
        from apps.ict_engine.screener import run_screener_sync

        raw_signals = run_screener_sync(
            user=request.user,
            strategy=None,
        )

        # WS push
        if raw_signals:
            try:
                from asgiref.sync import async_to_sync
                from channels.layers import get_channel_layer

                CRYPTO_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"}
                group_name = f"user_{request.user.id}"
                layer = get_channel_layer()

                for sig in raw_signals:
                    sym_upper    = sig["symbol"].upper()
                    is_crypto    = sym_upper in CRYPTO_SYMBOLS
                    delta_symbol = sym_upper.replace("USDT", "-USDT") if is_crypto else None

                    async_to_sync(layer.group_send)(group_name, {
                        "type":         "new_signal",
                        "direction":    sig["direction"],
                        "symbol":       sig["symbol"],
                        "delta_symbol": delta_symbol,
                        "market_type":  "crypto" if is_crypto else "indian",
                        "entry":        sig["entry_price"],
                        "sl":           sig["stop_loss"],
                        "target1":      sig["take_profit_1"],
                        "tp":           sig["take_profit_1"],
                        "confidence":   sig["confluence"],
                        "reason":       sig["notes"],
                        "grade":        sig["grade"],
                        "setup":        sig["setup_type"],
                        "rr":           sig["risk_reward"],
                        "position":     sig["position_size"],
                        "risk_inr":     sig["risk_amount"],
                        "tags":         sig["tags"],
                        "breakdown":    sig["breakdown"],
                        "grade_emoji":  sig["grade_emoji"],
                        "strategy":     "ICT",
                        "qty":          sig.get("position_size", 0.01),
                        "leverage":     10,
                    })
            except Exception as ws_err:
                logger.warning("screener_scan: WS push failed: %s", ws_err)

    except Exception as e:
        logger.error(
            "screener_scan: error | user=%s | %s", request.user.pk, e, exc_info=True
        )
        return Response({
            "success": False,
            "error":   "scan_failed",
            "message": "Scan mein error aaya. Dobara try karein.",
        }, status=500)

    _record_scan(request.user, tier)

    return Response({
        "success":               True,
        "message":               f"{len(raw_signals)} signal(s) mila",
        "signals_found":         len(raw_signals),
        "scans_remaining_today": _scans_remaining_today(request.user, tier),
        "signals":               raw_signals,
    })


# ═════════════════════════════════════════════════════════════════
#  3. GET /api/live-trading/screener/stats/
#     Aaj ka screener stats
# ═════════════════════════════════════════════════════════════════
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def screener_stats(request):
    """
    GET /api/live-trading/screener/stats/

    Response:
        {
          "success": true,
          "today": {
            "total_signals":    12,
            "a_plus_count":      3,
            "a_count":           5,
            "b_count":           4,
            "scan_count":        2,
            "scans_remaining":   3,
            "long_count":        7,
            "short_count":       5,
            "next_scan_in_sec": null
          },
          "plan":             "pro",
          "can_manual_scan":  true,
          "a_plus_access":    true,
          "live_scan_access": false
        }
    """
    tier  = _user_tier(request.user)
    today = timezone.now().replace(hour=9, minute=15, second=0, microsecond=0)

    qs = LiveSignal.objects.filter(user=request.user, detected_at__gte=today)

    total   = qs.count()
    a_plus  = qs.filter(raw_payload__grade="A+").count()
    a_grade = qs.filter(raw_payload__grade="A").count()
    b_grade = qs.filter(raw_payload__grade="B").count()
    long_c  = qs.filter(direction__in=["long", "buy"]).count()
    short_c = qs.filter(direction__in=["short", "sell"]).count()

    scan_count      = _get_scan_count_today(request.user)
    scans_remaining = _scans_remaining_today(request.user, tier)
    next_scan_in    = _get_next_scan_allowed_in(request.user, tier)

    return Response({
        "success": True,
        "today": {
            "total_signals":    total,
            "a_plus_count":     a_plus,
            "a_count":          a_grade,
            "b_count":          b_grade,
            "scan_count":       scan_count,
            "scans_remaining":  scans_remaining,
            "long_count":       long_c,
            "short_count":      short_c,
            "next_scan_in_sec": next_scan_in,
        },
        "plan":             _plan_name(tier),
        "can_manual_scan":  tier >= _TIER_PRO,
        "a_plus_access":    tier >= _TIER_PRO,
        "live_scan_access": tier >= _TIER_ELITE,
    })


# ═════════════════════════════════════════════════════════════════
#  4. GET /api/live-trading/screener/performance/
#     Weekly historical ICT signal performance (day-wise)
# ═════════════════════════════════════════════════════════════════
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def screener_performance(request):
    """
    GET /api/live-trading/screener/performance/

    Query params:
        days  → 1, 3, or 7 (capped by plan)

    Plan gating:
        Free/Basic → sirf today's summary, no day detail
        Pro        → 3 days detail + grade accuracy in summary
        Elite      → 7 days detail + grade accuracy per day + summary

    Response:
        {
          "success": true,
          "plan": "pro",
          "days_allowed": 3,
          "summary": {
            "total_signals": 18,
            "executed":       8,
            "wins":           5,
            "losses":         3,
            "win_rate":      62.5,
            "total_pnl":   4200.0,
            "best_trade":  { ... },
            "worst_trade": { ... },
            "grade_accuracy": {
              "A+": { "count": 3, "wins": 3, "win_rate": 100.0 },
              "A":  { "count": 3, "wins": 2, "win_rate": 66.7 },
              "B":  { "count": 2, "wins": 0, "win_rate": 0.0  }
            }
          },
          "days": [
            {
              "date":       "2025-05-16",
              "label":      "Yesterday",
              "signals":     6,
              "executed":    3,
              "wins":        2,
              "losses":      1,
              "win_rate":   66.7,
              "total_pnl": 1800.0,
              "best_trade": { ... },
              "trades":     [ ... ]
            },
            ...
          ]
        }
    """
    tier = _user_tier(request.user)

    _DAY_LIMITS = {
        _TIER_FREE:  0,   # summary only
        _TIER_BASIC: 1,
        _TIER_PRO:   3,
        _TIER_ELITE: 7,
    }
    days_allowed = _DAY_LIMITS.get(tier, 0)

    requested_days = int(request.query_params.get("days", days_allowed or 1))
    days_to_show   = min(requested_days, days_allowed)

    # Free users → summary only, no day-level detail
    if tier < _TIER_BASIC:
        summary = _build_summary(request.user, days=1, include_grade_accuracy=False)
        return Response({
            "success":      True,
            "plan":         _plan_name(tier),
            "days_allowed": 0,
            "upgrade_msg":  "Pro plan mein 3 din ka detailed history milega.",
            "summary":      summary,
            "days":         [],
        })

    today = timezone.now().date()
    days_data = []

    for i in range(days_to_show):
        day   = today - datetime.timedelta(days=i)
        label = (
            "Today"     if i == 0 else
            "Yesterday" if i == 1 else
            day.strftime("%a, %b %d")
        )
        days_data.append(_build_day_data(
            user=request.user,
            date=day,
            label=label,
            include_grade_accuracy=(tier >= _TIER_ELITE),
        ))

    summary = _build_summary(
        request.user,
        days=days_to_show,
        include_grade_accuracy=(tier >= _TIER_PRO),
    )

    return Response({
        "success":      True,
        "plan":         _plan_name(tier),
        "days_allowed": days_allowed,
        "summary":      summary,
        "days":         days_data,
    })


# ─────────────────────────────────────────────────────────────────
#  Performance helpers
# ─────────────────────────────────────────────────────────────────

def _build_day_data(user, date, label, include_grade_accuracy: bool) -> dict:
    """Ek din ka signal + trade performance."""
    from apps.orders.models import Position

    day_start = timezone.make_aware(
        datetime.datetime.combine(date, datetime.time(9, 15))
    )
    day_end = timezone.make_aware(
        datetime.datetime.combine(date, datetime.time(15, 30))
    )

    signals_qs = LiveSignal.objects.filter(
        user=user,
        detected_at__gte=day_start,
        detected_at__lte=day_end,
    )
    total_signals = signals_qs.count()

    signal_ids   = signals_qs.values_list("id", flat=True)
    positions_qs = Position.objects.filter(
        user=user,
        live_signal_id__in=signal_ids,
        status="closed",
    ).select_related("live_signal")

    executed  = positions_qs.count()
    wins      = positions_qs.filter(outcome="win").count()
    losses    = positions_qs.filter(outcome="loss").count()
    win_rate  = round(wins / executed * 100, 1) if executed else 0
    total_pnl = float(positions_qs.aggregate(s=Sum("realized_pnl"))["s"] or 0)

    best_pos  = positions_qs.order_by("-realized_pnl").first()
    worst_pos = positions_qs.order_by("realized_pnl").first()

    result = {
        "date":       str(date),
        "label":      label,
        "signals":    total_signals,
        "executed":   executed,
        "wins":       wins,
        "losses":     losses,
        "win_rate":   win_rate,
        "total_pnl":  round(total_pnl, 0),
        "best_trade": _position_to_dict(best_pos),
        "worst_trade": _position_to_dict(worst_pos),
        "trades":     [_position_to_dict(p) for p in positions_qs[:10]],
    }

    if include_grade_accuracy:
        result["grade_accuracy"] = _grade_accuracy(positions_qs)

    return result


def _build_summary(user, days: int, include_grade_accuracy: bool) -> dict:
    """Multi-day overall summary."""
    from apps.orders.models import Position

    since      = timezone.now() - datetime.timedelta(days=days)
    signals_qs = LiveSignal.objects.filter(user=user, detected_at__gte=since)
    signal_ids = signals_qs.values_list("id", flat=True)

    positions_qs = Position.objects.filter(
        user=user,
        live_signal_id__in=signal_ids,
        status="closed",
    ).select_related("live_signal")

    executed  = positions_qs.count()
    wins      = positions_qs.filter(outcome="win").count()
    losses    = positions_qs.filter(outcome="loss").count()
    win_rate  = round(wins / executed * 100, 1) if executed else 0
    total_pnl = float(positions_qs.aggregate(s=Sum("realized_pnl"))["s"] or 0)

    best_pos  = positions_qs.order_by("-realized_pnl").first()
    worst_pos = positions_qs.order_by("realized_pnl").first()

    summary = {
        "total_signals": signals_qs.count(),
        "executed":      executed,
        "wins":          wins,
        "losses":        losses,
        "win_rate":      win_rate,
        "total_pnl":     round(total_pnl, 0),
        "best_trade":    _position_to_dict(best_pos),
        "worst_trade":   _position_to_dict(worst_pos),
    }

    if include_grade_accuracy:
        summary["grade_accuracy"] = _grade_accuracy(positions_qs)

    return summary


def _position_to_dict(pos) -> dict:
    """Position model → Flutter-ready dict."""
    if pos is None:
        return {}

    raw        = pos.live_signal.raw_payload if pos.live_signal else {}
    grade      = raw.get("grade", "B")
    setup      = raw.get("setup", "ICT")
    emoji      = raw.get("grade_emoji", "🔵")
    rr_planned = float(pos.live_signal.rr_ratio) if pos.live_signal else 0

    entry  = float(pos.avg_entry_price or 0)
    exit_p = float(pos.current_price or 0)
    pnl    = float(pos.realized_pnl or 0)
    sl     = float(pos.live_signal.stop_loss or 0) if pos.live_signal else 0

    risk_per_unit = abs(entry - sl) if sl else 1
    rr_achieved   = round(abs(exit_p - entry) / risk_per_unit, 1) if risk_per_unit else 0

    outcome = pos.outcome or ""

    return {
        "symbol":      pos.live_signal.symbol    if pos.live_signal else "—",
        "direction":   pos.live_signal.direction if pos.live_signal else "—",
        "grade":       grade,
        "grade_emoji": emoji,
        "setup":       setup.replace("ICT_", ""),
        "entry":       round(entry, 0),
        "exit":        round(exit_p, 0),
        "sl":          round(sl, 0),
        "pnl":         round(pnl, 0),
        "outcome":     outcome,
        "is_win":      outcome == "win",
        "rr_planned":  rr_planned,
        "rr_achieved": rr_achieved,
        "closed_at":   pos.closed_at.isoformat() if pos.closed_at else None,
        "tags":        raw.get("tags", []),
    }


def _grade_accuracy(positions_qs) -> dict:
    """Grade-wise win rate calculate karo."""
    result = {}
    for grade in ["A+", "A", "B"]:
        grade_qs = positions_qs.filter(live_signal__raw_payload__grade=grade)
        count    = grade_qs.count()
        wins     = grade_qs.filter(outcome="win").count()
        result[grade] = {
            "count":    count,
            "wins":     wins,
            "win_rate": round(wins / count * 100, 1) if count else 0,
        }
    return result


# ─────────────────────────────────────────────────────────────────
#  Rate limit helpers — Redis cache se
# ─────────────────────────────────────────────────────────────────

_DAILY_SCAN_LIMITS = {
    _TIER_FREE:  0,
    _TIER_BASIC: 0,
    _TIER_PRO:   5,
    _TIER_ELITE: 9999,  # effectively unlimited
}

_SCAN_COOLDOWN_SEC = {
    _TIER_PRO:   300,   # 5 min between scans
    _TIER_ELITE: 60,    # 1 min between scans
}


def _scan_cache_key(user) -> str:
    date_str = timezone.now().strftime("%Y%m%d")
    return f"screener_scan:{user.pk}:{date_str}"


def _last_scan_key(user) -> str:
    return f"screener_last_scan:{user.pk}"


def _get_scan_count_today(user) -> int:
    return int(cache.get(_scan_cache_key(user), 0))


def _scans_remaining_today(user, tier: int) -> int | None:
    """None = unlimited (Elite)."""
    if tier >= _TIER_ELITE:
        return None
    limit = _DAILY_SCAN_LIMITS.get(tier, 0)
    used  = _get_scan_count_today(user)
    return max(0, limit - used)


def _get_next_scan_allowed_in(user, tier: int) -> int | None:
    """
    Returns seconds until next scan is allowed.
    None = can scan right now.
    """
    if tier < _TIER_PRO:
        return None  # not allowed at all — handled by plan gate

    # Daily limit exhausted?
    if tier < _TIER_ELITE:
        remaining = _scans_remaining_today(user, tier)
        if remaining is not None and remaining <= 0:
            now = timezone.now()
            tomorrow_open = (now + datetime.timedelta(days=1)).replace(
                hour=9, minute=15, second=0, microsecond=0
            )
            return int((tomorrow_open - now).total_seconds())

    # Per-scan cooldown
    last_scan = cache.get(_last_scan_key(user))
    if last_scan is None:
        return None

    cooldown      = _SCAN_COOLDOWN_SEC.get(tier, 300)
    elapsed       = (timezone.now() - last_scan).total_seconds()
    remaining_sec = int(cooldown - elapsed)
    return remaining_sec if remaining_sec > 0 else None


def _record_scan(user, tier: int):
    """Cache mein scan count aur timestamp record karo."""
    key   = _scan_cache_key(user)
    count = int(cache.get(key, 0)) + 1

    now      = timezone.now()
    midnight = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    ttl = int((midnight - now).total_seconds())
    cache.set(key, count, timeout=ttl)
    cache.set(_last_scan_key(user), now, timeout=86400)