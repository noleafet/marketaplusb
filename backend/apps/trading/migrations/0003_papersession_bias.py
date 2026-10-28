from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("trading", "0002_papersession_risk_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="papersession",
            name="bias",
            field=models.CharField(
                choices=[("bullish", "Bullish"), ("bearish", "Bearish")],
                default="bullish",
                max_length=8,
            ),
        ),
    ]
