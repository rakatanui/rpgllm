"""GM-reviewed scene close summaries."""
from __future__ import annotations

import re

from rpg.models import AuthorType, Message, Player, Scene, Visibility
from rpg.services.context_builder import build_player_context
from rpg.services.llm import get_llm_client


_SCENE_RE = re.compile(r"\[\[SCENE\]\](.*?)\[\[/SCENE\]\]", re.DOTALL)
_HOOKS_RE = re.compile(r"\[\[HOOKS\]\](.*?)\[\[/HOOKS\]\]", re.DOTALL)


def _model_for(player: Player) -> tuple[str, float]:
    if player.model_config and player.model_config.enabled:
        return player.model_config.gateway_model, player.model_config.temperature
    return "mock-echo", 0.3


def generate_close_summary(scene: Scene) -> dict:
    players = list(
        Player.objects.filter(scene_participations__scene=scene)
        .select_related("model_config")
        .order_by("scene_participations__order", "scene_participations__pk")
    )
    if not players:
        return {"scene_summary": "", "open_hooks": "", "players": {}}

    public_messages = list(
        Message.objects.filter(scene=scene, visibility=Visibility.PUBLIC)
        .select_related("author_player")
        .order_by("created_at", "pk")
    )
    transcript = []
    for message in public_messages:
        if message.author_type == AuthorType.GM:
            who = "GM"
        elif message.author_player_id:
            who = message.author_player.display_name
        else:
            who = message.author_type
        action = f" [{message.action_type}]" if message.action_type else ""
        transcript.append(f"{who}{action}: {message.content}")

    client = get_llm_client()
    summary_model, summary_temp = _model_for(players[0])
    summary_prompt = (
        "You are a neutral tabletop-RPG session summarizer for the Game Master. "
        "Use only the supplied PUBLIC transcript. Do not invent facts. Be compact and "
        "preserve unresolved uncertainty. Return the normal JSON envelope with action_type ACT, "
        "private_to_gm empty, and public containing EXACTLY these markers:\n"
        "[[SCENE]]compact factual scene summary[[/SCENE]]\n"
        "[[HOOKS]]unresolved leads, promises, questions and pending consequences[[/HOOKS]]"
    )
    response = client.generate(
        system_prompt=summary_prompt,
        messages=[{"role": "user", "content": "\n\n".join(transcript)[-50000:]}],
        model=summary_model,
        temperature=min(summary_temp, 0.4),
    )
    scene_match = _SCENE_RE.search(response.public or "")
    hooks_match = _HOOKS_RE.search(response.public or "")
    scene_summary = (
        scene_match.group(1).strip()
        if scene_match
        else (response.public or "").strip()
    )
    open_hooks = hooks_match.group(1).strip() if hooks_match else ""

    player_updates = {}
    for player in players:
        context = build_player_context(player=player, scene=scene)
        model, temp = _model_for(player)
        prompt = (
            context.system_prompt
            + "\n\n# CLOSE-SCENE MEMORY DRAFT\n"
            "You are drafting compact long-term memory for this character at scene close. "
            "Use only information visible to this character in the supplied history. "
            "Do not invent facts and do not reveal other players' private information. "
            "Return the normal JSON envelope with action_type ACT, private_to_gm empty, "
            "and public containing only the concise memory update, preferably bullet-like "
            "facts, commitments, suspicions, relationships, and unresolved intentions worth "
            "remembering after old transcript is trimmed."
        )
        memory_response = client.generate(
            system_prompt=prompt,
            messages=context.messages,
            model=model,
            temperature=min(temp, 0.4),
        )
        player_updates[str(player.pk)] = (memory_response.public or "").strip()

    draft = {
        "scene_summary": scene_summary,
        "open_hooks": open_hooks,
        "players": player_updates,
    }
    Scene.objects.filter(pk=scene.pk).update(close_summary_draft=draft)
    return draft
