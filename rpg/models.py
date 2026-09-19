"""Core data models for MRAZ Master."""
from pathlib import Path
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


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


class LoreScope(models.TextChoices):
    GLOBAL = "GLOBAL", "Global"
    SCENE = "SCENE", "Scene-specific"
    PLAYER = "PLAYER", "Player-specific"


class ExecutionState(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    WAITING_EXTERNAL = "WAITING_EXTERNAL", "Waiting for external chat"
    WAITING_HUMAN = "WAITING_HUMAN", "Waiting for human player"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"
    INVALID = "INVALID", "Invalid response"


class PlayerStatus(models.TextChoices):
    IDLE = "idle", "Idle"
    GENERATING = "generating", "Generating"
    WAITING_EXTERNAL = "waiting_external", "Waiting for external chat"
    WAITING_HUMAN = "waiting_human", "Waiting for human player"
    ERROR = "error", "Error"


class PlayerTransport(models.TextChoices):
    LITELLM = "LITELLM", "LiteLLM / API"
    MANUAL_CHAT = "MANUAL_CHAT", "Manual external chat"
    HUMAN = "HUMAN", "Human player"


class ManualChatContextMode(models.TextChoices):
    FULL = "FULL", "Full prompt every turn"
    CHAT_MEMORY = "CHAT_MEMORY", "Use external chat memory after bootstrap"


class GameMasterTransport(models.TextChoices):
    LITELLM = "LITELLM", "LiteLLM / API"
    MANUAL_CHAT = "MANUAL_CHAT", "Manual external chat"


class GameMasterExecutionState(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    WAITING_EXTERNAL = "WAITING_EXTERNAL", "Waiting for external chat"
    DRAFT = "DRAFT", "Draft ready"
    PUBLISHED = "PUBLISHED", "Published"
    FAILED = "FAILED", "Failed"
    INVALID = "INVALID", "Invalid response"
    DISCARDED = "DISCARDED", "Discarded"


class GameMasterAction(models.TextChoices):
    TURN = "TURN", "Publish and run player turn"
    NARRATE = "NARRATE", "Publish without player turn"
    WAIT = "WAIT", "Do nothing"


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
    shared_memory = models.TextField(
        blank=True,
        help_text=(
            "Compact shared campaign memory: established public facts and important "
            "events that should survive history trimming."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class GameMasterConfig(models.Model):
    campaign = models.OneToOneField(
        Campaign,
        on_delete=models.CASCADE,
        related_name="gm_config",
    )
    enabled = models.BooleanField(default=False)
    transport = models.CharField(
        max_length=30,
        choices=GameMasterTransport.choices,
        default=GameMasterTransport.LITELLM,
    )
    model_config = models.ForeignKey(
        ModelConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="gm_configs",
    )
    fallback_model_config = models.ForeignKey(
        ModelConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="gm_fallback_configs",
    )
    system_prompt = models.TextField(
        blank=True,
        help_text=(
            "Authoritative instructions for the model acting as Game Master. "
            "Campaign, scene, lore, characters and all GM-visible history are added separately."
        ),
    )
    review_before_publish = models.BooleanField(
        default=True,
        help_text=(
            "When enabled, model output becomes an editable draft. When disabled, "
            "a valid result is published immediately."
        ),
    )
    manual_chat_label = models.CharField(max_length=120, blank=True)
    manual_chat_url = models.URLField(max_length=1000, blank=True)
    manual_chat_context_mode = models.CharField(
        max_length=30,
        choices=ManualChatContextMode.choices,
        default=ManualChatContextMode.FULL,
    )
    manual_chat_initialized = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"GM / {self.campaign.name}"


class Scene(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="scenes")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    dialogue_language = models.CharField(
        max_length=80,
        blank=True,
        help_text=(
            "Default diegetic language for spoken dialogue in this scene, "
            "for example French, Portuguese, Russian. Direct speech is emitted "
            "in this language with a Russian hover translation."
        ),
    )
    participants = models.ManyToManyField(
        "Player",
        through="SceneParticipant",
        related_name="scenes",
        blank=True,
        help_text="Players who are actually present in / can act in this scene.",
    )
    previous_scenes = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="next_scenes",
        help_text=(
            "Closed earlier scenes whose visible history is inherited as backstory. "
            "A scene may have multiple predecessors when parallel threads converge."
        ),
    )
    memory_summary = models.TextField(
        blank=True,
        help_text=(
            "Compact summary of older events/state for this scene. It is always "
            "included in player context even when old messages are trimmed."
        ),
    )
    close_summary_draft = models.JSONField(
        default=dict,
        blank=True,
        help_text="GM-reviewed draft produced before closing a scene.",
    )
    mode = models.CharField(max_length=20, choices=TurnMode.choices, default=TurnMode.ROUND)
    round_order = models.JSONField(
        default=list,
        blank=True,
        help_text="Ordered list of Player IDs defining the round order.",
    )
    active_player_index = models.IntegerField(default=0)
    is_closed = models.BooleanField(
        default=False,
        help_text="Closed scenes are read-only and may only be used as history for later scenes.",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.campaign.name} / {self.name}"

    def clean(self):
        super().clean()
        order = list(self.round_order or [])
        if not order:
            if self.active_player_index != 0:
                raise ValidationError(
                    {"active_player_index": "Must be 0 when round_order is empty."}
                )
            return
        if any(not isinstance(player_id, int) for player_id in order):
            raise ValidationError(
                {"round_order": "All round_order entries must be integer Player IDs."}
            )
        if len(order) != len(set(order)):
            raise ValidationError({"round_order": "Duplicate Player IDs are not allowed."})
        if not (0 <= self.active_player_index < len(order)):
            raise ValidationError({"active_player_index": "Out of bounds for round_order."})
        if self.campaign_id:
            valid_ids = set(
                Player.objects.filter(campaign_id=self.campaign_id, pk__in=order)
                .values_list("pk", flat=True)
            )
            invalid = [player_id for player_id in order if player_id not in valid_ids]
            if invalid:
                raise ValidationError(
                    {"round_order": f"Invalid or foreign Player IDs: {invalid}"}
                )
        if self.pk:
            participant_ids = set(
                self.scene_participants.values_list("player_id", flat=True)
            )
            if order and set(order) != participant_ids:
                raise ValidationError(
                    {
                        "round_order": (
                            "ROUND order must contain every scene participant exactly once."
                        )
                    }
                )


def character_image_upload_to(instance, filename):
    ext = Path(filename or "").suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        ext = ".img"
    return f"characters/player_{instance.pk}/{uuid.uuid4().hex}{ext}"


def character_appearance_upload_to(instance, filename):
    ext = Path(filename or "").suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        ext = ".img"
    appearance_id = instance.pk or "new"
    return (
        f"characters/player_{instance.player_id}/appearance_{appearance_id}/"
        f"{uuid.uuid4().hex}{ext}"
    )


class Player(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="players")
    display_name = models.CharField(max_length=120)
    character_prompt = models.TextField(
        blank=True,
        help_text="Character description / system prompt for this player's LLM.",
    )
    character_image = models.ImageField(
        upload_to=character_image_upload_to,
        blank=True,
        help_text="Portrait shown on the human-player character card.",
    )
    character_summary = models.TextField(
        blank=True,
        help_text="Player-facing short character description / identity summary.",
    )
    characteristics = models.TextField(
        blank=True,
        help_text="Player-facing characteristics, stats, traits, or other sheet data.",
    )
    abilities = models.TextField(
        blank=True,
        help_text="Player-facing abilities, powers, skills, spells, or special rules.",
    )
    memory_summary = models.TextField(
        blank=True,
        help_text=(
            "Compact long-term memory known only to this player: secrets, promises, "
            "relationships, intentions and older important events."
        ),
    )
    model_config = models.ForeignKey(
        ModelConfig, on_delete=models.SET_NULL, null=True, blank=True, related_name="players"
    )
    fallback_model_config = models.ForeignKey(
        ModelConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fallback_players",
        help_text="Optional model used for one-off fallback retries.",
    )
    pending_nudge = models.TextField(
        blank=True,
        help_text="One-shot private GM instruction consumed by the player's next execution.",
    )
    transport = models.CharField(
        max_length=30,
        choices=PlayerTransport.choices,
        default=PlayerTransport.LITELLM,
    )
    manual_chat_label = models.CharField(
        max_length=120,
        blank=True,
        help_text="Human label shown in the GM UI, e.g. ChatGPT 5.6 or Claude.",
    )
    manual_chat_url = models.URLField(
        max_length=1000,
        blank=True,
        help_text="Optional URL of the persistent external chat used for this player.",
    )
    manual_chat_context_mode = models.CharField(
        max_length=30,
        choices=ManualChatContextMode.choices,
        default=ManualChatContextMode.FULL,
        help_text=(
            "FULL sends complete application context every turn. CHAT_MEMORY sends one "
            "full bootstrap, then only newly visible context and current turn constraints."
        ),
    )
    manual_chat_initialized = models.BooleanField(
        default=False,
        help_text=(
            "True after a successful bootstrap response was imported for CHAT_MEMORY. "
            "Reset this if the external conversation is replaced or loses its memory."
        ),
    )
    status = models.CharField(max_length=30, choices=PlayerStatus.choices, default=PlayerStatus.IDLE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        unique_together = [("campaign", "display_name")]

    def __str__(self):
        return self.display_name


class CharacterAppearance(models.Model):
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name="appearances",
    )
    name = models.CharField(max_length=120)
    description = models.TextField(
        blank=True,
        help_text=(
            "Player-facing description of this form. It is also included in the "
            "model context when this is the current appearance."
        ),
    )
    portrait_image = models.ImageField(
        upload_to=character_appearance_upload_to,
        blank=True,
        help_text="Square/cropped portrait for compact character cards.",
    )
    fullbody_image = models.ImageField(
        upload_to=character_appearance_upload_to,
        blank=True,
        help_text="Full-body/reference image shown without cropping.",
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="Fallback appearance when a scene has no explicit current form.",
    )
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["player", "name"],
                name="uniq_character_appearance_name_per_player",
            ),
            models.UniqueConstraint(
                fields=["player"],
                condition=Q(is_primary=True),
                name="uniq_primary_character_appearance_per_player",
            ),
        ]

    def __str__(self):
        return f"{self.player} / {self.name}"


class SceneParticipant(models.Model):
    scene = models.ForeignKey(
        Scene,
        on_delete=models.CASCADE,
        related_name="scene_participants",
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name="scene_participations",
    )
    order = models.PositiveSmallIntegerField(default=0)
    current_appearance = models.ForeignKey(
        CharacterAppearance,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scene_participations",
        help_text=(
            "Appearance currently used by this character in this scene. "
            "Leave empty to use the primary appearance."
        ),
    )

    class Meta:
        ordering = ["order", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["scene", "player"],
                name="uniq_scene_participant",
            )
        ]

    def __str__(self):
        return f"{self.scene} / {self.player}"

    def clean(self):
        super().clean()
        if (
            self.scene_id
            and self.player_id
            and self.scene.campaign_id != self.player.campaign_id
        ):
            raise ValidationError(
                {"player": "Scene participant must belong to the same campaign."}
            )
        if (
            self.current_appearance_id
            and self.player_id
            and self.current_appearance.player_id != self.player_id
        ):
            raise ValidationError(
                {"current_appearance": "Current appearance must belong to this player."}
            )


