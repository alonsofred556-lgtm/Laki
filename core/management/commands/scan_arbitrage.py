from django.core.management.base import BaseCommand
from django.conf import settings

from core.arbitrage import scan_opportunities
from core.pangea import PangeaError


class Command(BaseCommand):
    help = "Scan every Pangea pool route and queue any route beyond the synchronous quote budget."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one scan and exit.")
        parser.add_argument("--loop-seconds", type=int, default=int(getattr(settings, "CONI_ARBITRAGE_SCAN_INTERVAL_SECONDS", 10)), help="Delay between scans.")

    def handle(self, *args, **options):
        import time

        while True:
            try:
                rows = scan_opportunities()
                executable = sum(1 for row in rows if row.get("executable"))
                self.stdout.write(self.style.SUCCESS(str({"scanned": len(rows), "quotedInPass": sum(1 for row in rows if row.get("quoteState") == "fresh"), "queuedForQuotes": sum(1 for row in rows if row.get("quoteState") != "fresh"), "executableOnPaper": executable})))
            except PangeaError as exc:
                self.stderr.write(self.style.WARNING(f"Pangea scan unavailable: {exc}"))
                if options["once"]:
                    raise SystemExit(2) from exc
            if options["once"]:
                return
            time.sleep(max(5, options["loop_seconds"]))
