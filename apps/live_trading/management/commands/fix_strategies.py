"""
Django Management Command to Fix Strategy Database Issues
Usage: python manage.py fix_strategies

✅ FINAL CORRECTED VERSION - Uses BrokerAccount (not Broker)
"""

from django.core.management.base import BaseCommand
from django.db.models import Q, Count
from apps.live_trading.models import TradingSession, LiveSignal, ActivityLog
from apps.brokers.models import BrokerAccount


class Command(BaseCommand):
    help = 'Fix and verify trading session database entries - resolves "No strategies" issue'

    def add_arguments(self, parser):
        parser.add_argument(
            '--activate-all',
            action='store_true',
            help='Activate all inactive trading sessions',
        )
        parser.add_argument(
            '--show-sessions',
            action='store_true',
            help='Show all trading sessions with details',
        )
        parser.add_argument(
            '--check-brokers',
            action='store_true',
            help='Check broker connections',
        )

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.SUCCESS('='*60))
        self.stdout.write(self.style.SUCCESS('Trading Session Database Diagnostic Tool'))
        self.stdout.write(self.style.SUCCESS('='*60))
        self.stdout.write('')
        
        # 1. Trading Sessions Status
        self.stdout.write(self.style.WARNING('📊 Trading Sessions Status:'))
        self.stdout.write('-'*60)
        
        total_sessions = TradingSession.objects.count()
        active_sessions = TradingSession.objects.filter(is_active=True).count()
        inactive_sessions = TradingSession.objects.filter(is_active=False).count()
        
        self.stdout.write(f'Total Sessions: {total_sessions}')
        self.stdout.write(f'Active Sessions: {active_sessions}')
        self.stdout.write(f'Inactive Sessions: {inactive_sessions}')
        self.stdout.write('')
        
        # 2. Check for strategies (from session strategy_id field)
        unique_strategies = TradingSession.objects.values_list(
            'strategy_id', flat=True
        ).distinct()
        
        self.stdout.write(self.style.WARNING('📋 Unique Strategies Found:'))
        self.stdout.write('-'*60)
        self.stdout.write(f'Total Unique Strategies: {len(unique_strategies)}')
        
        for idx, strategy_id in enumerate(unique_strategies, 1):
            session_count = TradingSession.objects.filter(
                strategy_id=strategy_id
            ).count()
            active_count = TradingSession.objects.filter(
                strategy_id=strategy_id,
                is_active=True
            ).count()
            
            status_icon = '✓' if active_count > 0 else '✗'
            self.stdout.write(f'{idx}. {status_icon} {strategy_id} '
                            f'({session_count} sessions, {active_count} active)')
        self.stdout.write('')
        
        # 3. Signals Statistics
        self.stdout.write(self.style.WARNING('📊 Signal Statistics:'))
        self.stdout.write('-'*60)
        
        total_signals = LiveSignal.objects.count()
        pending_signals = LiveSignal.objects.filter(
            status=LiveSignal.Status.PENDING
        ).count()
        executed_signals = LiveSignal.objects.filter(
            status=LiveSignal.Status.EXECUTED
        ).count()
        
        self.stdout.write(f'Total Signals: {total_signals}')
        self.stdout.write(f'Pending Signals: {pending_signals}')
        self.stdout.write(f'Executed Signals: {executed_signals}')
        self.stdout.write('')
        
        # 4. Broker Check
        if kwargs['check_brokers']:
            self.stdout.write(self.style.WARNING('🔗 Broker Status:'))
            self.stdout.write('-'*60)
            
            total_brokers = BrokerAccount.objects.count()
            active_brokers = BrokerAccount.objects.filter(is_active=True).count()
            
            self.stdout.write(f'Total Broker Accounts: {total_brokers}')
            self.stdout.write(f'Active Broker Accounts: {active_brokers}')
            
            if active_brokers > 0:
                brokers = BrokerAccount.objects.filter(is_active=True)
                for broker in brokers:
                    broker_label = broker.label if broker.label else "(No Label)"
                    self.stdout.write(f'  ✓ {broker.broker} - {broker_label} (Active)')
            else:
                self.stdout.write(self.style.ERROR(
                    '  ⚠ No active broker accounts found!'
                ))
            self.stdout.write('')
        
        # 5. Show detailed sessions
        if kwargs['show_sessions']:
            self.stdout.write(self.style.WARNING('📋 Session Details:'))
            self.stdout.write('-'*60)
            
            sessions = TradingSession.objects.all().order_by('-started_at')[:20]
            
            if sessions.count() == 0:
                self.stdout.write(self.style.ERROR(
                    '✗ No trading sessions found in database'
                ))
            else:
                for idx, session in enumerate(sessions, 1):
                    status_icon = '✓' if session.is_active else '✗'
                    status_color = self.style.SUCCESS if session.is_active else self.style.ERROR
                    
                    duration = 'Running'
                    if session.ended_at:
                        delta = session.ended_at - session.started_at
                        hours = delta.total_seconds() / 3600
                        duration = f'{hours:.1f} hours'
                    
                    self.stdout.write(status_color(
                        f'{idx}. {status_icon} {session.strategy_id} '
                        f'[{session.mode}]'
                    ))
                    self.stdout.write(f'   User: {session.user.username}')
                    self.stdout.write(f'   Started: {session.started_at.strftime("%Y-%m-%d %H:%M")} | Duration: {duration}')
                    self.stdout.write(f'   Trades: {session.total_trades} | Winning: {session.winning_trades} | P&L: ₹{session.total_pnl}')
                    
                    # Signal count for this session
                    signal_count = LiveSignal.objects.filter(session=session).count()
                    self.stdout.write(f'   Signals: {signal_count}')
                    self.stdout.write('')
        
        # 6. Activate all if flag set
        if kwargs['activate_all']:
            self.stdout.write(self.style.WARNING('🔧 Activating all inactive sessions...'))
            
            inactive = TradingSession.objects.filter(is_active=False)
            count = inactive.count()
            
            if count > 0:
                self.stdout.write(f'Found {count} inactive sessions')
                for session in inactive:
                    self.stdout.write(f'  Activating: {session.strategy_id}')
                
                # Update all
                inactive.update(is_active=True)
                self.stdout.write(self.style.SUCCESS(
                    f'✓ Activated {count} sessions'
                ))
            else:
                self.stdout.write('No inactive sessions found')
            self.stdout.write('')
        
        # 7. Activity Log Statistics
        self.stdout.write(self.style.WARNING('📊 Activity Log:'))
        self.stdout.write('-'*60)
        
        total_activities = ActivityLog.objects.count()
        self.stdout.write(f'Total Activities: {total_activities}')
        self.stdout.write('')
        
        # 8. Final Recommendations
        self.stdout.write(self.style.SUCCESS('='*60))
        self.stdout.write(self.style.SUCCESS('💡 Recommendations:'))
        self.stdout.write(self.style.SUCCESS('='*60))
        
        issues = []
        
        if active_sessions == 0 and total_sessions > 0:
            issues.append('⚠ No active sessions! All sessions are stopped.')
            self.stdout.write(self.style.WARNING(
                '⚠ No active trading sessions found.'
            ))
            self.stdout.write(self.style.WARNING(
                '  Run: python manage.py fix_strategies --activate-all'
            ))
        
        if total_sessions == 0:
            issues.append('⚠ No trading sessions in database')
            self.stdout.write(self.style.WARNING(
                '⚠ No trading sessions found.'
            ))
            self.stdout.write(self.style.WARNING(
                '  Start a new trading session from the dashboard'
            ))
        
        if kwargs['check_brokers']:
            if BrokerAccount.objects.filter(is_active=True).count() == 0:
                issues.append('⚠ No active broker accounts')
                self.stdout.write(self.style.ERROR(
                    '⚠ No active broker accounts! Please activate a broker in admin panel'
                ))
        
        if not issues:
            self.stdout.write(self.style.SUCCESS(
                f'✓ System is operational! {active_sessions} active trading sessions'
            ))
        
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('='*60))
        self.stdout.write(self.style.SUCCESS('Done!'))
        self.stdout.write(self.style.SUCCESS('='*60))
        
        # Quick help
        self.stdout.write('')
        self.stdout.write('Quick Commands:')
        self.stdout.write('  --show-sessions   : Show all session details')
        self.stdout.write('  --check-brokers   : Check broker status')
        self.stdout.write('  --activate-all    : Activate all stopped sessions')
        self.stdout.write('')