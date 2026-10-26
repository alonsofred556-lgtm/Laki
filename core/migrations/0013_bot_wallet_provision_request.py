import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("core", "0012_bot_wallet_address_unique")]

    operations = [
        migrations.CreateModel(
            name="BotWalletProvisionRequest",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(default="pending", max_length=24)),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("lease_until", models.DateTimeField(blank=True, null=True)),
                ("failure_code", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "bot_wallet",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="provision_request",
                        to="core.botwallet",
                    ),
                ),
            ],
            options={
                "db_table": "bot_wallet_provision_requests",
                "indexes": [models.Index(fields=["status", "lease_until"], name="bot_provision_status_lease")],
            },
        ),
    ]
