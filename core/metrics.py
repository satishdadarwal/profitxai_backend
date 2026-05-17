# core/metrics.py (NEW FILE)

from prometheus_client import Counter, Histogram, Gauge
import time
from functools import wraps

# ── Tick Metrics ──
tick_received_total = Counter(
    'tick_received_total',
    'Total ticks received from broker',
    ['broker', 'symbol']
)

tick_processing_duration = Histogram(
    'tick_processing_duration_seconds',
    'Time to process tick end-to-end',
    ['broker']
)

tick_to_user_latency = Histogram(
    'tick_to_user_latency_ms',
    'Latency from broker to user WebSocket',
    ['broker']
)

# ── Order Metrics ──
order_placed_total = Counter(
    'order_placed_total',
    'Total orders placed',
    ['broker', 'status']  # status: success|failed
)

order_execution_duration = Histogram(
    'order_execution_duration_ms',
    'Order execution time',
    ['broker']
)

order_retry_total = Counter(
    'order_retry_total',
    'Order retry attempts',
    ['broker']
)

# ── Strategy Metrics ──
strategy_execution_duration = Histogram(
    'strategy_execution_duration_ms',
    'Strategy execution time',
    ['strategy_name']
)

signal_generated_total = Counter(
    'signal_generated_total',
    'Signals generated',
    ['strategy', 'signal_type']
)

# ── Risk Metrics ──
risk_check_failed_total = Counter(
    'risk_check_failed_total',
    'Risk checks failed',
    ['reason']
)

kill_switch_triggered_total = Counter(
    'kill_switch_triggered_total',
    'Kill switch activations',
    ['reason']
)

# ── System Metrics ──
active_users = Gauge(
    'active_users',
    'Currently connected users'
)

active_strategies = Gauge(
    'active_strategies',
    'Currently running strategies'
)

redis_pubsub_lag = Gauge(
    'redis_pubsub_lag_ms',
    'Redis Pub/Sub message lag'
)

# ── Helper Decorators ──
def track_latency(metric: Histogram):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.time()
            result = await func(*args, **kwargs)
            duration = (time.time() - start) * 1000  # ms
            metric.observe(duration)
            return result
        return wrapper
    return decorator

# Usage:
@track_latency(order_execution_duration)
async def execute_order(*args, **kwargs):
    pass