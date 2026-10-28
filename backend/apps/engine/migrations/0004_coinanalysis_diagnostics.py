from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("engine", "0003_analysisrun_scope"),
    ]

    operations = [
        migrations.AddField(
            model_name="coinanalysis",
            name="diagnostics",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
