"""
Django Management Command to Fix Strategy Database Issues
Usage: python manage.py fix_strategies
"""

from django.core.management.base import BaseCommand
from django.db.models import Q
from live_trading.models import Strategy, LiveTradingSession, TradingSignal
from brokers.models import Broker


class Command(BaseCommand):
    help = 'Fix and verify strategy database entries - resolves "No strategies" issue'

    def add_arguments(self, parser):
        parser.add_argument(
            '--activate-all',
            action='store_true',
            help='Activate all inactive strategies',
        )
        parser.add_argument(
            '--fix-brokers',
            action='store_true',
            help='Fix strategies with missing or inactive brokers',
        )
        parser.add_argument(
            '--create-sample',
            action='store_true',
            help='Create sample strategies for testing',
        )

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.SUCCESS('='*60))
        self.stdout.write(self.style.SUCCESS('Strategy Database Diagnostic & Fix Tool'))
        self.stdout.write(self.style.SUCCESS('='*60))
        self.stdout.write('')
        
        # 1. Count and display current state
        self.stdout.write(self.style.WARNING('📊 Current Database State:'))
        self.stdout.write('-'*60)
        
        total_strategies = Strategy.objects.count()
        active_strategies = Strategy.objects.filter(is_active=True).count()
        inactive_strategies = Strategy.objects.filter(is_active=False).count()
        null_active_strategies = Strategy.objects.filter(is_active__isnull=True).count()
        
        self.stdout.write(f'Total Strategies: {total_strategies}')
        self.stdout.write(f'Active Strategies: {active_strategies}')
        self.stdout.write(f'Inactive Strategies: {inactive_strategies}')
        self.stdout.write(f'Null is_active field: {null_active_strategies}')
        self.stdout.write('')
        
        # 2. Fix null is_active fields
        if null_active_strategies > 0:
            self.stdout.write(self.style.WARNING('🔧 Fixing null is_active fields...'))
            fixed = Strategy.objects.filter(is_active__isnull=True).update(is_active=False)
            self.stdout.write(self.style.SUCCESS(f'✓ Fixed {fixed} strategies with null is_active'))
            self.stdout.write('')
        
        # 3. Check broker associations
        self.stdout.write(self.style.WARNING('🔗 Broker Associations:'))
        self.stdout.write('-'*60)
        
        total_brokers = Broker.objects.count()
        active_brokers = Broker.objects.filter(is_active=True).count()
        
        strategies_with_broker = Strategy.objects.filter(broker__isnull=False).count()
        strategies_without_broker = Strategy.objects.filter(broker__isnull=True).count()
        strategies_with_inactive_broker = Strategy.objects.filter(
            broker__isnull=False,
            broker__is_active=False
        ).count()
        
        self.stdout.write(f'Total Brokers: {total_brokers}')
        self.stdout.write(f'Active Brokers: {active_brokers}')
        self.stdout.write(f'Strategies with Broker: {strategies_with_broker}')
        self.stdout.write(f'Strategies without Broker: {strategies_without_broker}')
        self.stdout.write(f'Strategies with Inactive Broker: {strategies_with_inactive_broker}')
        self.stdout.write('')
        
        # 4. Fix broker issues if flag is set
        if kwargs['fix_brokers']:
            self.stdout.write(self.style.WARNING('🔧 Fixing broker associations...'))
            
            if active_brokers > 0:
                default_broker = Broker.objects.filter(is_active=True).first()
                
                # Assign default broker to strategies without broker
                if strategies_without_broker > 0:
                    updated = Strategy.objects.filter(broker__isnull=True).update(
                        broker=default_broker
                    )
                    self.stdout.write(self.style.SUCCESS(
                        f'✓ Assigned broker "{default_broker.name}" to {updated} strategies'
                    ))
                
                # Update strategies with inactive brokers
                if strategies_with_inactive_broker > 0:
                    updated = Strategy.objects.filter(
                        broker__is_active=False
                    ).update(broker=default_broker)
                    self.stdout.write(self.style.SUCCESS(
                        f'✓ Updated {updated} strategies to active broker'
                    ))
            else:
                self.stdout.write(self.style.ERROR(
                    '✗ No active brokers found. Please create a broker first.'
                ))
            self.stdout.write('')
        
        # 5. Activate all if flag is set
        if kwargs['activate_all']:
            self.stdout.write(self.style.WARNING('🔧 Activating all strategies...'))
            activated = Strategy.objects.filter(is_active=False).update(is_active=True)
            self.stdout.write(self.style.SUCCESS(f'✓ Activated {activated} strategies'))
            self.stdout.write('')
        
        # 6. Create sample strategies if flag is set
        if kwargs['create_sample']:
            self.stdout.write(self.style.WARNING('🔧 Creating sample strategies...'))
            
            if active_brokers == 0:
                self.stdout.write(self.style.ERROR(
                    '✗ Cannot create sample strategies: No active brokers found'
                ))
            else:
                broker = Broker.objects.filter(is_active=True).first()
                
                sample_strategies = [
                    {
                        'name': 'EMA Crossover NIFTY50',
                        'strategy_type': 'EMA_CROSSOVER',
                        'symbol': 'NIFTY50',
                        'timeframe': '5m',
                        'lot_size': 1,
                        'stop_loss': 50,
                        'take_profit': 100
                    },
                    {
                        'name': 'RSI BANKNIFTY',
                        'strategy_type': 'RSI',
                        'symbol': 'BANKNIFTY',
                        'timeframe': '15m',
                        'lot_size': 1,
                        'stop_loss': 75,
                        'take_profit': 150
                    },
                ]
                
                created_count = 0
                for strategy_data in sample_strategies:
                    # Check if already exists
                    exists = Strategy.objects.filter(
                        name=strategy_data['name']
                    ).exists()
                    
                    if not exists:
                        Strategy.objects.create(
                            **strategy_data,
                            broker=broker,
                            is_active=True
                        )
                        created_count += 1
                        self.stdout.write(f'  ✓ Created: {strategy_data["name"]}')
                
                self.stdout.write(self.style.SUCCESS(
                    f'✓ Created {created_count} sample strategies'
                ))
            self.stdout.write('')
        
        # 7. List all strategies with detailed info
        self.stdout.write(self.style.WARNING('📋 Strategy List:'))
        self.stdout.write('-'*60)
        
        strategies = Strategy.objects.all().select_related('broker').order_by('-created_at')
        
        if strategies.count() == 0:
            self.stdout.write(self.style.ERROR('✗ No strategies found in database'))
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Suggestion: Run with --create-sample flag to create test strategies'
            ))
        else:
            for idx, strategy in enumerate(strategies, 1):
                # Status icon
                status_icon = '✓' if strategy.is_active else '✗'
                status_color = self.style.SUCCESS if strategy.is_active else self.style.ERROR
                
                # Broker info
                broker_name = strategy.broker.name if strategy.broker else '⚠ No Broker'
                broker_status = ''
                if strategy.broker:
                    broker_status = '(Active)' if strategy.broker.is_active else '(Inactive)'
                
                # Display
                self.stdout.write(status_color(
                    f'{idx}. {status_icon} {strategy.name}'
                ))
                self.stdout.write(f'   Symbol: {strategy.symbol} | '
                                f'Type: {strategy.strategy_type} | '
                                f'Timeframe: {strategy.timeframe}')
                self.stdout.write(f'   Broker: {broker_name} {broker_status}')
                self.stdout.write(f'   Lot Size: {strategy.lot_size} | '
                                f'SL: {strategy.stop_loss} | '
                                f'TP: {strategy.take_profit}')
                self.stdout.write(f'   Created: {strategy.created_at.strftime("%Y-%m-%d %H:%M")}')
                self.stdout.write('')
        
        # 8. Show signal statistics
        self.stdout.write(self.style.WARNING('📊 Signal Statistics:'))
        self.stdout.write('-'*60)
        
        total_signals = TradingSignal.objects.count()
        total_sessions = LiveTradingSession.objects.count()
        active_sessions = LiveTradingSession.objects.filter(is_active=True).count()
        
        self.stdout.write(f'Total Signals: {total_signals}')
        self.stdout.write(f'Total Sessions: {total_sessions}')
        self.stdout.write(f'Active Sessions: {active_sessions}')
        self.stdout.write('')
        
        # 9. Final recommendations
        self.stdout.write(self.style.SUCCESS('='*60))
        self.stdout.write(self.style.SUCCESS('💡 Recommendations:'))
        self.stdout.write(self.style.SUCCESS('='*60))
        
        if active_strategies == 0:
            self.stdout.write(self.style.ERROR(
                '⚠ No active strategies! Run: python manage.py fix_strategies --activate-all'
            ))
        
        if active_brokers == 0:
            self.stdout.write(self.style.ERROR(
                '⚠ No active brokers! Please create a broker in admin panel'
            ))
        
        if strategies_without_broker > 0:
            self.stdout.write(self.style.WARNING(
                f'⚠ {strategies_without_broker} strategies without broker! '
                'Run: python manage.py fix_strategies --fix-brokers'
            ))
        
        if total_strategies == 0:
            self.stdout.write(self.style.WARNING(
                '⚠ No strategies in database! '
                'Run: python manage.py fix_strategies --create-sample'
            ))
        
        if active_strategies > 0 and active_brokers > 0:
            self.stdout.write(self.style.SUCCESS(
                f'✓ System is healthy! {active_strategies} active strategies ready to trade'
            ))
        
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('='*60))
        self.stdout.write(self.style.SUCCESS('Done!'))
        self.stdout.write(self.style.SUCCESS('='*60))
