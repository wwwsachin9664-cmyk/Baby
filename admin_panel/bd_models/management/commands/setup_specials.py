"""
Management command: setup_specials

Ensures the required CricStar special events exist in the database.
Run automatically from run.py on each bot startup.
"""
from django.core.management.base import BaseCommand

REQUIRED_SPECIALS = [
    {
        "name": "T20 World Cup",
        "catch_phrase": "You caught a T20 World Cup special edition card!",
        "rarity": 0.05,
        "emoji": "🏆",
        "tradeable": True,
        "hidden": False,
        "credits": "CricStar",
        "is_event": True,
    },
    {
        "name": "IPL2026",
        "catch_phrase": "You caught an IPL 2026 special edition card!",
        "rarity": 0.05,
        "emoji": "🏏",
        "tradeable": True,
        "hidden": False,
        "credits": "CricStar",
        "is_event": True,
    },
    {
        "name": "ICONS",
        "catch_phrase": "You caught an **Icon Card**!",
        "rarity": 0.05,
        "emoji": None,
        "tradeable": True,
        "hidden": False,
        "credits": "CricStar",
        "is_event": True,
    },
]

EVENT_NAMES = {"T20 World Cup", "IPL2026", "ICONS"}


class Command(BaseCommand):
    help = "Ensure required CricStar special events exist in the database."

    def handle(self, *args, **options):
        from bd_models.models import Special

        created_count = 0
        for data in REQUIRED_SPECIALS:
            name = data["name"]
            defaults = {k: v for k, v in data.items() if k != "name"}
            obj, created = Special.objects.get_or_create(name=name, defaults=defaults)
            if created:
                self.stdout.write(self.style.SUCCESS(f"  Created special: {name} (id={obj.id})"))
                created_count += 1
            else:
                updated = False
                if not obj.is_event and name in EVENT_NAMES:
                    obj.is_event = True
                    obj.save(update_fields=["is_event"])
                    updated = True
                suffix = " (updated is_event=True)" if updated else ""
                self.stdout.write(f"  Special already exists: {name} (id={obj.id}){suffix}")

        if created_count:
            self.stdout.write(self.style.SUCCESS(f"setup_specials: {created_count} special(s) created."))
        else:
            self.stdout.write("setup_specials: all required specials already exist.")
