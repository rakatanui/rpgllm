import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mraz.settings')
os.environ['LLM_BACKEND'] = 'litellm'

import django
django.setup()

from rpg.services.llm import reset_llm_client, LiteLLMClient
from rpg.models import Scene, Player
from rpg.services.context_builder import build_player_context
import asyncio

reset_llm_client()
s = Scene.objects.get(name='Test scene')
mila = Player.objects.get(display_name='Mila')
ctx = build_player_context(player=mila, scene=s, trigger_message=None)
client = LiteLLMClient()
model = mila.model_config.gateway_model
temp = mila.model_config.temperature

async def go():
    resp = await client.generate(system_prompt=ctx.system_prompt, messages=ctx.messages, model=model, temperature=temp)
    return resp
resp = asyncio.new_event_loop().run_until_complete(go())
print('action_type=', resp.action_type)
print('public=', repr(resp.public)[:300])
print('private_to_gm=', repr(resp.private_to_gm)[:200])
reset_llm_client()