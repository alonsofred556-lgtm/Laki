import json

from django.core.management.base import BaseCommand, CommandError

from core.asset_attestation import verify_asset_registry


class Command(BaseCommand):
    help = "Verify configured token addresses, chain identity, and on-chain decimal scales without changing state."

    def handle(self, *args, **options):
        report = verify_asset_registry()
        self.stdout.write(json.dumps(report, sort_keys=True))
        if not report["ready"]:
            raise CommandError("asset registry attestation is not ready; token funding must remain gated")
