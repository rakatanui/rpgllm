import urllib.request

req = urllib.request.Request("http://localhost:4000/v1/chat/completions", method="POST")
try:
    urllib.request.urlopen(req, timeout=10)
except urllib.error.HTTPError as e:
    print("status:", e.code)
    print(e.read().decode()[:800])