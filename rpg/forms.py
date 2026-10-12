from pathlib import Path

from django import forms
from django.contrib.auth.forms import AuthenticationForm


MAX_CHARACTER_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_CHARACTER_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}


class CharacterImageUploadForm(forms.Form):
    image = forms.ImageField()

    def clean_image(self):
        image = self.cleaned_data["image"]
        if image.size > MAX_CHARACTER_IMAGE_BYTES:
            raise forms.ValidationError("Character image must be 5 MB or smaller.")

        image_format = getattr(getattr(image, "image", None), "format", None)
        extension = Path(image.name or "").suffix.lower()
        if (
            image_format not in ALLOWED_CHARACTER_IMAGE_FORMATS
            or extension not in {".jpg", ".jpeg", ".png", ".webp"}
        ):
            raise forms.ValidationError("Use a JPEG, PNG, or WebP image.")
        return image


class GameMasterAuthenticationForm(AuthenticationForm):
    """Only active staff accounts may sign into the GM workbench."""

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise forms.ValidationError(
                "This account is not authorized to access the Game Master workbench.",
                code="not_game_master",
            )
