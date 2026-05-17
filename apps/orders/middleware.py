# apps/orders/middleware.py (NEW FILE)

from django.core.cache import cache
from django.http import JsonResponse
from functools import wraps

def rate_limit(key_prefix: str, max_requests: int, window_seconds: int):
    """
    Rate limit decorator
    
    Example:
    @rate_limit('order_placement', max_requests=10, window_seconds=60)
    def place_order_view(request):
        ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            user_id = request.user.id
            cache_key = f"{key_prefix}:{user_id}"
            
            # Get current count
            count = cache.get(cache_key, 0)
            
            if count >= max_requests:
                return JsonResponse({
                    'error': f'Rate limit exceeded. Max {max_requests} requests per {window_seconds}s'
                }, status=429)
            
            # Increment count
            cache.set(cache_key, count + 1, timeout=window_seconds)
            
            return func(request, *args, **kwargs)
        
        return wrapper
    return decorator

# Usage in views:
from apps.orders.middleware import rate_limit

@rate_limit('order_placement', max_requests=20, window_seconds=60)
def place_order_api(request):
    ...