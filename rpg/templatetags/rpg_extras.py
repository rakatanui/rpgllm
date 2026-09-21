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


_OOC_PREFIXES = (
    "[OOC PLAYER]",
    "[OOC GM]",
)


@register.filter
def message_visible_content(message):
    """Return human-facing message text without transport-only OOC prefixes."""
    value = getattr(message, "content", "") or ""
    stripped = value.strip()
    for prefix in _OOC_PREFIXES:
        if stripped.startswith(prefix):
            return stripped[len(prefix):].lstrip("\n ").strip()
    return stripped


@register.filter
def message_type_label(message):
    """Small UI label without changing stored action/message semantics."""
    action = (getattr(message, "action_type", "") or "").strip().upper()
    if action:
        return action
    content = (getattr(message, "content", "") or "").strip()
    if any(content.startswith(prefix) for prefix in _OOC_PREFIXES):
        return "OOC"
    if (getattr(message, "author_type", "") or "").upper() == "SYSTEM":
        return "SYSTEM"
    return ""
