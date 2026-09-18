"""Template tags for rpg app."""
from django import template

register = template.Library()


@register.filter
def get(d, key):
    """Look up dict key (e.g. player_private|get:p.pk). Returns None if missing."""
    if d is None:
        return None
    try:
        return d.get(key)
    except AttributeError:
        try:
            return d[key]
        except (KeyError, TypeError, IndexError):
            return None


@register.filter
def index(lst, i):
    try:
        return lst[i]
    except (IndexError, TypeError):
        return None