from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0021_fee_transfer_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="botwallettransfer",
            name="fee_amount_base_units",
            field=models.DecimalField(decimal_places=0, default=0, max_digits=78),
        ),
        migrations.AddField(
            model_name="botwallettransfer",
            name="total_reserved_base_units",
            field=models.DecimalField(decimal_places=0, default=0, max_digits=78),
        ),
        migrations.AddField(
            model_name="fundingintent",
            name="fee_amount_base_units",
            field=models.DecimalField(decimal_places=0, default=0, max_digits=78),
        ),
        migrations.AddField(
            model_name="fundingintent",
            name="net_amount_base_units",
            field=models.DecimalField(decimal_places=0, default=0, max_digits=78),
        ),
        migrations.AddField(
            model_name="withdrawalrequest",
            name="fee_amount_base_units",
            field=models.DecimalField(decimal_places=0, default=0, max_digits=78),
        ),
        migrations.AddField(
            model_name="withdrawalrequest",
            name="total_reserved_base_units",
            field=models.DecimalField(decimal_places=0, default=0, max_digits=78),
        ),
    ]
