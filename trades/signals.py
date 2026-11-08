from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from . import rollups
from . import screenshots as shots
from .models import Screenshot, Trade, TradingAccount

User = get_user_model()


def _owner_deletion(origin):
    """True when a whole account or user is being deleted, as an instance or a queryset."""
    model = getattr(origin, "model", origin.__class__)
    return model in (TradingAccount, User)


@receiver(pre_save, sender=Trade)
def remember_previous_position(sender, instance, **kwargs):
    instance._rollup_before = None
    if instance.pk:
        instance._rollup_before = (
            Trade.objects.filter(pk=instance.pk).values_list("account_id", "closed_at").first()
        )


@receiver(post_save, sender=Trade)
def trade_saved(sender, instance, raw=False, **kwargs):
    if raw:  # loaddata
        return
    before = getattr(instance, "_rollup_before", None)
    with rollups.deferred():
        rollups.touch(instance.account_id, {instance.closed_at})
        if before:  # the trade may have moved day or account
            rollups.touch(before[0], {before[1]})


@receiver(post_delete, sender=Trade)
def trade_deleted(sender, instance, origin=None, **kwargs):
    # Deleting a whole account or user removes its rollups too
    if _owner_deletion(origin):
        return
    rollups.touch(instance.account_id, {instance.closed_at})


@receiver(post_delete, sender=Screenshot)
def screenshot_deleted(sender, instance, **kwargs):
    # Remove the files only once the database delete has really committed
    keys = [key for key in (instance.key, instance.thumb_key) if key]

    def remove():
        for key in keys:
            shots.delete_file(key)

    transaction.on_commit(remove)


@receiver(pre_save, sender=User)
def remember_timezone(sender, instance, **kwargs):
    instance._previous_timezone = None
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "timezone" not in update_fields:
        return  # e.g. the last_login update on every sign-in
    if instance.pk:
        instance._previous_timezone = (
            User.objects.filter(pk=instance.pk).values_list("timezone", flat=True).first()
        )


@receiver(post_save, sender=User)
def timezone_changed(sender, instance, created=False, raw=False, **kwargs):
    if raw or created:
        return
    previous = getattr(instance, "_previous_timezone", None)
    if previous is not None and previous != instance.timezone:
        rollups.rebuild_user(instance)