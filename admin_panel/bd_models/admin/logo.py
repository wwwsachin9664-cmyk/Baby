from django.contrib import admin
from django.utils.safestring import mark_safe

from ..models import Logo


@admin.register(Logo)
class LogoAdmin(admin.ModelAdmin):
    list_display = ("name", "logo_preview", "url", "pathname")
    search_fields = ("name",)
    fields = ("name", "url", "pathname", "image", "logo_preview")
    readonly_fields = ("logo_preview",)

    @admin.display(description="Preview")
    def logo_preview(self, obj: Logo):
        if obj.image:
            return mark_safe(f'<img src="/media/{obj.image}" height="60px" style="border-radius:4px;" />')
        if obj.pathname:
            return mark_safe(f'<img src="/media/logos/{obj.pathname}" height="60px" style="border-radius:4px;" />')
        return "—"
