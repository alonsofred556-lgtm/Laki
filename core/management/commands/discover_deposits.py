import time

from django.core.management.base import BaseCommand

from core.deposit_discovery import discover_deposits


class Command(BaseCommand):
    help = "Discover finalized native/token transfers to configured recipients as unclaimed or claimable observations."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one discovery pass and exit.")
        parser.add_argument("--loop-seconds", type=int, default=30, help="Delay between passes when not using --once.")

    def handle(self, *args, **options):
        while True:
            self.stdout.write(str(discover_deposits()))
            if options["once"]:
                return
            time.sleep(max(5, options["loop_seconds"]))
