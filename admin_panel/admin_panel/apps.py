from django.contrib.admin.apps import AdminConfig


class CricstarAdminConfig(AdminConfig):
    default_site = "admin_panel.admin.CricstarAdminSite"
