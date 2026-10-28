from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("trading", "0003_papersession_bias"),
    ]

    operations = [
        migrations.AddField(
            model_name="livetradingstate",
            name="mode",
            field=models.CharField(
                choices=[("normal", "Normal"), ("hybrid", "Hybrid")],
                db_index=True,
                default="normal",
                max_length=8,
            ),
        ),
    ]
