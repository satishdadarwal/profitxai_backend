from django.contrib import admin

# Options app models
from apps.options.models import (
    BacktestRun,
    OptionContract,
    OptionSnapshot,
    OptionSymbol,
    OptionTrade,
)

# Paper trading app models
from apps.paper_trading.models import (
    PaperAccount,
    PaperTopUp,
    PaperTrade,
)

admin.site.register(OptionSymbol)
admin.site.register(OptionContract)
admin.site.register(OptionSnapshot)
admin.site.register(PaperAccount)
admin.site.register(PaperTopUp)
admin.site.register(PaperTrade)
admin.site.register(OptionTrade)
admin.site.register(BacktestRun)
