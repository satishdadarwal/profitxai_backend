from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from celery.result import AsyncResult
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.backtest.tasks import run_backtest_task

from .engine import _REGISTRY, get_strategy
from .models import BacktestRun


class BacktestListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        runs = BacktestRun.objects.filter(user=request.user).order_by("-created_at")[
            :20
        ]

        data = []
        for r in runs:
            progress = 0

            # 🔥 Celery progress
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

    def post(self, request):
        d = request.data
        strategy_name = d.get("strategy_name")

        if not strategy_name:
            return Response({"error": "strategy_name required"}, status=400)

        # ICT strategies bypass registry check
        ict_strategies = ["ict_mtf", "ict_silver_bullet"]
        if strategy_name not in _REGISTRY and strategy_name not in ict_strategies:
            return Response(
                {"error": f"Available: {list(_REGISTRY.keys()) + ict_strategies}"},
                status=400,
            )

        run = BacktestRun.objects.create(
            user=request.user,
            name=d.get("name", f"{strategy_name} | {d['symbol']}"),
            strategy_name=strategy_name,
            strategy_params=d.get("strategy_params", {}),
            symbol=d["symbol"],
            timeframe=d.get("timeframe", "1h"),
            start_date=d["start_date"],
            end_date=d["end_date"],
            initial_capital=d.get("initial_capital", 10000),
            fee_rate=d.get("fee_rate", 0.001),
            status=BacktestRun.Status.PENDING,
        )

        task = run_backtest_task.delay(str(run.id))   # type: ignore[operator]

        def _send_task():
            run.celery_task_id = task.id
            run.save(update_fields=["celery_task_id"])

        transaction.on_commit(_send_task)

        return Response(
            {"id": str(run.id), "status": "queued", "progress": 0}, status=202
        )


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
