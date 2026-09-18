"""Template tags for rpg app."""
import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

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

_SAOOT_RENDER_RE = re.compile(
    r"\[\[SAOOT:(\d+)\|([^\]]+)\]\](.*?)\[\[/SAOOT\]\]",
    flags=re.DOTALL,
)


@register.filter
def saoot_format(value):
    """Escape message text and render GM SAOOT markers as readable highlights."""
    escaped = escape(value or "")

    def replace(match):
        player_name = match.group(2)
        body = match.group(3)
        return (
            '<span class="saoot-resolution">'
            f'<span class="saoot-label">SAOOT · {player_name}</span>'
            f'<span class="saoot-body">{body}</span>'
            '</span>'
        )

    return mark_safe(_SAOOT_RENDER_RE.sub(replace, escaped))
