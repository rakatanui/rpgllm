# MRAZ Browser Bridge

A small Manifest V3 extension for Chromium-family browsers. It automates the
existing MANUAL_CHAT workflow without changing MRAZ's parser or Turn Engine:

1. MRAZ prepares the normal external prompt.
2. The extension opens or focuses the configured persistent web-chat URL.
3. It fills the chat composer and sends the prompt.
4. It waits until the newest assistant response stops changing.
5. It returns that raw response to the original MRAZ scene tab.
6. The existing same-origin MRAZ form submits the response through the normal
   validation/import path.

The extension never talks directly to PostgreSQL and never bypasses Django CSRF.
Manual Copy/Open/Paste controls remain available as a fallback.

## Supported web chats

Current adapters:

- ChatGPT: `https://chatgpt.com/...`
- Claude: `https://claude.ai/...`
- Gemini: `https://gemini.google.com/...`
- DeepSeek: `https://chat.deepseek.com/a/chat/s/...`

Use a persistent conversation URL in the Player/GameMaster manual-chat config.
A generic "new chat" URL defeats CHAT_MEMORY semantics because the bridge cannot
know which conversation should be reused.

Web-chat DOMs are not stable APIs. Each provider adapter is isolated in
`external-chat.js`; if a provider changes its composer/send/message markup, the
bridge reports an error and the normal Copy/Open/Paste path still works.

## Install in Edge or Chrome

No build step is required.

1. Pull the repository.
2. Open the browser extension manager:
   - Edge: `edge://extensions/`
   - Chrome: `chrome://extensions/`
3. Enable **Developer mode**.
4. Choose **Load unpacked**.
5. Select the repository's `browser_bridge` directory.
6. Reload the MRAZ scene page.

When the content script is active, waiting MANUAL_CHAT cards gain a
**Send via browser bridge** button. Without the extension, that button stays
hidden.

## Runtime behavior

Pressing **Send via browser bridge** focuses the configured external chat tab.
The source MRAZ tab may keep polling or even reload while the model is thinking;
the bridge job is keyed by execution id and stored in
`chrome.storage.session`, so it can reconnect to the refreshed card.

A job times out after ten minutes while waiting for an assistant response.
Stale jobs are discarded after thirty minutes.

By default the bridge still requires an explicit click per execution.

When a campaign's GameMasterConfig has **auto_continue** enabled, the GM scene
page registers itself as an autoplay source. After a HUMAN player submits a
public move, Django creates the next model-GM execution automatically. The
bridge detects the waiting GM execution, reloads the local source tab, sends the
prompt to the configured persistent web chat, imports the response, and repeats
after the next HUMAN move.

For unattended phone play, keep these open on the GM machine:

- the local MRAZ scene tab (`http://mraz.local/scene/.../`);
- the configured persistent ChatGPT / Claude / Gemini / DeepSeek conversation.

The extension uses the active external-chat tab as a heartbeat so Chromium's
background-tab throttling does not normally stall the loop. The machine and
browser must stay awake. `review_before_publish` must be disabled when
`auto_continue` is enabled, otherwise every GM response would stop at a draft.

## Permissions

The extension requests tab/storage access plus host permissions only for:

- `mraz.local`, `127.0.0.100`, and `localhost`
- `chatgpt.com`
- `claude.ai`
- `gemini.google.com`
- `chat.deepseek.com`

It does not request `<all_urls>`.

If the local GM UI moves to a different hostname, add that hostname to both
`host_permissions` and the MRAZ content-script `matches` in `manifest.json`.
