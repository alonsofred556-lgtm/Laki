import time

from django.core.management.base import BaseCommand

from core.reconciliation import reconcile_ledger


class Command(BaseCommand):
    help = "Reconcile immutable ledger entries and persist anomalies for operations review."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one reconciliation pass and exit.")
        parser.add_argument("--loop-seconds", type=int, default=60, help="Delay between passes when not using --once.")

    def handle(self, *args, **options):
        while True:
            result = reconcile_ledger()
            if result["anomalies"]:
                self.stderr.write(self.style.ERROR(str(result)))
            else:
                self.stdout.write(self.style.SUCCESS(str(result)))
            if options["once"]:
                return
            time.sleep(max(10, options["loop_seconds"]))
