from django import forms


MAX_CHARACTER_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_CHARACTER_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}


class CharacterImageUploadForm(forms.Form):
    image = forms.ImageField()

    def clean_image(self):
        image = self.cleaned_data["image"]
        if image.size > MAX_CHARACTER_IMAGE_BYTES:
            raise forms.ValidationError("Character image must be 5 MB or smaller.")

        image_format = getattr(getattr(image, "image", None), "format", None)
        if image_format not in ALLOWED_CHARACTER_IMAGE_FORMATS:
            raise forms.ValidationError("Use a JPEG, PNG, or WebP image.")
        return image
