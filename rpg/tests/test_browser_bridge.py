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
    assert 'fetch(form.action' in source
    assert "new FormData(form)" in source
    assert "bridge-import-http-complete" in source
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

    assert manifest["version"] == "0.4.12"
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


def test_browser_bridge_guards_oversized_prompts_before_external_navigation():
    source = (BRIDGE / "mraz-page.js").read_text(encoding="utf-8")
    background = (BRIDGE / "background.js").read_text(encoding="utf-8")
    popup = (BRIDGE / "popup.js").read_text(encoding="utf-8")

    assert "MAX_BRIDGE_PROMPT_CHARS = 30000" in source
    assert "bridge-prompt-too-large" in source
    assert "too large for automatic browser insertion" in source
    assert "service-worker-started" in background
    assert "getManifest().version" in popup


def test_browser_bridge_validates_structured_response_before_import():
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")
    source = (BRIDGE / "mraz-page.js").read_text(encoding="utf-8")

    assert "structuredResponseStatus" in external
    assert "missing-gm-action" in external
    assert "missing-player-action" in external
    assert "response-contract-invalid" in external
    assert "waitForFreshResponse(adapter, beforeTexts, message.jobId)" in external
    assert "human-waiting" in source
    assert "human-submit" in source


def test_browser_bridge_pauses_failed_result_instead_of_autoretrying():
    background = (BRIDGE / "background.js").read_text(encoding="utf-8")
    source = (BRIDGE / "mraz-page.js").read_text(encoding="utf-8")

    assert 'job.state = "paused-result"' in background
    assert "job-paused-after-source-reject" in background
    assert "job-manual-retry" in background
    assert "manualRetry" in background
    assert "bridge-paused-after-external-error" in source
    assert "bridge-import-server-state" in source
    assert "sameExecutionStillWaiting" in source
    assert "Automatic retry is paused" in source


def test_browser_bridge_blocks_autoplay_for_paused_jobs():
    background = (BRIDGE / "background.js").read_text(encoding="utf-8")
    autoplay = background.split("async function tickAutoplay()", 1)[1]

    paused_guard = autoplay.index('job.state === "paused-result"')
    busy_guard = autoplay.index(
        "jobs.some((job) => job.sourceTabId === source.sourceTabId)",
        paused_guard,
    )

    assert paused_guard < busy_guard
    assert "autoplay-blocked-paused-job" in autoplay
    assert 'reason: "paused-result"' in autoplay


def test_browser_bridge_rejects_oversized_prompts_in_background():
    background = (BRIDGE / "background.js").read_text(encoding="utf-8")

    guard = background.index("promptLength > MAX_BRIDGE_PROMPT_LENGTH")
    tab_creation = background.index("chrome.tabs.create", guard)

    assert "MAX_BRIDGE_PROMPT_LENGTH = 30000" in background
    assert guard < tab_creation
    assert "job-rejected-prompt-too-large" in background
    assert 'failureReason: "prompt-too-large"' in background


def test_browser_bridge_preserves_machine_readable_failure_reasons():
    background = (BRIDGE / "background.js").read_text(encoding="utf-8")
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")
    source = (BRIDGE / "mraz-page.js").read_text(encoding="utf-8")

    assert "job.failureReason = delivery.failureReason" in background
    assert '"external-chat-timeout"' in external
    assert '"import-error"' in source
    assert '"source-tab-rejected-result"' in source


def test_browser_bridge_auto_repairs_invalid_structured_responses():
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")

    assert "structuredJsonCandidates" in external
    assert "response-auto-repair" in external
    assert "repair-send-clicked" in external
    assert "MRAZ_INVALID_STRUCTURED_RESPONSE" in external
    assert "preview: candidate.text.slice(0, 240)" in external


def test_manual_chat_player_delta_cards_autostart_bridge():
    players_template = (
        ROOT / "rpg" / "templates" / "rpg" / "_players.html"
    ).read_text(encoding="utf-8")

    assert 'data-mraz-bridge-kind="player"' in players_template
    assert 'data-mraz-bridge-autostart="1"' in players_template
    assert "not waiting.external_is_bootstrap" in players_template


