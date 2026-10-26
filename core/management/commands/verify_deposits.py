import time

from django.core.management.base import BaseCommand

from core.chain_verifier import verify_pending_deposits


class Command(BaseCommand):
    help = "Verify claimed deposits against configured EVM/Solana RPC providers and credit only finalized transfers."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one verification pass and exit.")
        parser.add_argument("--limit", type=int, default=None, help="Maximum pending observations per pass.")
        parser.add_argument("--loop-seconds", type=int, default=30, help="Delay between passes when not using --once.")

    def handle(self, *args, **options):
        if options["limit"] is not None and not 1 <= options["limit"] <= 1000:
            raise SystemExit("--limit must be between 1 and 1000")
        while True:
            counts = verify_pending_deposits(options["limit"])
            self.stdout.write(self.style.SUCCESS(str(counts)))
            if options["once"]:
                return
            time.sleep(max(5, options["loop_seconds"]))
