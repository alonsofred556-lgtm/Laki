from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("core", "0008_execution_lifecycle")]

    operations = [
        migrations.AddField(
            model_name="execution",
            name="quote_artifact",
            field=models.JSONField(default=dict),
        ),
    ]
