# apps/market/tick_ingestion_service.py


import asyncio
import json
import logging
from typing import Dict, Set, Optional, List
from datetime import datetime, timedelta
from collections import deque
from dataclasses import dataclass, asdict
import redis.asyncio as aioredis
from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class TickData:
    """Normalized tick data structure"""
    symbol: str
    ltp: float
    bid: float
    ask: float
    volume: int
    timestamp: str
    broker: str
    
    # Optional fields
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    change: Optional[float] = None
    change_pct: Optional[float] = None


class TickIngestionService:
    """
    Centralized tick ingestion for a single broker.
    Maintains ONE WebSocket connection and distributes via Redis Pub/Sub.
    """
    
    def __init__(self, broker: str):
        self.broker = broker
        self.redis: Optional[aioredis.Redis] = None
        self.ws_client = None
        self.is_running = False
        self.subscribed_symbols: Set[str] = set()
        
        # Tick buffer for replay (last 1000 ticks or 5 seconds)
        self.tick_buffer: deque[TickData] = deque(maxlen=1000)
        
        # Heartbeat monitoring
        self.last_tick_time: Optional[datetime] = None
        self.heartbeat_interval = 30  # seconds
        self.max_silence_duration = 60  # reconnect if no tick for 60s
        
        # Metrics
        self.total_ticks_received = 0
        self.total_ticks_published = 0
        self.reconnect_count = 0
        
        # Reconnection settings
        self.reconnect_delay = 5  # seconds
        self.max_reconnect_delay = 60  # seconds
        self.current_reconnect_delay = self.reconnect_delay
        
    async def start(self):
        """Start the ingestion service"""
        self.is_running = True
        logger.info(f"🚀 Starting tick ingestion for {self.broker}")
        
        # Initialize Redis connection
        self.redis = await aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True
        )
        
        # Start main loop
        while self.is_running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                logger.info(f"Ingestion service cancelled for {self.broker}")
                break
            except Exception as e:
                logger.error(f"Ingestion error for {self.broker}: {e}", exc_info=True)
                await self._handle_reconnect()
    
    async def stop(self):
        """Stop the ingestion service"""
        self.is_running = False
        
        if self.ws_client:
            try:
                await self.ws_client.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting WebSocket: {e}")
        
        if self.redis:
            await self.redis.close()
        
        logger.info(f"✅ Stopped ingestion service for {self.broker}")
    
    async def _connect_and_stream(self):
        """Connect to broker WebSocket and start streaming"""
        logger.info(f"Connecting to {self.broker} WebSocket...")
        
        # Import broker-specific WebSocket client
        if self.broker == 'fyers':
            from apps.market.broker_feeds.fyers_feed import FyersWebSocketClient
            self.ws_client = FyersWebSocketClient()
        elif self.broker == 'delta':
            from apps.market.broker_feeds.delta_feed import DeltaWebSocketClient
            self.ws_client = DeltaWebSocketClient()
        else:
            raise ValueError(f"Unknown broker: {self.broker}")
        
        # Connect
        await self.ws_client.connect()
        logger.info(f"✅ Connected to {self.broker} WebSocket")
        
        # Reset reconnect delay on successful connection
        self.current_reconnect_delay = self.reconnect_delay
        
        # Subscribe to all symbols
        for symbol in self.subscribed_symbols:
            await self.ws_client.subscribe(symbol)
            logger.debug(f"Resubscribed to {symbol}")
        
        # Publish connection status
        await self._publish_status('connected')
        
        # Start receiving ticks
        try:
            async for tick in self.ws_client.stream():
                await self._process_tick(tick)
        except Exception as e:
            logger.error(f"Stream error: {e}", exc_info=True)
            raise
        finally:
            await self._publish_status('disconnected')
    
    async def _process_tick(self, tick: Dict):
        """Process and distribute incoming tick"""
        try:
            # Normalize tick format
            normalized_tick = self._normalize_tick(tick)
            
            # Update last tick time for heartbeat
            self.last_tick_time = datetime.now()
            
            # Add to buffer
            self.tick_buffer.append(normalized_tick)
            
            # Metrics
            self.total_ticks_received += 1
            
            # Publish to Redis
            await self._publish_tick(normalized_tick)
            
            # Log every 100 ticks
            if self.total_ticks_received % 100 == 0:
                logger.debug(
                    f"📊 {self.broker} stats: "
                    f"received={self.total_ticks_received}, "
                    f"published={self.total_ticks_published}, "
                    f"buffer_size={len(self.tick_buffer)}"
                )
        
        except Exception as e:
            logger.error(f"Error processing tick: {e}", exc_info=True)
    
    def _normalize_tick(self, tick: Dict) -> TickData:
        """Convert broker-specific tick to standard format"""
        # Broker-specific normalization
        if self.broker == 'fyers':
            return TickData(
                symbol=tick.get('symbol', ''),
                ltp=float(tick.get('ltp', 0) or 0),
                bid=float(tick.get('bid', 0) or 0),
                ask=float(tick.get('ask', 0) or 0),
                volume=int(tick.get('volume', 0) or 0),
                timestamp=tick.get('timestamp') or datetime.now().isoformat(),
                broker='fyers',
                open=float(tick.get('open_price', 0) or 0) if 'open_price' in tick else None,
                high=float(tick.get('high_price', 0) or 0) if 'high_price' in tick else None,
                low=float(tick.get('low_price', 0) or 0) if 'low_price' in tick else None,
                change=float(tick.get('ch', 0) or 0) if 'ch' in tick else None,
                change_pct=float(tick.get('chp', 0) or 0) if 'chp' in tick else None,
            )
        
        elif self.broker == 'delta':
            return TickData(
                symbol=tick.get('symbol', ''),
                ltp=float(tick.get('close', 0) or tick.get('mark_price', 0) or 0),
                bid=float(tick.get('best_bid', 0) or 0),
                ask=float(tick.get('best_ask', 0) or 0),
                volume=int(tick.get('volume', 0) or 0),
                timestamp=tick.get('timestamp') or datetime.now().isoformat(),
                broker='delta',
                open=float(tick.get('open', 0) or 0) if 'open' in tick else None,
                high=float(tick.get('high', 0) or 0) if 'high' in tick else None,
                low=float(tick.get('low', 0) or 0) if 'low' in tick else None,
            )
        
        else:
            # Generic fallback
            return TickData(
                symbol=tick.get('symbol', ''),
                ltp=float(tick.get('ltp', 0) or tick.get('last', 0) or tick.get('price', 0) or 0),
                bid=float(tick.get('bid', 0) or 0),
                ask=float(tick.get('ask', 0) or 0),
                volume=int(tick.get('volume', 0) or 0),
                timestamp=tick.get('timestamp') or datetime.now().isoformat(),
                broker=self.broker,
            )
    
    async def _publish_tick(self, tick: TickData):
        """Publish tick to Redis channels"""
        try:
            tick_dict = asdict(tick)
            tick_json = json.dumps(tick_dict)
            
            # Publish to multiple channels for flexibility
            channels = [
                f"ticks:{self.broker}",           # Broker-specific channel
                "ticks:normalized",                # All normalized ticks
                f"ticks:symbol:{tick.symbol}",    # Symbol-specific channel
            ]
            
            for channel in channels:
                await self.redis.publish(channel, tick_json)
            
            self.total_ticks_published += 1
            
        except Exception as e:
            logger.error(f"Redis publish error: {e}", exc_info=True)
    
    async def _publish_status(self, status: str):
        """Publish connection status"""
        try:
            status_data = {
                'broker': self.broker,
                'status': status,
                'timestamp': datetime.now().isoformat(),
                'subscribed_symbols': list(self.subscribed_symbols),
                'total_ticks_received': self.total_ticks_received,
                'total_ticks_published': self.total_ticks_published,
                'reconnect_count': self.reconnect_count,
            }
            
            await self.redis.publish(
                f"status:{self.broker}",
                json.dumps(status_data)
            )
        
        except Exception as e:
            logger.error(f"Status publish error: {e}")
    
    async def subscribe(self, symbol: str):
        """Add symbol to subscription list"""
        if symbol not in self.subscribed_symbols:
            self.subscribed_symbols.add(symbol)
            logger.info(f"Added {symbol} to {self.broker} subscriptions")
            
            # If already connected, subscribe immediately
            if self.ws_client and self.ws_client.is_connected():
                try:
                    await self.ws_client.subscribe(symbol)
                    logger.info(f"✅ Subscribed to {symbol} on {self.broker}")
                except Exception as e:
                    logger.error(f"Failed to subscribe to {symbol}: {e}")
    
    async def unsubscribe(self, symbol: str):
        """Remove symbol from subscription list"""
        if symbol in self.subscribed_symbols:
            self.subscribed_symbols.remove(symbol)
            logger.info(f"Removed {symbol} from {self.broker} subscriptions")
            
            if self.ws_client and self.ws_client.is_connected():
                try:
                    await self.ws_client.unsubscribe(symbol)
                    logger.info(f"✅ Unsubscribed from {symbol} on {self.broker}")
                except Exception as e:
                    logger.error(f"Failed to unsubscribe from {symbol}: {e}")
    
    async def monitor_heartbeat(self):
        """Monitor connection health and reconnect if needed"""
        while self.is_running:
            await asyncio.sleep(self.heartbeat_interval)
            
            if self.last_tick_time:
                elapsed = (datetime.now() - self.last_tick_time).seconds
                
                if elapsed > self.max_silence_duration:
                    logger.warning(
                        f"⚠️ No ticks for {elapsed}s on {self.broker} - reconnecting"
                    )
                    await self._handle_reconnect()
    
    async def _handle_reconnect(self):
        """Handle reconnection with exponential backoff"""
        self.reconnect_count += 1
        
        logger.info(
            f"Reconnecting {self.broker} "
            f"(attempt #{self.reconnect_count}, delay={self.current_reconnect_delay}s)"
        )
        
        # Disconnect existing connection
        if self.ws_client:
            try:
                await self.ws_client.disconnect()
            except Exception:
                pass
        
        # Wait before reconnecting
        await asyncio.sleep(self.current_reconnect_delay)
        
        # Exponential backoff
        self.current_reconnect_delay = min(
            self.current_reconnect_delay * 2,
            self.max_reconnect_delay
        )
    
    def get_buffered_ticks(
        self,
        symbol: Optional[str] = None,
        since: Optional[datetime] = None
    ) -> List[TickData]:
        """
        Get recent ticks from buffer (for replay)
        
        Args:
            symbol: Filter by symbol (optional)
            since: Get ticks after this timestamp (optional)
        """
        ticks = list(self.tick_buffer)
        
        if symbol:
            ticks = [t for t in ticks if t.symbol == symbol]
        
        if since:
            ticks = [
                t for t in ticks
                if datetime.fromisoformat(t.timestamp) > since
            ]
        
        return ticks
    
    def get_stats(self) -> Dict:
        """Get service statistics"""
        return {
            'broker': self.broker,
            'is_running': self.is_running,
            'subscribed_symbols': list(self.subscribed_symbols),
            'total_ticks_received': self.total_ticks_received,
            'total_ticks_published': self.total_ticks_published,
            'buffer_size': len(self.tick_buffer),
            'reconnect_count': self.reconnect_count,
            'last_tick_time': self.last_tick_time.isoformat() if self.last_tick_time else None,
        }


