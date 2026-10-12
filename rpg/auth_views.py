"""Authentication endpoints for the Game Master workbench."""

from django.contrib.auth.views import LoginView

from rpg.forms import GameMasterAuthenticationForm


class GameMasterLoginView(LoginView):
    template_name = "rpg/login.html"
    authentication_form = GameMasterAuthenticationForm
