from .models import Tag

DEFAULT_EMOTIONS = [
    "Calm", "Confident", "Focused", "Anxious", "Fearful", "Greedy",
    "Impatient", "Frustrated", "Angry", "Overconfident", "Bored", "Tired",
]


def ensure_default_emotions(user):
    """Give a user a starter set of emotion tags the first time they ask for them."""
    if Tag.objects.filter(user=user, kind=Tag.Kind.EMOTION).exists():
        return
    Tag.objects.bulk_create(
        [Tag(user=user, name=name, kind=Tag.Kind.EMOTION) for name in DEFAULT_EMOTIONS],
        ignore_conflicts=True,
    )