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
    # ── Market hours guard (NSE: 9:15–15:30 IST) ──
    # Crypto (Delta) always open; skip check for paper mode too
    # ✅ FIX: broker se is_crypto check nahi — paper mode mein broker=None hota hai.
    # Symbol se detect karo (BTC/ETH/USDT) ya broker slug se (jab live ho).
    broker_slug = getattr(strategy.broker, "broker", "") if strategy.broker else ""
    _sym_upper = str(getattr(signal, "symbol", "") or "").upper()
    _CRYPTO_KW = {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "USDT"}
    is_crypto = broker_slug == "delta" or any(kw in _sym_upper for kw in _CRYPTO_KW)
    if not is_crypto and strategy.mode != "paper":
        from apps.strategies.services import _is_market_time
        if not _is_market_time():
            logger.warning(
                "Market closed — order blocked | strategy=%s | symbol=%s",
                strategy.id, signal.symbol,
            )
            return None

    # ── ✅ GLOBAL STRATEGY: har subscriber ke broker se order place karo ──
    # is_global=True strategy sirf creator (Satish) ke broker se nahi chalegee —
    # balki har eligible subscriber (Chanchal etc.) ke apne broker se order jayega.
    if strategy.is_global:
        return _route_global_strategy(strategy, signal)

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
        # ✅ SCALABLE: BROKER_ORDER_FUNCTIONS registry use karo
        # Naya broker: wahan add karo, yahan kuch nahi badalna
        fn_name = BROKER_ORDER_FUNCTIONS.get(broker_slug)
        if fn_name is None:
            logger.error(
                "route_and_place_order: broker=%s ke liye function nahi | "
                "BROKER_ORDER_FUNCTIONS mein add karo",
                broker_slug,
            )
            return None

        fn = globals().get(fn_name)
        if fn is None:
            logger.error("route_and_place_order: function %s not found", fn_name)
            return None

        # ✅ SECURITY: user ka APNA account — strategy.broker (admin ka) nahi
        user_account = _pick_best_account_for_user(strategy.user, instrument_type)
        if not user_account:
            logger.warning(
                "⛔ route_and_place_order: user=%s ke paas %s ke liye koi broker nahi",
                strategy.user.pk, instrument_type,
            )
            return None

        if user_account.user_id != strategy.user.pk:
            logger.critical(
                "🚨 SECURITY: account %s belongs to user %s but strategy.user=%s — BLOCKING",
                user_account.id, user_account.user_id, strategy.user.pk,
            )
            return None

        return fn(strategy, signal, instrument_type,
                  user=strategy.user, account=user_account)

    except Exception as exc:
        logger.exception(
            "Order placement failed | strategy=%s | broker=%s | %s",
            strategy.id,
            broker_slug,
            exc,
        )
        return None


# ─────────────────────────────────────────────────────────────────
#  GLOBAL STRATEGY ROUTING
#  Global strategy ke liye har eligible subscriber ke broker se
#  alag-alag order place karo.
# ─────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────
#  BROKER CAPABILITY MAP
#  Scalable broker registry — naya broker add karo toh sirf yahan daalo.
#  instrument_type → us instrument ko support karne wale brokers ki list
# ─────────────────────────────────────────────────────────────────

BROKER_CAPABILITIES: dict[str, list[str]] = {
    # Indian market instruments — priority order (first = preferred)
    # Dhan first: fresh token, reliable API
    # Zerodha second: stable OAuth, widely used
    # Fyers third: auto-refresh support
    "options":  ["dhan", "zerodha", "fyers", "upstox", "angel"],
    "futures":  ["dhan", "zerodha", "fyers", "upstox", "angel"],
    "equity":   ["dhan", "zerodha", "fyers", "upstox", "angel"],
    # Crypto instruments
    "perp":     ["delta", "binance", "bybit", "okx"],
    "crypto":   ["delta", "binance", "bybit", "okx"],
}

# Broker → order placement function mapping
# Naya broker add karna ho: function likhkar yahan register karo
# Zerodha ka dedicated _place_zerodha_order function hai (KiteConnect adapter)
BROKER_ORDER_FUNCTIONS: dict[str, str] = {
    "fyers":   "_place_fyers_order",
    "delta":   "_place_delta_order",
    "dhan":    "_place_dhan_order",
    "zerodha": "_place_zerodha_order",   # ✅ dedicated function
    "upstox":  "_place_fyers_order",     # placeholder until dedicated fn
}


def _get_eligible_brokers_for_instrument(instrument_type: str) -> list[str]:
    """
    Instrument type ke liye eligible broker slugs return karo.
    Unknown instrument → empty list (order block).
    """
    return BROKER_CAPABILITIES.get(instrument_type.lower(), [])


def _pick_best_account_for_user(
    user, instrument_type: str, exclude_user_ids: set = None
):
    """
    User ka sabse best BrokerAccount dhundo jo is instrument ko support karta ho.

    Priority: BROKER_CAPABILITIES list ka order (first = preferred).
    e.g. options → fyers pehle, phir dhan, phir zerodha...

    Returns: BrokerAccount ya None
    """
    from apps.brokers.models import BrokerAccount

    eligible_slugs = _get_eligible_brokers_for_instrument(instrument_type)
    if not eligible_slugs:
        logger.warning(
            "_pick_best_account_for_user: no brokers support instrument=%s",
            instrument_type,
        )
        return None

    # Sab eligible accounts ek query mein lo
    accounts = (
        BrokerAccount.objects
        .filter(
            user=user,
            broker__in=eligible_slugs,
            is_active=True,
            is_verified=True,
        )
        .exclude(
            # Dhan: dhan_access_token empty hone pe bhi filter karo
        )
        .select_related("user")
        .order_by("-updated_at")
    )

    # Filter: broker ke hisaab se token check karo
    valid_accounts = []
    for acc in accounts:
        if acc.broker == "dhan":
            if not getattr(acc, "dhan_access_token", ""):
                continue
        elif acc.broker == "delta":
            # Delta: api_key + api_secret use karta hai, access_token nahi
            if not getattr(acc, "api_key", "") or not getattr(acc, "api_secret", ""):
                continue
        else:
            if not getattr(acc, "access_token", ""):
                continue
        valid_accounts.append(acc)

    if not valid_accounts:
        return None

    # Priority order se sort karo
    slug_priority = {slug: i for i, slug in enumerate(eligible_slugs)}
    valid_accounts.sort(key=lambda a: slug_priority.get(a.broker, 999))
    return valid_accounts[0]


