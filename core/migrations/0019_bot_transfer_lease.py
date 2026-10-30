from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("core", "0018_profit_compound_proof")]

    operations = [
        migrations.AddField(
            model_name="botwallettransfer",
            name="attempt_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="botwallettransfer",
            name="lease_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
