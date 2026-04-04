from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bd_models", "0014_alter_ball_options_alter_ballinstance_options_and_more")]

    operations = [
        migrations.AddField(
            model_name="ball",
            name="spawnable",
            field=models.BooleanField(
                default=True,
                help_text="If False, this ball will never spawn randomly (even if enabled). Set via /cardmaker or /editcard.",
            ),
        ),
    ]
