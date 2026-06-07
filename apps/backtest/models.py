# apps/backtest/models.py

import uuid

from django.contrib.auth import get_user_model
from django.db import models

User = get_user_model()


class BacktestRun(models.Model):

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="backtests")

    name = models.CharField(max_length=200)
    symbol = models.CharField(max_length=20)
    timeframe = models.CharField(max_length=10, default="1h")  # 1m, 5m, 1h, 1d …
    start_date = models.DateField()
    end_date = models.DateField()

    # Strategy config — JSON dict that the strategy class reads
    strategy_name = models.CharField(max_length=100)
    strategy_params = models.JSONField(default=dict)

    # Capital / risk params
    initial_capital = models.DecimalField(
        max_digits=20, decimal_places=2, default=10000
    )
    fee_rate = models.DecimalField(max_digits=8, decimal_places=6, default=0.001)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    error_message = models.TextField(blank=True, default="")

    # Results — populated after run completes
    results = models.JSONField(null=True, blank=True)

    # 🔥 ONLY THIS LINE ADDED (बाकी सब same)
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.status}] {self.name} | {self.symbol} {self.start_date}→{self.end_date}"


class OptimizerRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="optimizer_runs")
    strategy_name = models.CharField(max_length=100)
    symbol = models.CharField(max_length=20)
    timeframe = models.CharField(max_length=10, default="15")
    start_date = models.DateField()
    end_date = models.DateField()
    initial_capital = models.DecimalField(max_digits=20, decimal_places=2, default=100000)
    param_ranges = models.JSONField(default=dict)
    objective = models.CharField(max_length=50, default="balanced")
    train_ratio = models.FloatField(default=0.7)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True, default="")
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    progress = models.IntegerField(default=0)
    total_combinations = models.IntegerField(default=0)
    completed_combinations = models.IntegerField(default=0)
    best_params = models.JSONField(null=True, blank=True)
    best_score = models.FloatField(null=True, blank=True)
    all_results = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Optimizer({self.strategy_name}|{self.symbol}|{self.status})"