def test_gemini_bridge_prefers_visible_final_response_body():
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")

    assert 'requireVisibleResponseBody: true' in external
    assert 'preferLastResponseBody: true' in external
    assert 'function elementIsVisible' in external
    assert '".model-response-text",' not in external.split('"gemini.google.com": {', 1)[1].split('};', 1)[0]
    assert '"response-contract-ready"' in external
    assert "preview: contract.normalized.slice(0, 240)" in external


def test_browser_bridge_autostarts_jobs_inserted_by_live_ui_updates():
    source = (BRIDGE / "mraz-page.js").read_text(encoding="utf-8")

    assert 'document.addEventListener("mraz:gm-panel-updated"' in source
    assert 'document.body.addEventListener("htmx:afterSwap"' in source
    assert "new MutationObserver" in source
    assert "scheduleAutoStartBridge" in source
    assert '"autostart-after-dom-update"' in source


def test_browser_bridge_requires_full_scene_state_on_gm_transition():
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")

    assert '"invalid-scene-transition-state"' in external
    assert "transition.description" in external
    assert "transition.memory" in external
    assert '"description":"current state"' in external


def test_browser_bridge_does_not_autostart_paused_cards():
    source = (BRIDGE / "mraz-page.js").read_text(encoding="utf-8")
    autostart = source.split("function autoStartBridgeIfPresent()", 1)[1]

    assert 'card.dataset.mrazBridgePaused === "1"' in autostart
    assert 'result.state === "paused-result"' in source


def test_chatgpt_bridge_uses_current_submit_button_and_ready_candidate():
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")
    chatgpt = external.split('"chatgpt.com": {', 1)[1].split('"claude.ai": {', 1)[0]

    assert '"#composer-submit-button"' in chatgpt
    assert 'button[data-testid*="send-button"]' in chatgpt
    assert 'button[type="submit"]' in chatgpt
    assert "button.composer-submit-btn" in chatgpt
    assert 'button[aria-label="Отправить"]' in chatgpt
    assert "root.querySelectorAll(selector)" in external
    assert "sendButtonIsReady" in external
    assert "sendButtonDiagnostics" in external
    assert "MRAZ_SEND_BUTTON_UNAVAILABLE" in external


def test_chatgpt_bridge_reuses_custom_gpt_redirect_for_same_conversation():
    background = (BRIDGE / "background.js").read_text(encoding="utf-8")

    assert "function chatgptConversationId(url)" in background
    assert 'parsed.pathname.match(/\\/c\\/([^/?#]+)/)' in background
    assert (
        'chatgptConversationId(tab.url || "") === desiredConversationId'
        in background
    )


def test_chatgpt_bridge_updates_prosemirror_state_before_sending():
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")

    assert "function selectComposerContents(element)" in external
    assert 'document.execCommand("insertText", false, prompt)' in external
    assert "function pasteIntoComposer(element, prompt)" in external
    assert 'dispatchComposerInput(element, prompt, "insertFromPaste")' in external
    assert "composed: true" in external
    assert "fillMode," in external
    assert "documentFocused: document.hasFocus()" in external


def test_chatgpt_bridge_scopes_submit_button_to_composer_form():
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")

    assert 'composer.closest("form")' in external
    assert "sendButtonRoot(composer)" in external
    assert "waitForSendButton(adapter, composer)" in external
    assert "waitForSendButton(adapter, repairComposer)" in external
    assert 'root.querySelectorAll("button")' in external


def test_chatgpt_bridge_detects_current_assistant_turn_shells():
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")
    chatgpt = external.split('"chatgpt.com": {', 1)[1].split('"claude.ai": {', 1)[0]

    assert (
        '[data-testid^="conversation-turn-"][data-turn="assistant"]'
        in chatgpt
    )
    assert 'article[data-turn="assistant"]' in chatgpt
    assert 'section[data-turn="assistant"]' in chatgpt
    assert 'button[aria-label*="Остановить"]' in chatgpt
    assert 'trace("response-wait-status"' in external
    assert "turnShellCount" in external


def test_chatgpt_bridge_detects_hash_scoped_markdown_responses():
    external = (BRIDGE / "external-chat.js").read_text(encoding="utf-8")
    chatgpt = external.split('"chatgpt.com": {', 1)[1].split('"claude.ai": {', 1)[0]

    assert '[class^="MarkdownRoot-"]' in chatgpt
    assert '[class*=" MarkdownRoot-"]' in chatgpt
