import json
import urllib.request

try:
    r = urllib.request.urlopen("http://host.docker.internal:11434/api/tags", timeout=5)
    d = json.load(r)
    names = [m["name"] for m in d.get("models", [])]
    print("OLLAMA_OK models:", names)
except Exception as e:
    print("OLLAMA_FAIL:", repr(e))