def _route_global_strategy(strategy, signal):
    """
    ✅ SCALABLE MULTI-BROKER ROUTING
    Admin strategy set karta hai (instrument_type) → har user apne broker se trade karta hai.

    Design:
    - Strategy mein sirf instrument_type matter karta hai (options/futures/equity/perp)
    - Admin ka broker (strategy.broker) sirf reference ke liye — users par enforce nahi
    - Har user ka apna best connected broker auto-pick hota hai (BROKER_CAPABILITIES se)
    - Naya broker add: BROKER_CAPABILITIES + BROKER_ORDER_FUNCTIONS mein daalo, bas

    9:15 pe 100 users:
    - User A (Dhan) → DhanAdapter.place_order()
    - User B (Fyers) → FyersAdapter.place_order()
    - User C (Zerodha) → ZerodhaAdapter.place_order()
    - Sab parallel, kisi ka bhi token kisi aur pe affect nahi karta

    Returns: First placed order (ya None agar koi nahi hua)
    """
    from apps.brokers.models import BrokerAccount
    from apps.strategies.models import UserStrategyPreference

    instrument_type = strategy.instrument_type
    # FIX: subscriber_strategy.user=Chanchal hota hai, real creator DB se lo
    real_strategy = getattr(strategy, '_real', strategy)
    from apps.strategies.models import Strategy as _Strategy
    try:
        _db_strat = _Strategy.objects.select_related('user').get(pk=real_strategy.id)
        creator_id = _db_strat.user.pk if _db_strat.user else None
    except Exception:
        creator_id = strategy.user.pk if strategy.user else None
    placed_orders = []

    # ── Step 1: Is strategy ko kaun chala raha hai ────────────────
    running_user_ids = list(
        UserStrategyPreference.objects.filter(
            strategy=real_strategy,
            is_running=True,
        )
        .exclude(user_id=creator_id)          # creator ko exclude karo
        .values_list("user_id", flat=True)
    )

    if not running_user_ids:
        logger.info(
            "Global strategy: koi subscriber nahi chala raha | strategy=%s",
            strategy.id,
        )
        return None

    # ── Step 2: Eligible broker slugs for this instrument ─────────
    eligible_slugs = _get_eligible_brokers_for_instrument(instrument_type)
    if not eligible_slugs:
        logger.error(
            "Global strategy: instrument_type=%s ke liye koi broker support nahi | "
            "strategy=%s",
            instrument_type, strategy.id,
        )
        return None

    # ── Step 3: Sabhi running users ke best accounts ─────────────
    # Ek query mein sab accounts lo, phir Python mein user-wise group karo
    all_accounts = (
        BrokerAccount.objects
        .filter(
            broker__in=eligible_slugs,
            is_active=True,
            is_verified=True,
            user_id__in=running_user_ids,
        )
        .select_related("user")
        .order_by("user_id", "-updated_at")
    )

    # User-wise best account pick karo (priority order se)
    slug_priority = {slug: i for i, slug in enumerate(eligible_slugs)}
    user_best_account: dict = {}
    for acc in all_accounts:
        # Token validity check
        if acc.broker == "dhan":
            if not getattr(acc, "dhan_access_token", ""):
                continue
        elif acc.broker == "delta":
            # Delta: api_key + api_secret use karta hai, access_token nahi
            if not getattr(acc, "api_key", "") or not getattr(acc, "api_secret", ""):
                continue
        else:
            if not getattr(acc, "access_token", ""):
                continue

        uid = acc.user_id
        if uid not in user_best_account:
            user_best_account[uid] = acc
        else:
            # Lower priority number = better broker
            existing_priority = slug_priority.get(user_best_account[uid].broker, 999)
            new_priority = slug_priority.get(acc.broker, 999)
            if new_priority < existing_priority:
                user_best_account[uid] = acc

    # allowed_plans filter
    if strategy.allowed_plans:
        allowed_lower = {p.lower() for p in strategy.allowed_plans}
        user_best_account = {
            uid: acc for uid, acc in user_best_account.items()
            if getattr(acc.user, "plan", "free").lower() in allowed_lower
        }

    logger.info(
        "Global strategy routing | strategy=%s | instrument=%s | "
        "running_users=%d | eligible_with_broker=%d | "
        "eligible_slugs=%s | signal=%s",
        strategy.id, instrument_type,
        len(running_user_ids), len(user_best_account),
        eligible_slugs, signal.signal_type,
    )

    # ── Step 4: Har user ke liye order place karo ─────────────────
    for user_id, account in user_best_account.items():
        subscriber = account.user
        try:
            order = _place_order_for_subscriber(
                strategy=strategy,
                signal=signal,
                user=subscriber,
                instrument_type=instrument_type,
                placed_orders=placed_orders,
                preloaded_account=account,
            )
            if order:
                logger.info(
                    "✅ Order placed | user=%s | broker=%s | strategy=%s",
                    subscriber.pk, account.broker, strategy.id,
                )
        except Exception as exc:
            logger.exception(
                "Global strategy order failed | strategy=%s | user=%s | broker=%s | %s",
                strategy.id, subscriber.pk, account.broker, exc,
            )

    logger.info(
        "Global strategy done | strategy=%s | orders_placed=%d / %d users",
        strategy.id, len(placed_orders), len(user_best_account),
    )
    return placed_orders[0] if placed_orders else None


def _place_order_for_subscriber(
    strategy, signal, user, instrument_type, placed_orders: list,
    preloaded_account=None,
):
    """
    ✅ SCALABLE: Ek subscriber user ke liye order place karo.

    preloaded_account: _route_global_strategy se pehle se fetched BrokerAccount.
    Agar None → user ka best available broker auto-pick karo (BROKER_CAPABILITIES se).
    """
    # ── Broker account: preloaded ya best-pick ────────────────────
    if preloaded_account is not None:
        account = preloaded_account
    else:
        account = _pick_best_account_for_user(user, instrument_type)

    # preferred_mode check (UserStrategyPreference)
    from apps.strategies.models import UserStrategyPreference
    try:
        real_strategy_id = getattr(strategy, '_real', strategy).id
        pref = UserStrategyPreference.objects.get(user=user, strategy_id=real_strategy_id)
        preferred_mode = pref.preferred_mode
    except UserStrategyPreference.DoesNotExist:
        preferred_mode = 'paper'

    logger.info("DEBUG preferred_mode | user=%s | strategy=%s | mode=%s", user.pk, real_strategy_id, preferred_mode)

    if preferred_mode == 'paper':
        logger.info('Global strategy | user=%s | preferred_mode=paper -> paper trade', user.pk)
        paper_order = _place_paper_trade_for_user(strategy, signal, user)
        if paper_order:
            placed_orders.append(paper_order)
        return


    # ── Live mode: broker account se order ───────────────────────
    if account:
        broker_slug = account.broker
        logger.info(
            "Global strategy | user=%s | broker=%s | placing LIVE order | instrument=%s",
            user.pk, broker_slug, instrument_type,
        )
        order = _place_order_via_broker(
            strategy=strategy,
            signal=signal,
            user=user,
            account=account,
            broker_slug=broker_slug,
            instrument_type=instrument_type,
        )
        if order:
            placed_orders.append(order)
            logger.info(
                "✅ Order placed | user=%s | broker=%s | order=%s",
                user.pk, broker_slug, order.id,
            )
        else:
            logger.warning(
                "Order nahi hua | user=%s | broker=%s", user.pk, broker_slug
            )
    else:
        # Koi bhi eligible broker connected nahi → paper trade
        logger.info(
            "Global strategy | user=%s | no eligible broker for %s → paper trade",
            user.pk, instrument_type,
        )
        paper_order = _place_paper_trade_for_user(strategy, signal, user)
        if paper_order:
            placed_orders.append(paper_order)


def _make_strategy_proxy(strategy, user):
    """
    Strategy object wrap karo taaki strategy.user = subscriber ho.
    Yeh important hai kyunki create_order() strategy.user se user uthata hai.
    Global strategy ke signals creator ke naam pe nahi, subscriber ke naam pe record honge.
    """
    class _StrategyProxy:
        def __init__(self, real_strategy, override_user):
            self._real = real_strategy
            self.user = override_user

        def __getattr__(self, name):
            return getattr(self._real, name)

    return _StrategyProxy(strategy, user)


def _place_order_via_broker(strategy, signal, user, account, broker_slug: str, instrument_type: str):
    """
    ✅ SCALABLE: Subscriber ke broker account se actual order place karo.
    BROKER_ORDER_FUNCTIONS registry se correct placement function call karo.
    Naya broker: BROKER_ORDER_FUNCTIONS mein add karo, function likho, done.

    strategy.user ko subscriber user se replace karo (proxy) taaki
    order subscriber ke naam pe record ho, creator (admin) ke naam pe nahi.
    """
    proxy = _make_strategy_proxy(strategy, user)

    # ── Registry se function dhundo ───────────────────────────────
    fn_name = BROKER_ORDER_FUNCTIONS.get(broker_slug)
    if fn_name is None:
        logger.error(
            "_place_order_via_broker: broker=%s ke liye koi function registered nahi | "
            "BROKER_ORDER_FUNCTIONS mein add karo",
            broker_slug,
        )
        return None

    fn = globals().get(fn_name)
    if fn is None:
        logger.error(
            "_place_order_via_broker: function %s not found in signal_router.py | "
            "define karo",
            fn_name,
        )
        return None

    logger.info(
        "_place_order_via_broker | user=%s | broker=%s | fn=%s | instrument=%s",
        user.pk, broker_slug, fn_name, instrument_type,
    )
    return fn(proxy, signal, instrument_type, user=user, account=account)


