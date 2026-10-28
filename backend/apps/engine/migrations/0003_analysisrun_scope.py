from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0002_timescale_tables"),
    ]

    operations = [
        migrations.AddField(
            model_name="analysisrun",
            name="scope",
            field=models.CharField(
                choices=[("universe", "Universe"), ("solo", "Solo")],
                default="universe",
                max_length=16,
            ),
        ),
    ]
