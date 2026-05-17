# apps/websocket/routing.py

from django.urls import path

from .consumers import MarketConsumer, TradeConsumer

websocket_urlpatterns = [
    path("ws/market/", MarketConsumer.as_asgi()),
    path("ws/trades/", TradeConsumer.as_asgi()),
]