def _place_paper_trade_for_user(strategy, signal, user):
    """Subscriber ke liye paper trade create karo."""
    proxy = _make_strategy_proxy(strategy, user)
    try:
        return _place_paper_trade(proxy, signal)
    except Exception as e:
        logger.error("Paper trade failed | user=%s | %s", user.pk, e)
        return None


# ─────────────────────────────────────────────────────────────────
#  FYERS ORDER PLACEMENT
# ─────────────────────────────────────────────────────────────────


def _refresh_fyers_token_sync(account) -> bool:
    """
    Token expired hone par programmatic login se fresh access_token lo.
    Returns True agar refresh successful, False otherwise.

    Sirf kaam karta hai jab:
    - account.fyers_client_id set ho
    - account.totp_secret ya account.fyers_pin set ho

    User ne sirf OAuth (manual) login kiya tha → credentials nahi hain → False return.
    Is case mein user ko app se reconnect karna hoga.
    """
    from django.conf import settings as _settings

    fyers_client_id = getattr(account, "fyers_client_id", "") or ""
    totp_secret     = getattr(account, "totp_secret", "") or ""
    fyers_pin       = getattr(account, "fyers_pin", "") or ""

    if not fyers_client_id:
        logger.warning(
            "_refresh_fyers_token_sync: fyers_client_id missing | account=%s — "
            "user needs to reconnect via app (Settings > Broker > Fyers)",
            account.id,
        )
        return False

    if not totp_secret and not fyers_pin:
        logger.warning(
            "_refresh_fyers_token_sync: No auth credentials (totp/pin) | account=%s — "
            "user needs to save TOTP/PIN in app",
            account.id,
        )
        return False

    try:
        from apps.brokers.views import _fyers_programmatic_login
        import hashlib, requests as _requests

        app_id      = account.app_id       or getattr(_settings, "FYERS_APP_ID", "")
        secret_key  = account.secret_key   or getattr(_settings, "FYERS_SECRET_KEY", "")
        redirect_uri = getattr(_settings, "FYERS_REDIRECT_URI", "")

        if not app_id or not secret_key:
            logger.error("_refresh_fyers_token_sync: app_id/secret missing | account=%s", account.id)
            return False

        result = _fyers_programmatic_login(
            app_id=app_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            fyers_client_id=fyers_client_id,
            totp_secret=totp_secret,
            fyers_pin=fyers_pin,
            state=str(account.user_id),
        )

        if not result.get("success"):
            logger.error(
                "_refresh_fyers_token_sync: login failed | account=%s | err=%s",
                account.id, result.get("error"),
            )
            return False

        auth_code   = result["auth_code"]
        FYERS_AUTH  = "https://api-t1.fyers.in/api/v3"
        app_id_hash = hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()

        resp = _requests.post(
            f"{FYERS_AUTH}/validate-authcode",
            json={"grant_type": "authorization_code", "appIdHash": app_id_hash, "code": auth_code},
            timeout=(5, 15),
        )
        data = resp.json()
        new_token = data.get("access_token", "")

        if not new_token:
            logger.error(
                "_refresh_fyers_token_sync: no access_token in response | account=%s | resp=%s",
                account.id, data,
            )
            return False

        # Save new token
        from django.utils import timezone as _tz
        import datetime
        account.access_token  = new_token
        account.is_verified   = True
        account.is_active     = True
        account.token_expiry  = _tz.now() + datetime.timedelta(hours=23)
        account.save(update_fields=["access_token", "is_verified", "is_active", "token_expiry"])

        logger.info(
            "✅ _refresh_fyers_token_sync: token refreshed | account=%s | user=%s",
            account.id, account.user_id,
        )
        return True

    except Exception as e:
        logger.exception(
            "_refresh_fyers_token_sync: exception | account=%s | %s",
            account.id, e,
        )
        return False


def _place_fyers_order(strategy, signal, instrument_type: str, user=None, account=None):
    """
    Fyers pe order place karo.
    instrument_type:
      - 'options' → CALL/PUT option buy karo
      - 'futures' → F&O future contract
      - 'equity'  → equity (cash) segment

    user, account: Global strategy routing ke liye subscriber ka user/account pass karo.
    Normal strategy mein None rahega — strategy.user se fetch hoga.
    """
    from fyers_apiv3 import fyersModel

    from apps.brokers.models import BrokerAccount
    from apps.orders.services import create_order

    # ── Effective user determine karo ─────────────────────────────
    # Global strategy ke liye subscriber ka user pass hota hai.
    # Normal strategy ke liye strategy.user use karo.
    effective_user = user or strategy.user

    # ── ✅ RISK CHECK: RiskManager se pre-trade validation ─────────
    # Yahan check karo PEHLE broker account dhundhe —
    # agar risk limits cross ho gayi hain toh Fyers pe order mat bhejo.
    try:
        from apps.risk.manager import RiskManager
        rm = RiskManager(effective_user)
        _meta = getattr(signal, 'metadata', {}) or {}
        _sl = getattr(signal, 'sl_price', None) or getattr(signal, 'stop_loss', None) or _meta.get('stop_loss') or _meta.get('sl_price')
        _tp = getattr(signal, 'tp_price', None) or getattr(signal, 'take_profit', None) or _meta.get('take_profit') or _meta.get('take_profit_1')
        _sig_type = str(getattr(signal, 'signal_type', 'buy')).lower()
        _side = 'buy' if _sig_type in ('buy', 'long') else 'sell'
        # Options mein risk check ke liye premium use karo, spot price nahi
        _instr = getattr(strategy, 'instrument_type', 'equity')
        _raw_price = Decimal(str(signal.price))
        _check_price = round(_raw_price * Decimal("0.03"), 2) if _instr == 'options' else _raw_price
        allowed, reason = rm.can_place_order(
            symbol=signal.symbol,
            qty=1,
            price=_check_price,
            stop_loss=Decimal(str(_sl)) if _sl else None,
            take_profit=Decimal(str(_tp)) if _tp else None,
            side=_side,
        )
        if not allowed:
            logger.warning(
                "❌ Risk check FAILED — order blocked | user=%s | strategy=%s | "
                "symbol=%s | reason=%s",
                effective_user.pk, strategy.id, signal.symbol, reason,
            )
            # ── Rejected order DB mein save karo ─────────────────
            _save_rejected_broker_order(
                strategy=strategy,
                signal=signal,
                user=effective_user,
                account=account,
                reason=f"Risk check failed: {reason}",
            )
            return None
    except Exception as risk_err:
        # RiskManager fail hone pe order BLOCK karo (fail-safe)
        logger.error(
            "RiskManager check error | user=%s | err=%s — order BLOCKED",
            effective_user.pk, risk_err,
        )
        return None

    # ── Broker credentials fetch karo ─────────────────────────────
    # Agar account pehle se pass kiya gaya (global routing) toh use karo.
    # Warna effective_user ka fyers account dhundho.
    if account is None:
        account = (
            BrokerAccount.objects.filter(
                user=effective_user,
                broker="fyers",
                is_active=True,
                is_verified=True,
            )
            .select_related()
            .first()
        )

    if not account:
        logger.error("Fyers account not connected for user %s", effective_user.pk)
        return None

    # ── ✅ FIX: Token validity check BEFORE order ─────────────────
    # Agar access_token missing/expired hai → auto-refresh try karo
    # (tabhi jab fyers_client_id + totp_secret/pin stored hai)
    if not account.access_token:
        logger.error(
            "Fyers access_token missing | user=%s | account=%s — reconnect needed",
            effective_user.pk, account.id,
        )
        return None

    # Token stored hai — expiry check karo (token_expiry field agar hai)
    token_expiry = getattr(account, "token_expiry", None)
    if token_expiry:
        from django.utils import timezone as tz
        if tz.now() >= token_expiry:
            logger.warning(
                "Fyers token expired | user=%s | account=%s | expiry=%s — "
                "auto-refresh try karo",
                effective_user.pk, account.id, token_expiry,
            )
            # Auto-refresh try karo agar credentials available hain
            _refresh_fyers_token_sync(account)
            # Reload account from DB
            account.refresh_from_db()
            if not account.access_token:
                logger.error(
                    "Token refresh failed | user=%s | account=%s",
                    effective_user.pk, account.id,
                )
                return None

    # Fyers client initialize
    # SDK internally: header = f"{client_id}:{token}"
    # Isliye:
    #   client_id = app_id        (e.g. "RBPEXX0S3A-200")
    #   token     = access_token  (sirf JWT token — app_id prefix NAHI)
    # Dono ko alag pass karo — SDK khud combine karta hai.
    effective_app_id = account.app_id or settings.FYERS_APP_ID
    fyers = fyersModel.FyersModel(
        client_id=effective_app_id,
        token=account.access_token,
        log_path="",
        is_async=False,
    )
    logger.info(
        "Fyers client initialized | user=%s | app_id=%s | account=%s",
        effective_user.pk, effective_app_id, account.id,
    )

    # Risk params
    risk = strategy.risk_config
    # qty Flutter se aata hai — code multiply nahi karega
    qty = int(risk.get("qty", 1))

    if instrument_type == "futures":
        logger.info("Futures order | qty=%d (Flutter se)", qty)
        return _fyers_futures_order(strategy, signal, fyers, account, qty, risk, effective_user=effective_user)
    elif instrument_type == "options":
        # Options: available capital × risk_pct se qty calculate karo
        qty = _calculate_options_qty(strategy, signal, fyers, risk)
        if not qty or qty < 1:
            logger.error("Options qty 0 hua | symbol=%s | capital insufficient", signal.symbol)
            return None
        logger.info("Options order | qty=%d (capital-based)", qty)
        return _fyers_options_order(strategy, signal, fyers, account, qty, risk, effective_user=effective_user)
    elif instrument_type == "equity":
        return _fyers_equity_order(strategy, signal, fyers, account, qty, risk, effective_user=effective_user)
    else:
        logger.error("Unknown instrument_type for Fyers: %s", instrument_type)
        return None


