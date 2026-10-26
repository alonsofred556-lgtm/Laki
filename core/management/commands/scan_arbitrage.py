from django.core.management.base import BaseCommand

from core.arbitrage import scan_opportunities
from core.pangea import PangeaError


class Command(BaseCommand):
    help = "Scan Pangea pools, request fresh route quotes, and persist paper-only arbitrage decisions."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one scan and exit.")
        parser.add_argument("--loop-seconds", type=int, default=30, help="Delay between scans.")

    def handle(self, *args, **options):
        import time

        while True:
            try:
                rows = scan_opportunities()
                executable = sum(1 for row in rows if row.get("executable"))
                self.stdout.write(self.style.SUCCESS(str({"scanned": len(rows), "executableOnPaper": executable})))
            except PangeaError as exc:
                self.stderr.write(self.style.WARNING(f"Pangea scan unavailable: {exc}"))
                if options["once"]:
                    raise SystemExit(2) from exc
            if options["once"]:
                return
            time.sleep(max(10, options["loop_seconds"]))
