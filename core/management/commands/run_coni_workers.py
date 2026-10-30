"""Run Coni's long-lived private workers as one deployable service.

The web service remains request/response-only. Railway should run this command
in a separate worker service using the same image and environment. Child
commands already own their polling intervals and remain safe to restart
because their database cursors and leases are durable.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from django.core.management.base import BaseCommand, CommandError


DEFAULT_WORKERS = (
    "scan_arbitrage",
    "quote_arbitrage_opportunities",
    "verify_deposits",
    "reconcile_ledger",
    "plan_arbitrage_executions",
    "queue_profit_settlements",
)


def configured_workers():
    raw = os.environ.get("CONI_WORKER_COMMANDS", "").strip()
    if not raw:
        return DEFAULT_WORKERS
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    allowed = set(DEFAULT_WORKERS)
    if not values or any(item not in allowed for item in values):
        raise CommandError("CONI_WORKER_COMMANDS contains an unsupported worker")
    return values


class Command(BaseCommand):
    help = "Run the durable Coni market, deposit, reconciliation, and planning workers."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run each selected worker once and exit.")

    def handle(self, *args, **options):
        workers = configured_workers()
        once = bool(options["once"])
        processes = []
        stopping = False

        def stop_children(*_signals):
            nonlocal stopping
            stopping = True
            for process in processes:
                if process.poll() is None:
                    process.terminate()

        previous_int = signal.signal(signal.SIGINT, stop_children)
        previous_term = signal.signal(signal.SIGTERM, stop_children)
        try:
            for worker in workers:
                command = [sys.executable, "manage.py", worker]
                if once:
                    command.append("--once")
                process = subprocess.Popen(command, env=os.environ.copy())
                processes.append(process)
                self.stdout.write(self.style.SUCCESS(f"started {worker} pid={process.pid}"))

            if once:
                while any(process.poll() is None for process in processes):
                    time.sleep(0.2)
                failed = [worker for worker, process in zip(workers, processes) if process.returncode]
                if failed:
                    raise CommandError(f"worker failed: {', '.join(failed)}")
                return

            while not stopping:
                for worker, process in zip(workers, processes):
                    if process.poll() is not None:
                        raise CommandError(f"worker exited unexpectedly: {worker} ({process.returncode})")
                time.sleep(2)
        finally:
            stop_children()
            for process in processes:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
