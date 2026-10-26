from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("core", "0011_profit_settlement_event")]

    operations = [
        migrations.AddConstraint(
            model_name="botwallet",
            constraint=models.UniqueConstraint(
                condition=~models.Q(address=""),
                fields=("chain", "address"),
                name="bot_wallet_chain_address_once",
            ),
        ),
    ]
