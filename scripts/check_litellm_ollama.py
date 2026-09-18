import json
import urllib.request

body = json.dumps({
    "model": "ollama-local",
    "messages": [{"role": "user", "content": "Reply with a single short word: hello"}],
    "max_tokens": 20,
}).encode()

req = urllib.request.Request(
    "http://localhost:4000/v1/chat/completions",
    data=body,
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer sk-litellm-default",
    },
)
try:
    r = urllib.request.urlopen(req, timeout=60)
    d = json.load(r)
    print("LITELLM_OK")
    print("content:", d["choices"][0]["message"]["content"][:200])
except Exception as e:
    print("LITELLM_FAIL:", repr(e))