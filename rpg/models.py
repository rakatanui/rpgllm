"""MRAZ Master models.

Core principle: a single Message table for the whole Campaign/Scene with
visibility rules. No separate "chats".

Visibility:
  - PUBLIC            : everyone in the scene sees it
  - PRIVATE_GM_PLAYER : only Master and the designated player see it
  - GM_ONLY           : only Master sees it

Context(Mila) = PUBLIC + PRIVATE(GM, Mila)  -- never other players' private.
"""
from django.db import models
from django.core.exceptions import ValidationError


# ---- enums (re-exported via class attributes) ----
class AuthorType(models.TextChoices):
    GM = "GM", "Game Master"
    PLAYER = "PLAYER", "Player"
    SYSTEM = "SYSTEM", "System"


class Visibility(models.TextChoices):
    PUBLIC = "PUBLIC", "Public"
    PRIVATE_GM_PLAYER = "PRIVATE_GM_PLAYER", "Private GM-Player"
    GM_ONLY = "GM_ONLY", "GM only"


class TurnMode(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    ROUND = "ROUND", "Round"
    SIMULTANEOUS = "SIMULTANEOUS", "Simultaneous"
    TABLE = "TABLE", "Table"


class TurnState(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class PlayerStatus(models.TextChoices):
    IDLE = "idle", "Idle"
    GENERATING = "generating", "Generating"
    ERROR = "error", "Error"


class ModelConfig(models.Model):
    name = models.CharField(max_length=120, unique=True)
    gateway_model = models.CharField(
        max_length=200,
        help_text="LiteLLM model alias, e.g. ollama/llama3.1, openai/gpt-4o, mock-echo",
    )
    temperature = models.FloatField(default=0.7)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Campaign(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    system_prompt = models.TextField(
        blank=True,
        help_text="Global system rules shared with all players (public part).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class Scene(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="scenes")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    mode = models.CharField(max_length=20, choices=TurnMode.choices, default=TurnMode.ROUND)
    round_order = models.JSONField(
        default=list,
        blank=True,
        help_text="Ordered list of Player IDs defining the round order.",
    )
    active_player_index = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.campaign.name} / {self.name}"

    def clean(self):
        super().clean()
        if self.mode == TurnMode.ROUND:
            # active index must be valid
            if self.round_order and not (0 <= self.active_player_index < len(self.round_order)):
                raise ValidationError({"active_player_index": "Out of bounds for round_order."})


class Player(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="players")
    display_name = models.CharField(max_length=120)
    character_prompt = models.TextField(
        blank=True,
        help_text="Character description / system prompt for this player's LLM.",
    )
    model_config = models.ForeignKey(
        ModelConfig, on_delete=models.SET_NULL, null=True, blank=True, related_name="players"
    )
    status = models.CharField(max_length=20, choices=PlayerStatus.choices, default=PlayerStatus.IDLE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        unique_together = [("campaign", "display_name")]

    def __str__(self):
        return self.display_name


class Turn(models.Model):
    scene = models.ForeignKey(Scene, on_delete=models.CASCADE, related_name="turns")
    mode = models.CharField(max_length=20, choices=TurnMode.choices)
    state = models.CharField(max_length=20, choices=TurnState.choices, default=TurnState.PENDING)
    # snapshot of the round order used by this turn (for SIMULTANEOUS snapshot guarantee)
    participants = models.JSONField(default=list, blank=True)
    # GM input message that triggered this turn (the "ask"). Public by default.
    trigger_message = models.ForeignKey(
        "Message",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_turns",
    )
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Turn #{self.pk} ({self.mode}/{self.state})"


class Message(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="messages")
    scene = models.ForeignKey(Scene, on_delete=models.CASCADE, related_name="messages", null=True, blank=True)
    turn = models.ForeignKey(Turn, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages")
    author_type = models.CharField(max_length=20, choices=AuthorType.choices)
    author_player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages"
    )
    content = models.TextField()
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.PUBLIC)
    private_player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, blank=True, related_name="private_messages_for"
    )
    # structured action info (for PLAYER messages produced by LLM)
    action_type = models.CharField(max_length=30, blank=True, default="")
    private_to_gm = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["scene", "visibility"]),
            models.Index(fields=["private_player", "visibility"]),
        ]

    def __str__(self):
        who = self.author_player.display_name if self.author_player else self.author_type
        return f"[{self.visibility}] {who}: {self.content[:60]}"

    def clean(self):
        super().clean()
        if self.visibility == Visibility.PRIVATE_GM_PLAYER and self.private_player is None:
            raise ValidationError({"private_player": "PRIVATE_GM_PLAYER requires private_player."})
        if self.visibility != Visibility.PRIVATE_GM_PLAYER and self.private_player is not None:
            raise ValidationError({"private_player": "private_player must be set only for PRIVATE_GM_PLAYER."})
        if self.author_type == AuthorType.PLAYER and self.author_player is None:
            raise ValidationError({"author_player": "PLAYER author requires author_player."})