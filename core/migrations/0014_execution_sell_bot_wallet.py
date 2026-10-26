from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0013_bot_wallet_provision_request")]

    operations = [
        migrations.AddField(
            model_name="execution",
            name="sell_bot_wallet",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sell_executions",
                to="core.botwallet",
            ),
        ),
    ]
