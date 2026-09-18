"""Context Builder.

Builds the LLM prompt for a given Player. This is the security boundary of
the application: it NEVER includes private messages of other players.

Context(Player X) =
    SYSTEM RULES (campaign)
  + CHARACTER PROMPT (player X)
  + SCENE STATE (description)
  + PUBLIC HISTORY (all public messages in scene, in order)
  + PRIVATE HISTORY (PRIVATE_GM_PLAYER messages where X is the private_player)
  + CURRENT GM INPUT (the GM message that triggered the turn)

GM_ONLY messages are NEVER included in any player context.
Other players' PRIVATE_GM_PLAYER messages are NEVER included.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from rpg.models import (
    AuthorType,
    Message,
    Player,
    Scene,
    Visibility,
)


@dataclass
class BuiltContext:
    system_prompt: str
    messages: list[dict]  # list of {"role": "...", "content": "..."} for LLM


def build_player_context(
    *,
    player: Player,
    scene: Scene,
    trigger_message: Message | None = None,
    history: Iterable[Message] | None = None,
) -> BuiltContext:
    """Build the chat context visible to a single player.

    Privacy contract enforced here:
      - only PUBLIC and PRIVATE_GM_PLAYER(for this player) messages are used
      - GM_ONLY and other players' private messages are strictly excluded
    """
    campaign = player.campaign

    # ---- system prompt ----
    parts: list[str] = []
    # sentinel so mock + provider can know the player's name
    parts.append(f"[PLAYER: {player.display_name}]")
    parts.append("# SYSTEM RULES")
    if campaign.system_prompt.strip():
        parts.append(campaign.system_prompt.strip())
    else:
        parts.append("You are a player in a tabletop RPG. Stay in character.")

    parts.append("# YOUR CHARACTER")
    if player.character_prompt.strip():
        parts.append(player.character_prompt.strip())
    else:
        parts.append(f"You are {player.display_name}.")

    parts.append("# SCENE STATE")
    parts.append(f"Scene: {scene.name}")
    if scene.description.strip():
        parts.append(scene.description.strip())

    parts.append(
        "# RESPONSE FORMAT\n"
        'Respond with a JSON object: '
        '{"action_type": "ACT|PASS|ACT_OUT_OF_TURN", "public": "...", "private_to_gm": "..."}\n'
        '"public" is seen by everyone. "private_to_gm" is seen only by the Game Master.\n'
        'Use "private_to_gm" sparingly. Leave it as an empty string for ordinary thoughts, '
        'routine reasoning, atmosphere, or anything already conveyed by "public". '
        'Use it only when you intentionally need to hide important information from the '
        'other players, such as a concealed intention, a secret observation, a confidential '
        'question for the GM, or another materially relevant secret. Never duplicate or '
        'paraphrase the public response there.'
    )

    system_prompt = "\n\n".join(parts)

    # ---- message history (chat) ----
    if history is None:
        qs = Message.objects.filter(scene=scene).select_related("author_player").order_by("created_at")
        history = list(qs)

    chat: list[dict] = []
    for m in history:
        # Privacy filter: the single most important rule.
        if not _player_can_see(m, player):
            continue
        entry = _message_to_chat(m, player)
        if entry is not None:
            chat.append(entry)

    # Append the current trigger GM input as the final user message,
    # but avoid duplicating if it's already the last public GM message.
    if trigger_message is not None and not _already_included(history, trigger_message):
        entry = _message_to_chat(trigger_message, player)
        if entry is not None:
            chat.append(entry)

    return BuiltContext(system_prompt=system_prompt, messages=chat)


def _already_included(history: Iterable[Message], msg: Message) -> bool:
    for m in history:
        if m.pk == msg.pk:
            return True
    return False


def _player_can_see(msg: Message, player: Player) -> bool:
    """Strict visibility check. This is the privacy boundary."""
    if msg.visibility == Visibility.PUBLIC:
        return True
    if msg.visibility == Visibility.PRIVATE_GM_PLAYER:
        # Only this specific player (and GM) see it.
        return msg.private_player_id == player.pk
    if msg.visibility == Visibility.GM_ONLY:
        # Players NEVER see GM_ONLY.
        return False
    return False


def _message_to_chat(msg: Message, viewer: Player) -> dict | None:
    """Map a Message to {"role","content"} for chat format.

    GM messages     -> "user" (GM input as user instructions)
    Player's own    -> "assistant"
    Other players'  -> "user" with name prefix
    System          -> "system"
    """
    if msg.author_type == AuthorType.GM:
        return {"role": "user", "content": f"GM: {msg.content}"}
    if msg.author_type == AuthorType.PLAYER:
        if msg.author_player_id is None:
            return None
        if msg.action_type == "PASS":
            content = f"[PASS] {msg.author_player.display_name}"
        else:
            content = f"{msg.author_player.display_name}: {msg.content}"
        role = "assistant" if msg.author_player_id == viewer.pk else "user"
        return {"role": role, "content": content}
    if msg.author_type == AuthorType.SYSTEM:
        return {"role": "system", "content": msg.content}
    return None