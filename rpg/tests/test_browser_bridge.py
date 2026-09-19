"""Static contract checks for the unpacked Chromium browser bridge."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "browser_bridge"


def test_browser_bridge_manifest_is_loadable_and_scoped():
    manifest = json.loads((BRIDGE / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert manifest["background"]["service_worker"] == "background.js"
    assert "tabs" in manifest["permissions"]
    assert "storage" in manifest["permissions"]
    assert "<all_urls>" not in manifest["host_permissions"]
    assert "https://chatgpt.com/*" in manifest["host_permissions"]
    assert "https://claude.ai/*" in manifest["host_permissions"]
    assert "https://gemini.google.com/*" in manifest["host_permissions"]


def test_browser_bridge_files_required_by_manifest_exist():
    manifest = json.loads((BRIDGE / "manifest.json").read_text(encoding="utf-8"))
    required = {manifest["background"]["service_worker"]}
    for entry in manifest["content_scripts"]:
        required.update(entry["js"])

    assert required == {"background.js", "mraz-page.js", "external-chat.js"}
    for relative in required:
        assert (BRIDGE / relative).is_file()


def test_browser_bridge_keeps_results_until_source_acknowledges_import():
    background = (BRIDGE / "background.js").read_text(encoding="utf-8")
    source = (BRIDGE / "mraz-page.js").read_text(encoding="utf-8")

    assert "chrome.storage.session" in background
    assert 'job.state = "result-ready"' in background
    assert "MRAZ_SOURCE_READY" in background
    assert "response && response.accepted" in background
    assert "form.requestSubmit()" in source
    assert "data-mraz-bridge-card" in source
