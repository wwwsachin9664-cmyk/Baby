from django.db import migrations, models


def mark_existing_events(apps, schema_editor):
    Special = apps.get_model("bd_models", "Special")
    event_names = ["ICONS", "IPL2026", "Prime", "Timeliners", "Flashback", "Trophy"]
    Special.objects.filter(name__in=event_names).update(is_event=True)


class Migration(migrations.Migration):

    dependencies = [
        ("bd_models", "0018_guildconfig_spawn_persistence"),
    ]

    operations = [
        migrations.AddField(
            model_name="special",
            name="is_event",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True = card series event (e.g. ICONS, IPL2026). "
                    "False = seasonal overlay special (e.g. Shiny, Diwali)."
                ),
            ),
        ),
        migrations.AddField(
            model_name="special",
            name="overlay_text",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                help_text="Text shown below the card when displayed (e.g. 🪔Diwali🪔). Supports emoji and markdown.",
            ),
        ),
        migrations.RunPython(mark_existing_events, migrations.RunPython.noop),
    ]
