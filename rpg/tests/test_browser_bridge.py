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
    assert "https://chat.deepseek.com/*" in manifest["host_permissions"]

    external_matches = next(
        item["matches"]
        for item in manifest["content_scripts"]
        if "external-chat.js" in item["js"]
    )
    assert "https://chat.deepseek.com/*" in external_matches


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



def test_browser_bridge_contains_deepseek_adapter():
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")
    background = (BRIDGE / "background.js").read_text(encoding="utf-8")

    assert '"chat.deepseek.com": {' in external
    assert "textarea[placeholder='Message DeepSeek']" in external
    assert "div.ds-message:has(.ds-markdown)" in external
    assert "completionStablePolls: 7" in external
    assert "preferLastResponseBody: true" in external
    assert '"chat.deepseek.com"' in background


def test_browser_bridge_recovers_stale_tabs_after_extension_reload():
    background = (BRIDGE / "background.js").read_text(encoding="utf-8")
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")
    source = (BRIDGE / "mraz-page.js").read_text(encoding="utf-8")

    assert "reusedExistingTab" in background
    assert "chrome.tabs.reload(target.id)" in background
    assert "safeRuntimeMessage" in external
    assert "MRAZ_EXTERNAL_READY" in external
    assert "safeRuntimeMessage" in source
    assert "MRAZ_SOURCE_READY" in source


def test_browser_bridge_exposes_persistent_debug_log_popup():
    manifest = json.loads((BRIDGE / "manifest.json").read_text(encoding="utf-8"))
    background = (BRIDGE / "background.js").read_text(encoding="utf-8")
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")
    source = (BRIDGE / "mraz-page.js").read_text(encoding="utf-8")

    assert manifest["version"] == "0.4.0"
    assert manifest["action"]["default_popup"] == "popup.html"
    assert (BRIDGE / "popup.html").is_file()
    assert (BRIDGE / "popup.js").is_file()
    assert "mraz-bridge-debug-log" in background
    assert "MRAZ_DEBUG_GET" in background
    assert "MRAZ_DEBUG_CLEAR" in background
    assert "MRAZ_DEBUG_LOG" in external
    assert "MRAZ_DEBUG_LOG" in source
    assert "job-send-attempt" in background
    assert "bridge-result-received" in source
    assert "response-detected" in external
