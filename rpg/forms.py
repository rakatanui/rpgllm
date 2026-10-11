from pathlib import Path
from io import BytesIO

from django.core.files.base import ContentFile
from PIL import Image, UnidentifiedImageError

from django import forms
from django.contrib.auth.forms import AuthenticationForm


MAX_CHARACTER_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_CHARACTER_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_CHARACTER_IMAGE_PIXELS = 12_000_000


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
        # Re-encode after decoding, removing EXIF, embedded metadata and
        # unwanted extra bytes. Never store the original uploaded payload.
        try:
            image.seek(0)
            with Image.open(image) as original:
                if original.width * original.height > MAX_CHARACTER_IMAGE_PIXELS:
                    raise forms.ValidationError("Image dimensions exceed 12 megapixels.")
                format_name = original.format
                original.load()
                if format_name == "JPEG":
                    decoded = original.convert("RGB")
                    output_extension, output_type = ".jpg", "image/jpeg"
                elif format_name == "PNG":
                    decoded = original.convert(
                        "RGBA" if "A" in original.getbands() or "transparency" in original.info else "RGB"
                    )
                    output_extension, output_type = ".png", "image/png"
                else:
                    decoded = original.convert(
                        "RGBA" if "A" in original.getbands() else "RGB"
                    )
                    output_extension, output_type = ".webp", "image/webp"
                sanitized = BytesIO()
                kwargs = {"quality": 90} if format_name in {"JPEG", "WEBP"} else {}
                decoded.save(sanitized, format=format_name, **kwargs)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise forms.ValidationError("Cannot safely process this image.") from exc
        if sanitized.tell() > MAX_CHARACTER_IMAGE_BYTES:
            raise forms.ValidationError("Processed character image exceeds 5 MB.")
        return ContentFile(
            sanitized.getvalue(), name="portrait" + output_extension
        )


class GameMasterAuthenticationForm(AuthenticationForm):
    """Only active staff accounts may sign into the GM workbench."""

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise forms.ValidationError(
                "This account is not authorized to access the Game Master workbench.",
                code="not_game_master",
            )
