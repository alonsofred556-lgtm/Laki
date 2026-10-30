from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0019_bot_transfer_lease"),
    ]

    operations = [
        migrations.AddField(
            model_name="withdrawalrequest",
            name="attempt_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="withdrawalrequest",
            name="lease_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="WithdrawalEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("idempotency_key", models.CharField(max_length=128, unique=True)),
                ("event", models.CharField(max_length=24)),
                ("tx_hash", models.CharField(blank=True, max_length=160)),
                ("metadata", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("withdrawal", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="events", to="core.withdrawalrequest")),
            ],
            options={
                "db_table": "withdrawal_events",
                "indexes": [models.Index(fields=["withdrawal", "created_at"], name="withdrawal_event_created")],
            },
        ),
    ]
