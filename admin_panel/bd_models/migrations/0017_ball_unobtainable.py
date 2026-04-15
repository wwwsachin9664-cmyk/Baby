from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bd_models", "0016_add_logo_model"),
    ]

    operations = [
        migrations.AddField(
            model_name="ball",
            name="unobtainable",
            field=models.BooleanField(
                default=False,
                help_text="If True, this card cannot be obtained from random spawn, daily, or weekly rewards.",
            ),
        ),
    ]