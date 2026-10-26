from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0014_execution_sell_bot_wallet")]

    operations = [
        migrations.CreateModel(
            name="UserBotControl",
            fields=[
                (
                    "owner",
                    models.OneToOneField(
                        db_column="owner_id",
                        on_delete=django.db.models.deletion.PROTECT,
                        primary_key=True,
                        related_name="bot_control",
                        serialize=False,
                        to="core.walletidentity",
                    ),
                ),
                ("wallet_mode", models.CharField(default="external-bot", max_length=24)),
                ("enabled", models.BooleanField(default=True)),
                ("paused", models.BooleanField(default=False)),
                ("min_net_bps", models.IntegerField(default=100)),
                ("max_notional_usd", models.DecimalField(decimal_places=8, default=25, max_digits=30)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "user_bot_controls"},
        ),
    ]
