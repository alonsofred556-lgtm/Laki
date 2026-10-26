import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("core", "0010_bot_wallet_transfer_event")]

    operations = [
        migrations.CreateModel(
            name="ProfitSettlementEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("idempotency_key", models.CharField(max_length=128, unique=True)),
                ("event", models.CharField(max_length=24)),
                ("tx_hash", models.CharField(blank=True, max_length=160)),
                ("metadata", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("settlement", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="events", to="core.profitsettlement")),
            ],
            options={
                "db_table": "profit_settlement_events",
                "indexes": [models.Index(fields=["settlement", "created_at"], name="profit_event_created")],
            },
        ),
    ]
