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
_SPEECH_RENDER_RE = re.compile(
    r"\[\[SPEECH\]\](.*?)\[\[RU\]\](.*?)\[\[/SPEECH\]\]",
    flags=re.DOTALL,
)


def _render_speech_markup(escaped):
    def replace(match):
        original = match.group(1).strip()
        translation = match.group(2).strip()
        return (
            '<span class="translated-speech" tabindex="0" '
            f'data-translation="{translation}" '
            'aria-label="Перевод на русский">'
            f'{original}'
            '</span>'
        )

    return _SPEECH_RENDER_RE.sub(replace, escaped)


def _render_saoot_markup(escaped):
    def replace(match):
        player_name = match.group(2)
        body = match.group(3)
        return (
            '<span class="saoot-resolution">'
            f'<span class="saoot-label">SAOOT · {player_name}</span>'
            f'<span class="saoot-body">{body}</span>'
            '</span>'
        )

    return _SAOOT_RENDER_RE.sub(replace, escaped)


@register.filter
def message_format(value):
    """Escape message text, then render controlled SPEECH and SAOOT markup."""
    escaped = escape(value or "")
    rendered = _render_speech_markup(escaped)
    rendered = _render_saoot_markup(rendered)
    return mark_safe(rendered)


@register.filter
def saoot_format(value):
    """Backward-compatible alias for templates/plugins using the old filter."""
    return message_format(value)
