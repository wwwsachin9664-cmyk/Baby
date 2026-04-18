from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bd_models", "0017_ball_unobtainable"),
    ]

    operations = [
        migrations.AddField(
            model_name="guildconfig",
            name="last_spawn_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="Timestamp of the last cricketer spawn in this guild — persisted across restarts",
            ),
        ),
        migrations.AddField(
            model_name="guildconfig",
            name="spawn_threshold",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                help_text="Current spawn threshold for this guild — persisted across restarts",
            ),
        ),
    ]