def _calculate_options_qty(strategy, signal, fyers, risk: dict) -> int:
    """
    Capital percentage se options qty calculate karo.

    Logic:
    1. Fyers se available funds lo
    2. risk_config ka capital_pct use karo (default 50%)
    3. Trade capital = available × capital_pct / 100
    4. Option ka LTP fetch karo (ya signal price use karo)
    5. qty = floor(trade_capital / (option_ltp × lot_size))
    6. Minimum 1 lot return karo

    risk_config fields:
        capital_pct  : kitna % capital use karna hai (default 50)
        max_lots     : maximum lots cap (default 10)
    """
    from .fyers_utils import _clean_symbol, STRIKE_STEPS

    LOT_SIZES_OPTIONS = {
        "NIFTY":      65,
        "BANKNIFTY":  30,
        "FINNIFTY":   40,
        "MIDCPNIFTY": 120,
        "SENSEX":     10,
    }

    try:
        # ── 1. Available capital Fyers se lo ─────────────────────
        funds_resp = fyers.funds()
        available = 0.0
        if funds_resp and funds_resp.get("s") == "ok":
            fund_list = funds_resp.get("fund_limit", [])
            # ✅ FIX: Fyers v3 API mein multiple possible field names hain
            # Title variants: "Available Balance", "Avl. Balance", "Net Balance"
            # Amount field variants: "equityAmount", "amount", "balance"
            BALANCE_TITLES = {
                "Available Balance", "Avl. Balance", "Available Margin",
                "Total Balance", "Net Balance", "Avl. Margin",
            }
            for item in fund_list:
                if item.get("title") in BALANCE_TITLES:
                    # Try all possible amount fields
                    amt = (
                        item.get("equityAmount")
                        or item.get("amount")
                        or item.get("balance")
                        or 0
                    )
                    try:
                        available = float(amt)
                    except (TypeError, ValueError):
                        available = 0.0
                    if available > 0:
                        break
            
            # Last resort: sum all positive equityAmount values
            if available <= 0:
                for item in fund_list:
                    try:
                        amt = float(item.get("equityAmount") or item.get("amount") or 0)
                        if amt > 0:
                            available = amt
                            break
                    except (TypeError, ValueError):
                        continue
        
        if available <= 0:
            # ✅ NOTE: Negative balance = account mein funds nahi hain
            # qty=1 fallback — order attempt hoga, broker reject karega agar insufficient funds
            logger.warning(
                "Fyers funds fetch failed ya 0/negative — qty=1 fallback | available=%.2f",
                available,
            )
            return 1

        logger.info("Available capital: ₹%.2f", available)

        # ── 2. Capital percentage ─────────────────────────────────
        capital_pct = float(risk.get("capital_pct", 50))  # default 50%
        max_lots    = int(risk.get("max_lots", 10))
        trade_capital = available * capital_pct / 100
        logger.info("Trade capital: ₹%.2f (%.0f%% of ₹%.2f)", trade_capital, capital_pct, available)

        # ── 3. Symbol clean karo ─────────────────────────────────
        base = _clean_symbol(signal.symbol)
        lot_size = LOT_SIZES_OPTIONS.get(base, 65)

        # ── 4. Option premium estimate ────────────────────────────
        # Signal price = underlying price, option LTP alag hota hai
        # Estimate: ATM option ~ 0.4-0.5% of underlying (rough)
        # Real scenario mein Fyers quote API se milega
        underlying_price = float(signal.price)
        signal_type = getattr(signal, "signal_type", "buy")
        option_type = "CE" if signal_type in ("buy", "long") else "PE"

        # ── Actual premium — NSE option chain se ─────────────────
        option_premium = 0.0
        try:
            from .fyers_utils import get_best_premium_option
            best = get_best_premium_option(
                symbol=base,
                current_price=underlying_price,
                option_type=option_type,
                user=strategy.user,
            )
            if best:
                option_premium = float(best.get("ltp", 0) or best.get("premium", 0))
                logger.info("NSE premium | %s %s | premium=%.1f", base, option_type, option_premium)
        except Exception as e:
            logger.warning("NSE premium fetch failed | %s", e)

        if option_premium <= 0:
            option_premium = max(round(underlying_price * 0.004, 2), 10.0)
            logger.info("Premium fallback | premium~%.1f", option_premium)

        # ── calculate_lots() — wallet + TradingProfile based ─────
        try:
            from .fyers_utils import calculate_lots
            from apps.wallet.models import Wallet
            _w = Wallet.objects.get(user=strategy.user, currency="INR")
            _capital = float(_w.available_balance + _w.locked_balance)
            try:
                _tp = strategy.user.trading_profile
                _risk_pct = float(_tp.risk_per_trade_pct) if _tp.risk_per_trade_pct else 0.10
            except Exception:
                _risk_pct = 0.10
            lots = calculate_lots(base, option_premium, _capital, _risk_pct)
            lots = min(lots, max_lots)
            logger.info(
                "Options qty | wallet=%.0f | risk_pct=%.0f%% | premium=%.1f | lot_size=%d | lots=%d",
                _capital, _risk_pct*100, option_premium, lot_size, lots,
            )
        except Exception as e:
            logger.warning("calculate_lots fallback | %s", e)
            cost_per_lot = option_premium * lot_size
            lots = max(1, min(int(trade_capital / cost_per_lot) if cost_per_lot > 0 else 1, max_lots))
        return lots

    except Exception as e:
        logger.error("_calculate_options_qty error: %s — fallback qty=1", e)
        return 1


