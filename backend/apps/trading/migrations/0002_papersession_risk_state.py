from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="papersession",
            name="risk_state",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
