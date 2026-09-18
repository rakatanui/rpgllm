"""Idempotent seed command creating the demo campaign and players."""
from django.core.management.base import BaseCommand
from django.db import transaction

from rpg.models import Campaign, ModelConfig, Player, Scene, TurnMode


PLAYERS = [
    ("Lucien", "A cautious scout. Speaks tersely. Wary of strangers."),
    ("Mila", "A silver-tongued diplomat. Observant and calculating."),
    ("Mathis", "A battle-hardened veteran. Direct and blunt."),
]


class Command(BaseCommand):
    help = "Seed demo Campaign (МРАЗь), Scene and players (Lucien/Mila/Mathis). Idempotent."

    @transaction.atomic
    def handle(self, *args, **options):
        campaign, created = Campaign.objects.get_or_create(
            name="МРАЗь",
            defaults={
                "description": "Demo campaign for MRAZ Master vertical MVP.",
                "system_prompt": (
                    "You are players in a grim northern RPG. Stay in character. "
                    "The Game Master (GM) describes the world and NPCs. "
                    "Respond concisely in character."
                ),
            },
        )
        self.stdout.write(f"Campaign: {campaign.name} ({'created' if created else 'exists'})")

        # Mock model config
        mock_cfg, mc_created = ModelConfig.objects.get_or_create(
            name="Mock (deterministic)",
            defaults={"gateway_model": "mock-echo", "temperature": 0.7, "enabled": True},
        )

        # Players
        created_players: list[Player] = []
        for name, char in PLAYERS:
            p, p_created = Player.objects.get_or_create(
                campaign=campaign, display_name=name,
                defaults={"character_prompt": char, "model_config": mock_cfg},
            )
            self.stdout.write(f"  Player {p.display_name} ({'created' if p_created else 'exists'})")
            created_players.append(p)

        # Scene
        scene, s_created = Scene.objects.get_or_create(
            campaign=campaign, name="Test scene",
            defaults={
                "description": "A frozen clearing. Footprints lead toward a dark treeline.",
                "mode": TurnMode.ROUND,
            },
        )

        # Round order: Lucien -> Mila -> Mathis (by pk)
        if s_created or not scene.round_order:
            scene.round_order = [p.pk for p in created_players]
            scene.active_player_index = 0
            scene.mode = TurnMode.ROUND
            scene.save()

        self.stdout.write(f"Scene: {scene.name} ({'created' if s_created else 'exists'})")
        self.stdout.write(self.style.SUCCESS("seed_demo complete. Open http://mraz.local/"))