def _fyers_options_order(strategy, signal, fyers, account, qty: int, risk: dict, effective_user=None):
    """Fyers options order — CALL (buy) ya PUT (buy)"""
    from apps.orders.services import create_order

    from .fyers_utils import get_atm_option_symbol, LOT_SIZES, _clean_symbol

    symbol = signal.symbol  # e.g. 'NIFTY'
    signal_type = signal.signal_type  # 'buy' ya 'sell'
    current_price = float(signal.price)

    # ✅ FIX: Crypto symbols ke liye Fyers options exist nahi karte
    # ETH-USDT, BTC-USDT → NSE:ETH-USDT26MAY212150PE jaisa galat symbol banta tha
    from .fyers_utils import _clean_symbol as _cs
    _base_check = _cs(symbol)
    _CRYPTO_CHECK = {"BTC-USDT", "ETH-USDT", "BNB-USDT", "XRP-USDT", "SOL-USDT", "BTCUSD", "ETHUSD"}
    if _base_check in _CRYPTO_CHECK or "-USDT" in _base_check or "-USD" in _base_check:
        logger.warning(
            "Crypto symbol ke liye Fyers options nahi hote — order skip | symbol=%s", symbol
        )
        return None

    _order_user = effective_user or strategy.user
    min_prem = float(strategy.parameters.get('min_premium', 80))
    max_prem = float(strategy.parameters.get('max_premium', 400))

    # Directional — best premium
    option_type = "CE" if signal_type in ("buy", "long") else "PE"
    from .fyers_utils import get_best_premium_option
    best = get_best_premium_option(symbol, current_price, option_type, user=_order_user,
                                   min_premium=min_prem, max_premium=max_prem)
    if best:
        option_symbol = best['symbol']
        logger.info("Best premium | %s | strike=%d | ltp=%.1f",
                    option_symbol, best['strike'], best['ltp'])
    else:
        option_symbol = get_atm_option_symbol(symbol, current_price, option_type, user=_order_user)
    if not option_symbol:
        logger.error("Option symbol nahi mila | symbol=%s | price=%s", symbol, current_price)
        return None

    # ── FIX: qty = lots (Flutter se / capital-based), actual_qty = lots × lot_size ──
    base = _clean_symbol(symbol)
    lot_size = LOT_SIZES.get(base, 1)
    actual_qty = qty * lot_size
    logger.info(
        "Options qty | lots=%d | lot_size=%d | actual_qty=%d | symbol=%s",
        qty, lot_size, actual_qty, base,
    )

    # SL/target calculate karo
    sl_pct = float(risk.get("sl_pct", 0.5))
    target_pct = float(risk.get("target_pct", 1.0))
    sl_price = round(current_price * (1 - sl_pct / 100), 2)
    tgt_price = round(current_price * (1 + target_pct / 100), 2)

    logger.info(
        "Fyers OPTIONS order | symbol=%s | type=%s | lots=%d | qty=%d | price=%s | sl=%s | tgt=%s",
        option_symbol,
        option_type,
        qty,
        actual_qty,
        current_price,
        sl_price,
        tgt_price,
    )

    # Fyers order data
    order_data = {
        "symbol": option_symbol,
        "qty": actual_qty,
        "type": 2,  # Fyers v3: 2=Market order (1=Limit, 2=Market, 3=SL, 4=SL-M)
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
            quantity=actual_qty,
            price=Decimal(str(current_price)),
            sl_price=Decimal(str(sl_price)),
            target_price=Decimal(str(tgt_price)),
            instrument_type="options",
            broker=account,
            exchange_order_id=exchange_order_id,
            mode="live",  # FIX: broker fn = always live
        )
        logger.info(
            "Fyers options order placed | lots=%d | qty=%d | order_id=%s | exchange_id=%s",
            qty, actual_qty, order.id if order else None, exchange_order_id,
        )
        # ── GTT SL/Target order place karo ──────────────────────
        if order and exchange_order_id:
            try:
                _place_fyers_gtt(
                    fyers=fyers,
                    option_symbol=option_symbol,
                    actual_qty=actual_qty,
                    sl_price=sl_price,
                    tgt_price=tgt_price,
                    option_type=option_type,
                )
            except Exception as gtt_err:
                logger.warning("GTT set failed (entry OK) | err=%s", gtt_err)
        return order
    else:
        reject_reason = resp.get("message", "Unknown error")
        logger.error("Fyers options order FAILED | resp=%s", resp)
        _ws_notify_failure(effective_user or strategy.user, strategy.algo_name, reject_reason)
        # ── ✅ FIX: Rejected order DB mein save karo ──────────────
        _save_rejected_broker_order(
            strategy=strategy,
            signal=signal,
            user=effective_user or strategy.user,
            account=account,
            reason=f"Broker rejected: {reject_reason}",
            broker_response=resp,
        )
        return None



def _place_fyers_gtt(fyers, option_symbol: str, actual_qty: int, sl_price: float, tgt_price: float, option_type: str):
    """Entry ke baad Fyers GTT OCO order place karo — SL + Target dono."""
    # OCO: leg1 = target (above LTP), leg2 = SL (below LTP)
    gtt_data = {
        "symbol": option_symbol,
        "side": -1,  # sell (exit)
        "productType": "INTRADAY",
        "orderInfo": {
            "leg1": {
                "price": round(tgt_price, 2),
                "triggerPrice": round(tgt_price, 2),
                "qty": actual_qty,
            },
            "leg2": {
                "price": round(sl_price, 2),
                "triggerPrice": round(sl_price, 2),
                "qty": actual_qty,
            },
        },
    }
    resp = fyers.place_gtt_order(data=gtt_data)
    logger.info("GTT OCO placed | symbol=%s | sl=%.2f | tgt=%.2f | resp=%s", option_symbol, sl_price, tgt_price, resp)
    return resp

def _fyers_futures_order(strategy, signal, fyers, account, qty: int, risk: dict, effective_user=None):
    """Fyers F&O futures order"""
    from apps.orders.services import create_order

    from .fyers_utils import get_current_futures_symbol, LOT_SIZES, _clean_symbol

    symbol = signal.symbol
    signal_type = signal.signal_type
    current_price = float(signal.price)

    # Current month futures symbol
    futures_symbol = get_current_futures_symbol(symbol)
    if not futures_symbol:
        logger.error("Futures symbol nahi mila | symbol=%s", symbol)
        return None

    # ── FIX: qty = lots (Flutter se), actual_qty = lots × lot_size ──
    base = _clean_symbol(symbol)
    lot_size = LOT_SIZES.get(base, 1)
    actual_qty = qty * lot_size
    logger.info(
        "Futures qty | lots=%d | lot_size=%d | actual_qty=%d | symbol=%s",
        qty, lot_size, actual_qty, base,
    )

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
        "qty": actual_qty,
        "type": 2,  # Market
        "side": side,
        "productType": "MARGIN",  # Fyers F&O futures ke liye MARGIN chahiye, INTRADAY nahi
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
            quantity=actual_qty,
            price=Decimal(str(current_price)),
            sl_price=Decimal(str(sl_price)),
            target_price=Decimal(str(tgt_price)),
            instrument_type="futures",
            broker=strategy.broker,
            exchange_order_id=exchange_order_id,
            mode="live",  # FIX: broker fn = always live
        )
        logger.info(
            "Fyers futures order placed | lots=%d | qty=%d | order_id=%s",
            qty, actual_qty, order.id if order else None,
        )
        return order
    else:
        reject_reason = resp.get("message", "Unknown error")
        logger.error("Fyers futures order FAILED | resp=%s", resp)
        _ws_notify_failure(effective_user or strategy.user, strategy.algo_name, reject_reason)
        _save_rejected_broker_order(
            strategy=strategy,
            signal=signal,
            user=effective_user or strategy.user,
            account=account,
            reason=f"Broker rejected: {reject_reason}",
            broker_response=resp,
        )
        return None


