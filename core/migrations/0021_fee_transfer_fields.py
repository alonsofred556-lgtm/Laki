from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0020_withdrawal_signer_lifecycle"),
    ]

    operations = [
        migrations.AddField(
            model_name="feeevent",
            name="kind",
            field=models.CharField(default="trade", max_length=24),
        ),
        migrations.AddField(
            model_name="feeevent",
            name="source_id",
            field=models.CharField(default="", max_length=128),
        ),
        migrations.AddField(
            model_name="feeevent",
            name="gross_amount_base_units",
            field=models.DecimalField(decimal_places=0, default=0, max_digits=78),
        ),
        migrations.AddField(
            model_name="feeevent",
            name="fee_bps",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="feeevent",
            name="idempotency_key",
            field=models.CharField(default=uuid.uuid4, max_length=160, unique=True),
        ),
        migrations.AddField(
            model_name="feeevent",
            name="tx_hash",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="feeevent",
            name="attempt_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="feeevent",
            name="lease_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="feeevent",
            name="status",
            field=models.CharField(default="pending", max_length=16),
        ),
    ]