class LoreEntry(models.Model):
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="lore_entries",
    )
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=80, blank=True)
    content = models.TextField()
    scope = models.CharField(
        max_length=20,
        choices=LoreScope.choices,
        default=LoreScope.GLOBAL,
        help_text=(
            "GLOBAL: all campaign players. SCENE: only assigned scenes. "
            "PLAYER: only assigned players."
        ),
    )
    scenes = models.ManyToManyField(
        Scene,
        blank=True,
        related_name="lore_entries",
        help_text="Used when scope is SCENE.",
    )
    players = models.ManyToManyField(
        Player,
        blank=True,
        related_name="lore_entries",
        help_text="Used when scope is PLAYER.",
    )
    priority = models.PositiveSmallIntegerField(
        default=100,
        help_text="Lower values are included first when the lore context budget is full.",
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "title", "pk"]

    def __str__(self):
        return self.title


class Turn(models.Model):
    scene = models.ForeignKey(Scene, on_delete=models.CASCADE, related_name="turns")
    mode = models.CharField(max_length=20, choices=TurnMode.choices)
    state = models.CharField(max_length=20, choices=TurnState.choices, default=TurnState.PENDING)
    participants = models.JSONField(default=list, blank=True)
    trigger_message = models.ForeignKey(
        "Message",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_turns",
    )
    client_turn_id = models.UUIDField(null=True, blank=True)
    is_private = models.BooleanField(default=False)
    active_player_id_snapshot = models.BigIntegerField(null=True, blank=True)
    round_advanced = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["scene", "client_turn_id"],
                condition=Q(client_turn_id__isnull=False),
                name="uniq_scene_client_turn_id",
            )
        ]

    def __str__(self):
        return f"Turn #{self.pk} ({self.mode}/{self.state})"