def _fyers_equity_order(strategy, signal, fyers, account, qty: int, risk: dict, effective_user=None):
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
            mode="live",  # FIX: broker fn = always live
        )
        return order
    else:
        reject_reason = resp.get("message", "Unknown error")
        logger.error("Fyers equity order FAILED | resp=%s", resp)
        _ws_notify_failure(effective_user or strategy.user, strategy.algo_name, reject_reason)
        _save_rejected_broker_order(
            strategy=strategy,
            signal=signal,
            user=effective_user or strategy.user,
            account=account,
            reason=f"Broker rejected: {reject_reason}",
            broker_response=resp,
        )
        return None


# ─────────────────────────────────────────────────────────────────
#  DELTA EXCHANGE ORDER PLACEMENT
# ─────────────────────────────────────────────────────────────────


def _place_delta_order(strategy, signal, instrument_type: str, user=None, account=None):
    """
    Delta Exchange pe order place karo.
    instrument_type:
      - 'futures' → futures contract
      - 'perp'    → perpetual contract (most common)

    user, account: Global strategy routing ke liye subscriber ka user/account pass karo.
    """
    from apps.brokers.models import BrokerAccount
    from apps.orders.services import create_order

    # ── Effective user determine karo ─────────────────────────────
    effective_user = user or strategy.user

    # ── Broker account fetch (ya use passed account) ───────────────
    if account is None:
        account = BrokerAccount.objects.filter(
            user=effective_user,
            broker="delta",
            is_active=True,
            is_verified=True,
        ).first()

    if not account:
        logger.error("Delta account not connected for user %s", effective_user.pk)
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
    product_type = "perp" if instrument_type == "perp" else "futures"

    try:
        # Delta API call
        delta_resp = _call_delta_api(
            account=account,
            symbol=symbol,
            side=side,
            qty=qty,
            product_type=product_type,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tgt_price,
        )

        if delta_resp.get("success"):
            exchange_order_id = str(delta_resp.get("result", {}).get("id", ""))
            # ✅ FIX: Proxy object nahi chalega — real Strategy instance chahiye
            _real_strategy = getattr(strategy, '_real', strategy)
            order = create_order(
                strategy=_real_strategy,
                symbol=symbol,
                side=signal_type,
                quantity=qty,
                price=Decimal(str(current_price)),
                sl_price=Decimal(str(sl_price)),
                target_price=Decimal(str(tgt_price)),
                instrument_type=instrument_type,
                broker=strategy.broker,
                exchange_order_id=exchange_order_id,
                mode="live",  # FIX: broker fn = always live
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

def _delta_sign_and_post(api_key, api_secret, path, payload):
    """Delta India API — sign karke POST karo."""
    import hashlib, hmac, json, time
    import requests

    method    = "POST"
    timestamp = str(int(time.time()))
    body_str  = json.dumps(payload)
    sig_data  = method + timestamp + path + body_str

    signature = hmac.new(
        key=api_secret.encode(),
        msg=sig_data.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()

    headers = {
        "api-key":      api_key,
        "timestamp":    timestamp,
        "signature":    signature,
        "Content-Type": "application/json",
        "User-Agent":   "python-rest-client",
    }
    resp = requests.post(
        "https://api.india.delta.exchange" + path,
        data=body_str,
        headers=headers,
        timeout=10,
    )
    return resp.json()


def _call_delta_api(account, symbol, side, qty, product_type, current_price,
                    sl_price=None, tp_price=None):
    """
    Delta India pe order place karo.
    ✅ SL/TP bracket orders bhi bhejo agar sl_price/tp_price pass kiye hain.
    """
    api_key    = account.api_key
    api_secret = account.api_secret
    product_id = _get_delta_product_id(_to_delta_symbol(symbol, product_type))

    # ── Step 1: Main market order ─────────────────────────────────
    payload = {
        "product_id": product_id,
        "size":        qty,
        "side":        side,
        "order_type":  "market_order",
        "time_in_force": "gtc",
    }
    resp = _delta_sign_and_post(api_key, api_secret, "/v2/orders", payload)
    logger.info("Delta market order resp | symbol=%s | side=%s | qty=%d | resp=%s",
                symbol, side, qty, resp.get("success"))

    if not resp.get("success"):
        return resp

    # ── Step 2: SL order ──────────────────────────────────────────
    if sl_price:
        sl_side = "sell" if side == "buy" else "buy"
        sl_payload = {
            "product_id":    product_id,
            "size":           qty,
            "side":           sl_side,
            "order_type":     "limit_order",
            "limit_price":    str(round(sl_price, 2)),
            "stop_price":     str(round(sl_price, 2)),
            "time_in_force":  "gtc",
            "reduce_only":    True,
            "close_on_trigger": True,
        }
        import time as _time
        _time.sleep(1.5)  # Position fill hone ka wait karo
        sl_resp = _delta_sign_and_post(api_key, api_secret, "/v2/orders", sl_payload)
        logger.info("Delta SL order | symbol=%s | sl_price=%s | success=%s",
                    symbol, sl_price, sl_resp.get("success"))
        if not sl_resp.get("success"):
            logger.warning("Delta SL order FAILED | resp=%s", sl_resp)

    # ── Step 3: TP order ──────────────────────────────────────────
    if tp_price:
        tp_side = "sell" if side == "buy" else "buy"
        tp_payload = {
            "product_id":   product_id,
            "size":          qty,
            "side":          tp_side,
            "order_type":    "limit_order",
            "limit_price":   str(round(tp_price, 2)),
            "time_in_force": "gtc",
        }
        tp_resp = _delta_sign_and_post(api_key, api_secret, "/v2/orders", tp_payload)
        logger.info("Delta TP order | symbol=%s | tp_price=%s | success=%s",
                    symbol, tp_price, tp_resp.get("success"))
        if not tp_resp.get("success"):
            logger.warning("Delta TP order FAILED | resp=%s", tp_resp)

    return resp



# ─────────────────────────────────────────────────────────────────
#  DHAN ORDER PLACEMENT
#  Dhan broker pe options/futures/equity orders
# ─────────────────────────────────────────────────────────────────

def _place_dhan_order(strategy, signal, instrument_type: str, user=None, account=None):
    """
    Dhan pe order place karo.
    instrument_type: options / futures / equity

    ✅ DhanAdapter.place_order() use karta hai — same interface as Fyers.
    Dhan ka securityId + exchange_segment DhanSymbolMapper se auto-resolve hota hai.
    """
    from apps.brokers.models import BrokerAccount
    from apps.orders.services import create_order
    from broker_adapters.dhan.adapter import DhanAdapter
    from broker_adapters.dhan.symbol_mapper import get_lot_size

    effective_user = user or strategy.user

    # ── Risk check ─────────────────────────────────────────────────
    try:
        from apps.risk.manager import RiskManager
        rm = RiskManager(effective_user)
        _cp = float(signal.price)
        _sl_pct = float(strategy.risk_config.get("sl_pct", 20))
        _sl = round(_cp * (1 - _sl_pct / 100), 2)
        allowed, reason = rm.can_place_order(
            symbol=signal.symbol,
            qty=1,
            price=_cp,
            stop_loss=Decimal(str(_sl)),
        )
        if not allowed:
            logger.warning(
                "❌ Risk check FAILED (Dhan) | user=%s | symbol=%s | reason=%s",
                effective_user.pk, signal.symbol, reason,
            )
            _save_rejected_broker_order(
                strategy=strategy, signal=signal,
                user=effective_user, account=account,
                reason=f"Risk check failed: {reason}",
            )
            return None
    except Exception as risk_err:
        logger.error("RiskManager error (Dhan) | user=%s | %s — BLOCKED", effective_user.pk, risk_err)
        return None

    # ── Account ────────────────────────────────────────────────────
    if account is None:
        account = BrokerAccount.objects.filter(
            user=effective_user, broker="dhan",
            is_active=True, is_verified=True,
        ).exclude(dhan_access_token="").first()

    if not account:
        logger.error("Dhan account not connected | user=%s", effective_user.pk)
        return None

    adapter = DhanAdapter({
        "dhan_client_id":    account.dhan_client_id,
        "dhan_access_token": account.dhan_access_token,
    })

    risk          = strategy.risk_config
    signal_type   = signal.signal_type
    current_price = float(signal.price)
    sl_pct        = float(risk.get("sl_pct", 0.5))
    target_pct    = float(risk.get("target_pct", 1.0))

    # ── Symbol + qty ───────────────────────────────────────────────
    symbol = signal.symbol  # e.g. "NIFTY", "NIFTY26JUN2524500CE"

    if instrument_type == "options":
        # Options symbol (already resolved by signal, or build ATM)
        if symbol.endswith("CE") or symbol.endswith("PE"):
            dhan_symbol = symbol
        else:
            # Build ATM option symbol (same logic as Fyers)
            from apps.strategies.fyers_utils import get_atm_option_symbol
            opt_type = "CE" if signal_type in ("buy", "long") else "PE"
            dhan_symbol = get_atm_option_symbol(symbol, current_price, opt_type, user=effective_user)
            if not dhan_symbol:
                logger.error("Dhan: ATM option symbol nahi mila | symbol=%s", symbol)
                return None

        lot_size   = get_lot_size(dhan_symbol)
        qty_lots   = int(risk.get("qty", 1))
        actual_qty = qty_lots * lot_size
        product_type = "MARGIN"

    elif instrument_type == "futures":
        # Futures symbol
        from apps.strategies.fyers_utils import get_current_futures_symbol
        fyers_fut = get_current_futures_symbol(symbol)
        # Convert Fyers format → Dhan format (strip NSE: prefix)
        dhan_symbol = fyers_fut.replace("NSE:", "").replace("BSE:", "") if fyers_fut else symbol
        lot_size    = get_lot_size(dhan_symbol)
        qty_lots    = int(risk.get("qty", 1))
        actual_qty  = qty_lots * lot_size
        product_type = "MARGIN"

    else:  # equity
        dhan_symbol = f"NSE:{symbol}-EQ" if ":" not in symbol else symbol
        actual_qty   = int(risk.get("qty", 1))
        product_type = "CNC"

    # ── SL / TP ────────────────────────────────────────────────────
    if signal_type == "buy":
        sl_price  = round(current_price * (1 - sl_pct / 100), 2)
        tgt_price = round(current_price * (1 + target_pct / 100), 2)
    else:
        sl_price  = round(current_price * (1 + sl_pct / 100), 2)
        tgt_price = round(current_price * (1 - target_pct / 100), 2)

    logger.info(
        "Dhan order | user=%s | %s %s | qty=%d | price=%.2f | sl=%.2f | tgt=%.2f",
        effective_user.pk, signal_type.upper(), dhan_symbol,
        actual_qty, current_price, sl_price, tgt_price,
    )

    # ── Place order ────────────────────────────────────────────────
    result = adapter.place_order(
        symbol=dhan_symbol,
        side=signal_type.lower(),
        qty=actual_qty,
        order_type="market",
        product_type=product_type,
    )

    if result.success:
        order = create_order(
            strategy=strategy,
            symbol=dhan_symbol,
            side=signal_type,
            quantity=actual_qty,
            price=Decimal(str(current_price)),
            sl_price=Decimal(str(sl_price)),
            target_price=Decimal(str(tgt_price)),
            instrument_type=instrument_type,
            broker=account,
            exchange_order_id=result.order_id or "",
            broker_response=result.raw,
            mode="live",  # FIX: broker fn = always live
        )
        logger.info(
            "✅ Dhan order placed | user=%s | order_id=%s | exchange_id=%s",
            effective_user.pk, order.id if order else None, result.order_id,
        )
        return order
    else:
        logger.error("❌ Dhan order FAILED | user=%s | reason=%s", effective_user.pk, result.message)
        _ws_notify_failure(effective_user, strategy.algo_name, result.message)
        _save_rejected_broker_order(
            strategy=strategy, signal=signal,
            user=effective_user, account=account,
            reason=f"Dhan rejected: {result.message}",
            broker_response=result.raw if hasattr(result, "raw") else {},
        )
        return None


def _get_delta_product_id(delta_symbol: str) -> int:
    """
    Delta India product IDs — perpetual contracts.

    FIX: Pehle API se live lookup karo — hardcoded IDs stale ho sakte hain.
    Fallback: known IDs use karo agar API fail ho.

    Verify/update: https://api.india.delta.exchange/v2/products
    """
    # Step 1: Live API se try karo
    try:
        import requests as _req
        r = _req.get("https://api.india.delta.exchange/v2/products", timeout=5)
        if r.status_code == 200:
            for product in r.json().get("result", []):
                if (
                    product.get("symbol") == delta_symbol
                    and product.get("contract_type") in ("perpetual_futures", "futures")
                ):
                    logger.info("Delta product ID (live) | symbol=%s | id=%s", delta_symbol, product["id"])
                    return int(product["id"])
    except Exception as _lookup_err:
        logger.warning("Delta product ID live lookup failed: %s — using fallback", _lookup_err)

    # Step 2: Known fallback IDs
    # ✅ VERIFIED 2026-05-29 from https://api.india.delta.exchange/v2/products
    product_ids = {
        "BTCUSD": 27,    # BTC Perpetual — verified ✅
        "ETHUSD": 3136,  # ETH Perpetual — FIX: was 3 (wrong), now 3136 (verified from API)
        "SOLUSD": 14823, # SOL Perpetual — verified ✅
        "BNBUSD": 15042, # BNB Perpetual — verified ✅
        "XRPUSD": 14969, # XRP Perpetual — verified ✅
    }
    product_id = product_ids.get(delta_symbol)
    if product_id is None:
        raise ValueError(
            f"Delta product ID not found: {delta_symbol}. "
            f"Check https://api.india.delta.exchange/v2/products"
        )
    logger.info("Delta product ID (fallback) | symbol=%s | id=%d", delta_symbol, product_id)
    return product_id

def _to_delta_symbol(symbol: str, product_type: str) -> str:
    """
    BTCUSDT / BTC-USDT dono formats → Delta ke format mein convert karo.
    ✅ FIX: ETH-USDT, BTC-USDT (hyphen wale) bhi handle karo.
    """
    # Normalize: BTC-USDT → BTCUSDT (hyphen remove karo)
    normalized = symbol.replace("-", "")

    mapping = {
        "BTCUSDT": {"perp": "BTCUSD", "futures": "BTCUSD"},
        "ETHUSDT": {"perp": "ETHUSD", "futures": "ETHUSD"},
        "SOLUSDT": {"perp": "SOLUSD", "futures": "SOLUSD"},
        "BNBUSDT": {"perp": "BNBUSD", "futures": "BNBUSD"},
        "XRPUSDT": {"perp": "XRPUSD", "futures": "XRPUSD"},
    }
    result = mapping.get(normalized, {}).get(product_type)
    if result:
        return result
    # Fallback: USDT ya USDT suffix hata ke USD lagao
    return normalized.replace("USDT", "USD")




# ─────────────────────────────────────────────────────────────────
#  REJECTED / FAILED ORDER — DB mein save karo
# ─────────────────────────────────────────────────────────────────

def _save_rejected_broker_order(strategy, signal, user, account, reason: str, broker_response: dict = None):
    """
    Jab bhi order reject ho — chahe broker ne kiya ya risk check ne block kiya —
    BrokerOrder DB mein REJECTED status ke saath save karo.

    Isse:
    - Admin dashboard pe saare rejected orders dikhte hain
    - Fyers orders count == DB orders count (screenshot issue fix)
    - Audit trail bana rehta hai

    Called from:
    - _place_fyers_order()  → risk check fail
    - _fyers_options_order() → fyers.place_order() failure
    - _fyers_futures_order() → fyers.place_order() failure
    - _fyers_equity_order()  → fyers.place_order() failure
    """
    try:
        from apps.orders.services import create_order
        from apps.brokers.models import BrokerOrder

        # ── DB mein Order record banao (rejected state mein) ──────
        order = create_order(
            strategy=strategy,
            symbol=signal.symbol,
            side=signal.signal_type,
            quantity=1,  # actual qty unknown at reject time
            price=Decimal(str(float(signal.price))),
            instrument_type=getattr(strategy, "instrument_type", "options"),
            broker=account,
            mode=getattr(strategy, "mode", "live"),
        )

        if order and account:
            # ── BrokerOrder REJECTED status se create karo ────────
            BrokerOrder.objects.create(
                broker_account   = account,
                order            = order,
                order_type       = BrokerOrder.OrderType.ENTRY,
                status           = BrokerOrder.Status.REJECTED,
                symbol           = signal.symbol,
                side             = signal.signal_type.lower(),  # ✅ 'buy'/'sell' lowercase
                quantity         = 1,                           # ✅ IntegerField
                rejection_reason = reason[:500],
                broker_response  = broker_response or {"rejection_reason": reason},
                exchange_order_id = "",
                notes            = f"auto-rejected | {reason[:200]}",
            )
            logger.info(
                "💾 Rejected order saved to DB | user=%s | symbol=%s | reason=%s",
                user.pk if user else "?", signal.symbol, reason,
            )
    except Exception as e:
        # DB save fail hone pe silently log karo — order block already ho gaya
        logger.error(
            "Failed to save rejected order to DB | user=%s | err=%s",
            user.pk if user else "?", e,
        )


# ─────────────────────────────────────────────────────────────────
#  ZERODHA ORDER PLACEMENT (KiteConnect)
# ─────────────────────────────────────────────────────────────────

def _place_zerodha_order(strategy, signal, instrument_type: str, user=None, account=None):
    """
    Zerodha KiteConnect pe order place karo.
    instrument_type: options / futures / equity

    ✅ ZerodhaAdapter.place_order() use karta hai.
    Zerodha ka symbol format: "NIFTY24JUN24500CE" (no exchange prefix)
    Exchange: NFO for options/futures, NSE for equity.
    """
    from apps.brokers.models import BrokerAccount
    from apps.orders.services import create_order
    from broker_adapters.zerodha.adapter import ZerodhaAdapter
    from apps.strategies.fyers_utils import get_lot_size as _get_lot_size

    effective_user = user or strategy.user

    # ── Risk check ─────────────────────────────────────────────────
    try:
        from apps.risk.manager import RiskManager
        rm = RiskManager(effective_user)
        allowed, reason = rm.can_place_order(
            symbol=signal.symbol, qty=1, price=float(signal.price),
        )
        if not allowed:
            logger.warning(
                "❌ Risk check FAILED (Zerodha) | user=%s | symbol=%s | reason=%s",
                effective_user.pk, signal.symbol, reason,
            )
            _save_rejected_broker_order(
                strategy=strategy, signal=signal,
                user=effective_user, account=account,
                reason=f"Risk check failed: {reason}",
            )
            return None
    except Exception as risk_err:
        logger.error("RiskManager error (Zerodha) | user=%s | %s — BLOCKED", effective_user.pk, risk_err)
        return None

    # ── Account ────────────────────────────────────────────────────
    if account is None:
        account = BrokerAccount.objects.filter(
            user=effective_user, broker="zerodha",
            is_active=True, is_verified=True,
        ).exclude(access_token="").first()

    if not account:
        logger.error("Zerodha account not connected | user=%s", effective_user.pk)
        return None

    adapter = ZerodhaAdapter({
        "api_key":      account.api_key,
        "access_token": account.access_token,
    })

    risk          = strategy.risk_config
    signal_type   = signal.signal_type
    current_price = float(signal.price)
    sl_pct        = float(risk.get("sl_pct", 0.5))
    target_pct    = float(risk.get("target_pct", 1.0))
    symbol        = signal.symbol

    # ── Symbol + qty + exchange ────────────────────────────────────
    if instrument_type == "options":
        from apps.strategies.fyers_utils import get_atm_option_symbol
        opt_type = "CE" if signal_type in ("buy", "long") else "PE"

        if symbol.endswith("CE") or symbol.endswith("PE"):
            zerodha_symbol = symbol  # Already an option symbol
        else:
            fyers_sym = get_atm_option_symbol(symbol, current_price, opt_type, user=effective_user)
            # Fyers format: NSE:NIFTY26JUN2524500CE → Zerodha: NIFTY26JUN2524500CE (NFO)
            zerodha_symbol = fyers_sym.replace("NSE:", "").replace("BSE:", "") if fyers_sym else None

        if not zerodha_symbol:
            logger.error("Zerodha: option symbol nahi mila | symbol=%s", symbol)
            return None

        lot_size   = _get_lot_size(symbol) if hasattr(_get_lot_size, "__call__") else 1
        qty_lots   = int(risk.get("qty", 1))
        actual_qty = qty_lots * lot_size
        exchange   = "NFO"
        product    = "MIS"  # Intraday

    elif instrument_type == "futures":
        from apps.strategies.fyers_utils import get_current_futures_symbol
        fyers_fut = get_current_futures_symbol(symbol) or ""
        zerodha_symbol = fyers_fut.replace("NSE:", "").replace("BSE:", "")
        lot_size   = _get_lot_size(symbol) if hasattr(_get_lot_size, "__call__") else 1
        qty_lots   = int(risk.get("qty", 1))
        actual_qty = qty_lots * lot_size
        exchange   = "NFO"
        product    = "MIS"

    else:  # equity
        zerodha_symbol = symbol.replace("NSE:", "").replace("-EQ", "")
        actual_qty     = int(risk.get("qty", 1))
        exchange       = "NSE"
        product        = "CNC"

    # ── SL / TP ────────────────────────────────────────────────────
    if signal_type == "buy":
        sl_price  = round(current_price * (1 - sl_pct / 100), 2)
        tgt_price = round(current_price * (1 + target_pct / 100), 2)
    else:
        sl_price  = round(current_price * (1 + sl_pct / 100), 2)
        tgt_price = round(current_price * (1 - target_pct / 100), 2)

    logger.info(
        "Zerodha order | user=%s | %s %s | qty=%d | exchange=%s | price=%.2f",
        effective_user.pk, signal_type.upper(), zerodha_symbol,
        actual_qty, exchange, current_price,
    )

    # ── Place order ────────────────────────────────────────────────
    result = adapter.place_order(
        symbol=zerodha_symbol,
        side=signal_type.lower(),
        qty=actual_qty,
        order_type="market",
        exchange=exchange,
        product=product,
    )

    if result.success:
        order = create_order(
            strategy=strategy,
            symbol=zerodha_symbol,
            side=signal_type,
            quantity=actual_qty,
            price=Decimal(str(current_price)),
            sl_price=Decimal(str(sl_price)),
            target_price=Decimal(str(tgt_price)),
            instrument_type=instrument_type,
            broker=account,
            exchange_order_id=result.order_id or "",
            mode="live",  # FIX: broker fn = always live
        )
        logger.info(
            "✅ Zerodha order placed | user=%s | order_id=%s | exchange_id=%s",
            effective_user.pk, order.id if order else None, result.order_id,
        )
        return order
    else:
        logger.error("❌ Zerodha order FAILED | user=%s | reason=%s", effective_user.pk, result.message)
        _ws_notify_failure(effective_user, strategy.algo_name, result.message)
        _save_rejected_broker_order(
            strategy=strategy, signal=signal,
            user=effective_user, account=account,
            reason=f"Zerodha rejected: {result.message}",
        )
        return None


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
        "FINNIFTY": 40,
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