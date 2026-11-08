from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("Preferences", {"fields": ("timezone", "theme", "email_verified")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Preferences", {"fields": ("email", "timezone", "theme")}),
    )
    list_display = ("email", "username", "timezone", "email_verified", "is_staff")