class TurnExecution(models.Model):
    turn = models.ForeignKey(Turn, on_delete=models.CASCADE, related_name="executions")
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="turn_executions")
    order_index = models.PositiveIntegerField(default=0)
    state = models.CharField(
        max_length=20, choices=ExecutionState.choices, default=ExecutionState.PENDING
    )
    action_type = models.CharField(max_length=30, blank=True, default="")
    error = models.TextField(blank=True)
    history_message_ids = models.JSONField(default=list, blank=True)
    nudge_text = models.TextField(blank=True)
    model_used = models.CharField(max_length=200, blank=True, default="")
    system_prompt_snapshot = models.TextField(blank=True)
    request_messages = models.JSONField(default=list, blank=True)
    raw_response = models.TextField(blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    transport = models.CharField(
        max_length=30,
        choices=PlayerTransport.choices,
        default=PlayerTransport.LITELLM,
    )
    external_prompt = models.TextField(blank=True)
    external_context_mode = models.CharField(
        max_length=30,
        choices=ManualChatContextMode.choices,
        blank=True,
        default="",
    )
    external_chat_label = models.CharField(max_length=120, blank=True, default="")
    external_chat_url = models.URLField(max_length=1000, blank=True, default="")
    external_is_bootstrap = models.BooleanField(default=False)
    external_synced_message_ids = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order_index", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["turn", "player"],
                name="uniq_turn_execution_player",
            )
        ]

    def __str__(self):
        return f"Turn #{self.turn_id} / {self.player} ({self.state})"


