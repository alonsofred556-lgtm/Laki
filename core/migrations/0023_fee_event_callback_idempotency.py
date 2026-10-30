from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0022_fee_amounts_on_funding_and_withdrawals"),
    ]

    operations = [
        migrations.AddField(
            model_name="feeevent",
            name="last_event_key",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="feeevent",
            name="last_event",
            field=models.CharField(blank=True, max_length=16),
        ),
    ]