# ── Global Service Registry ──
_ingestion_services: Dict[str, TickIngestionService] = {}
_service_tasks: Dict[str, asyncio.Task] = {}


def get_ingestion_service(broker: str) -> TickIngestionService:
    """
    Get or create ingestion service for broker
    
    Usage:
        service = get_ingestion_service('fyers')
        service.subscribe('NSE:NIFTY50-INDEX')
    """
    if broker not in _ingestion_services:
        _ingestion_services[broker] = TickIngestionService(broker)
    
    return _ingestion_services[broker]


async def start_all_ingestion_services():
    """
    Start all configured ingestion services
    
    Usage:
        # In Django startup (apps/market/apps.py)
        import asyncio
        from apps.market.tick_ingestion_service import start_all_ingestion_services
        
        asyncio.create_task(start_all_ingestion_services())
    """
    brokers = getattr(settings, 'ENABLED_BROKERS', ['fyers', 'delta'])
    
    logger.info(f"🚀 Starting ingestion services for: {brokers}")
    
    tasks = []
    for broker in brokers:
        service = get_ingestion_service(broker)
        
        # Start main ingestion task
        ingestion_task = asyncio.create_task(service.start())
        tasks.append(ingestion_task)
        _service_tasks[f'{broker}_ingestion'] = ingestion_task
        
        # Start heartbeat monitor
        heartbeat_task = asyncio.create_task(service.monitor_heartbeat())
        tasks.append(heartbeat_task)
        _service_tasks[f'{broker}_heartbeat'] = heartbeat_task
    
    # Wait for all tasks
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Ingestion services cancelled")
        # Stop all services
        for service in _ingestion_services.values():
            await service.stop()


async def stop_all_ingestion_services():
    """Stop all running ingestion services"""
    logger.info("Stopping all ingestion services...")
    
    # Cancel all tasks
    for task_name, task in _service_tasks.items():
        if not task.done():
            task.cancel()
            logger.info(f"Cancelled task: {task_name}")
    
    # Stop all services
    for broker, service in _ingestion_services.items():
        await service.stop()
        logger.info(f"Stopped service: {broker}")
    
    _ingestion_services.clear()
    _service_tasks.clear()
    
    logger.info("✅ All ingestion services stopped")


def get_service_stats() -> Dict[str, Dict]:
    """Get stats for all running services"""
    return {
        broker: service.get_stats()
        for broker, service in _ingestion_services.items()
    }