class GameMasterExecution(models.Model):
    scene = models.ForeignKey(
        Scene,
        on_delete=models.CASCADE,
        related_name="gm_executions",
    )
    config = models.ForeignKey(
        GameMasterConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="executions",
    )
    state = models.CharField(
        max_length=30,
        choices=GameMasterExecutionState.choices,
        default=GameMasterExecutionState.PENDING,
    )
    transport = models.CharField(
        max_length=30,
        choices=GameMasterTransport.choices,
        default=GameMasterTransport.LITELLM,
    )
    action = models.CharField(
        max_length=20,
        choices=GameMasterAction.choices,
        blank=True,
        default="",
    )
    public_draft = models.TextField(blank=True)
    private_drafts = models.JSONField(default=list, blank=True)
    turn_targets = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    context_message_ids = models.JSONField(default=list, blank=True)
    model_used = models.CharField(max_length=200, blank=True, default="")
    system_prompt_snapshot = models.TextField(blank=True)
    request_messages = models.JSONField(default=list, blank=True)
    raw_response = models.TextField(blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    external_prompt = models.TextField(blank=True)
    external_context_mode = models.CharField(
        max_length=30,
        choices=ManualChatContextMode.choices,
        blank=True,
        default="",
    )
    external_chat_label = models.CharField(max_length=120, blank=True, default="")
    external_chat_url = models.URLField(max_length=1000, blank=True, default="")
    external_is_bootstrap = models.BooleanField(default=False)
    external_synced_message_ids = models.JSONField(default=list, blank=True)
    published_turn = models.ForeignKey(
        Turn,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="gm_executions",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"GM execution #{self.pk} / {self.scene} / {self.state}"


class MessageRevision(models.Model):
    message = models.ForeignKey(
        "Message",
        on_delete=models.CASCADE,
        related_name="revisions",
    )
    revision_index = models.PositiveIntegerField()
    content = models.TextField()
    action_type = models.CharField(max_length=30, blank=True, default="")
    reason = models.CharField(max_length=30, default="ORIGINAL")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["revision_index", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["message", "revision_index"],
                name="uniq_message_revision_index",
            )
        ]

    def __str__(self):
        return f"Message #{self.message_id} revision {self.revision_index}"


class Message(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="messages")
    scene = models.ForeignKey(
        Scene, on_delete=models.CASCADE, related_name="messages", null=True, blank=True
    )
    turn = models.ForeignKey(
        Turn, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages"
    )
    execution = models.ForeignKey(
        TurnExecution,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages",
    )
    author_type = models.CharField(max_length=20, choices=AuthorType.choices)
    author_player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages"
    )
    content = models.TextField()
    visibility = models.CharField(
        max_length=20, choices=Visibility.choices, default=Visibility.PUBLIC
    )
    private_player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, blank=True, related_name="private_messages_for"
    )
    action_type = models.CharField(max_length=30, blank=True, default="")
    gm_unread = models.BooleanField(
        default=False,
        help_text="True for a player private message the GM has not opened yet.",
    )
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

    def save(self, *args, **kwargs):
        if self.scene_id and Scene.objects.filter(pk=self.scene_id, is_closed=True).exists():
            raise ValidationError("Cannot write messages to a closed scene.")
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.scene_id and self.campaign_id and self.scene.campaign_id != self.campaign_id:
            raise ValidationError({"scene": "Scene must belong to the message campaign."})
        if self.author_player_id and self.author_player.campaign_id != self.campaign_id:
            raise ValidationError(
                {"author_player": "Author player must belong to the message campaign."}
            )
        if self.private_player_id and self.private_player.campaign_id != self.campaign_id:
            raise ValidationError(
                {"private_player": "Private player must belong to the message campaign."}
            )
        if self.visibility == Visibility.PRIVATE_GM_PLAYER and self.private_player is None:
            raise ValidationError({"private_player": "PRIVATE_GM_PLAYER requires private_player."})
        if self.visibility != Visibility.PRIVATE_GM_PLAYER and self.private_player is not None:
            raise ValidationError(
                {"private_player": "private_player must be set only for PRIVATE_GM_PLAYER."}
            )
        if self.author_type == AuthorType.PLAYER and self.author_player is None:
            raise ValidationError({"author_player": "PLAYER author requires author_player."})
