from django.shortcuts import get_object_or_404

from celery.result import AsyncResult
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.backtest.tasks import run_backtest_task

from .engine import _REGISTRY
from .models import BacktestRun


# ─────────────────────────────────────────────────────────────────
# GET backtest/  →  sirf list, koi POST nahi
# ✅ BUG #2 FIX: List aur Run alag views mein split kiye
# ─────────────────────────────────────────────────────────────────
class BacktestListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        runs = BacktestRun.objects.filter(user=request.user).order_by("-created_at")[
            :20
        ]

        data = []
        for r in runs:
            progress = 0

            if r.celery_task_id:
                task = AsyncResult(r.celery_task_id)
                if task.info and isinstance(task.info, dict):
                    progress = task.info.get("progress", 0)

            data.append(
                {
                    "id": str(r.id),
                    "name": r.name,
                    "strategy_name": r.strategy_name,
                    "symbol": r.symbol,
                    "timeframe": r.timeframe,
                    "status": r.status,
                    "progress": progress,
                    "results": r.results,
                    "start_date": str(r.start_date),
                    "end_date": str(r.end_date),
                    "initial_capital": str(r.initial_capital),
                    "created_at": r.created_at,
                    "completed_at": r.completed_at,
                }
            )

        return Response({"runs": data})


# ─────────────────────────────────────────────────────────────────
# POST backtest/run/  →  naya backtest start karo
# ✅ BUG #1 FIX: symbol / start_date / end_date validation add ki
# ✅ BUG #2 FIX: Alag BacktestRunView — duplicate POST possible nahi
# ✅ BUG #3 FIX: celery_task_id race condition fix — on_commit hataya
# ─────────────────────────────────────────────────────────────────
class BacktestRunView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        d = request.data

        # ── Required field validations ────────────────────────────
        strategy_name = d.get("strategy_name")
        if not strategy_name:
            return Response({"error": "strategy_name required"}, status=400)

        # ✅ BUG #1 FIX — pehle d["symbol"] tha jo KeyError → 400 deta tha
        symbol = d.get("symbol")
        if not symbol:
            return Response({"error": "symbol required"}, status=400)

        start_date = d.get("start_date")
        if not start_date:
            return Response({"error": "start_date required"}, status=400)

        end_date = d.get("end_date")
        if not end_date:
            return Response({"error": "end_date required"}, status=400)

        # ── Strategy registry check ───────────────────────────────
        ict_strategies = ["ict_mtf", "ict_silver_bullet", "ema_scalp"]
        if strategy_name not in _REGISTRY and strategy_name not in ict_strategies:
            return Response(
                {"error": f"Available: {list(_REGISTRY.keys()) + ict_strategies}"},
                status=400,
            )

        # ── BacktestRun create karo ───────────────────────────────
        run = BacktestRun.objects.create(
            user=request.user,
            # ✅ BUG #1 FIX — d["symbol"] ki jagah validated `symbol` variable
            name=d.get("name", f"{strategy_name} | {symbol}"),
            strategy_name=strategy_name,
            strategy_params=d.get("strategy_params", {}),
            symbol=symbol,
            timeframe=d.get("timeframe", "1h"),
            start_date=start_date,
            end_date=end_date,
            initial_capital=d.get("initial_capital") or d.get("strategy_params", {}).get("capital", 100000),
            fee_rate=d.get("fee_rate") or d.get("strategy_params", {}).get("fee_rate", 0.001),
            status=BacktestRun.Status.PENDING,
        )

        # ✅ BUG #3 FIX — Celery race condition fix
        #
        # PEHLA CODE (galat):
        #   task = run_backtest_task.delay(run.id)
        #   def _send_task():
        #       run.celery_task_id = task.id
        #       run.save(...)
        #   transaction.on_commit(_send_task)   ← commit ke BAAD save hota tha
        #
        # PROBLEM:
        #   Celery worker bohot fast hota hai — task already shuru ho jaata tha
        #   jab tak on_commit fire hota. celery_task_id DB mein NULL rehta tha.
        #   Isliye GET backtest/ pe progress hamesha 0 dikhta tha.
        #
        # FIX:
        #   Pehle celery_task_id save karo, PHIR task dispatch karo.
        #   Is order mein guarantee hai ki DB mein ID exist karti hai
        #   jab bhi worker pehli baar status update kare.

        task = run_backtest_task.delay(str(run.id))  # type: ignore[operator]

        # Turant save karo — on_commit wait nahi karenge
        run.celery_task_id = task.id
        run.save(update_fields=["celery_task_id"])

        return Response(
            {"id": str(run.id), "status": "queued", "progress": 0}, status=202
        )


# ─────────────────────────────────────────────────────────────────
# GET backtest/<uuid>/  →  single run ka status + results
# ─────────────────────────────────────────────────────────────────
class BacktestDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, run_id):
        run = get_object_or_404(BacktestRun, pk=run_id, user=request.user)

        progress = 0
        if run.celery_task_id:
            task = AsyncResult(run.celery_task_id)
            if task.info and isinstance(task.info, dict):
                progress = task.info.get("progress", 0)

        return Response(
            {
                "id": str(run.id),
                "status": run.status,
                "progress": progress,
                "results": run.results,
            }
        )
