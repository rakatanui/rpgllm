const activeJobs = new Set();

const ADAPTERS = {
  "chatgpt.com": {
    name: "ChatGPT",
    composer: [
      "#prompt-textarea",
      '[contenteditable="true"][data-virtualkeyboard="true"]',
      'div[contenteditable="true"]',
    ],
    send: [
      'button[data-testid="send-button"]',
      'button[aria-label="Send prompt"]',
      'button[aria-label*="Send"]',
    ],
    assistant: [
      '[data-message-author-role="assistant"]',
    ],
    responseBody: [
      ".markdown",
      '[class*="markdown"]',
      '[class*="prose"]',
    ],
    busy: [
      'button[data-testid="stop-button"]',
      'button[aria-label*="Stop generating"]',
      'button[aria-label*="Stop"]',
    ],
  },
  "claude.ai": {
    name: "Claude",
    composer: [
      'div[contenteditable="true"].ProseMirror',
      'div[contenteditable="true"][data-testid*="chat"]',
      'div[contenteditable="true"]',
    ],
    send: [
      'button[aria-label*="Send"]',
      'button[data-testid*="send"]',
    ],
    assistant: [
      '[data-testid="assistant-message"]',
      '.font-claude-message',
      '[data-is-streaming] .font-claude-message',
    ],
    responseBody: [
      ".font-claude-message",
      ".prose",
      '[class*="prose"]',
    ],
    busy: [
      '[data-is-streaming="true"]',
      'button[aria-label*="Stop"]',
      'button[data-testid*="stop"]',
    ],
  },
  "chat.deepseek.com": {
    name: "DeepSeek",
    composer: [
      "textarea[placeholder='Message DeepSeek']",
      "textarea",
    ],
    send: [
      "[role='button'].ds-button._52c986b",
      ".ds-button._52c986b.ds-button--circle",
      "div.ds-icon-button._52c986b",
      ".ds-button.ds-button--circle",
      "[role='button'].ds-button",
      "div.ds-icon-button[role='button']",
    ],
    assistant: [
      "div.ds-message:has(.ds-markdown)",
      ".ds-message:has(.ds-markdown)",
    ],
    responseBody: [
      ".ds-markdown",
      '[class*="markdown"]',
    ],
    busy: [
      '[aria-label*="Stop"]',
      '[title*="Stop"]',
    ],
    completionStablePolls: 7,
    fallbackStablePolls: 10,
    preferLastResponseBody: true,
  },
  "gemini.google.com": {
    name: "Gemini",
    composer: [
      'rich-textarea [contenteditable="true"]',
      '.ql-editor[contenteditable="true"]',
      'div[contenteditable="true"]',
    ],
    send: [
      'button[aria-label*="Send message"]',
      'button[aria-label*="Send"]',
      '.send-button button',
      'button.send-button',
    ],
    assistant: [
      "model-response",
      '[data-test-id="model-response"]',
      ".model-response-text",
    ],
    responseBody: [
      ".model-response-text",
      ".markdown",
      '[class*="response-content"]',
    ],
    busy: [
      'button[aria-label*="Stop"]',
      ".stop-button",
      '[data-test-id*="stop"]',
    ],
  },
};

function adapterForPage() {
  return ADAPTERS[window.location.hostname.toLowerCase()] || null;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function safeRuntimeMessage(message) {
  try {
    if (!chrome.runtime || !chrome.runtime.id) {
      return Promise.resolve(null);
    }
    return chrome.runtime.sendMessage(message).catch(() => null);
  } catch {
    return Promise.resolve(null);
  }
}

function firstElement(selectors, root = document) {
  for (const selector of selectors) {
    const element = root.querySelector(selector);
    if (element) return element;
  }
  return null;
}

async function waitForElement(selectors, timeoutMs = 180000, predicate = null) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const element = firstElement(selectors);
    if (element && (!predicate || predicate(element))) return element;
    await sleep(500);
  }
  throw new Error(
    "Chat composer did not become available. Check that the persistent chat URL is correct and that you are logged in."
  );
}

function setNativeValue(element, value) {
  const prototype = Object.getPrototypeOf(element);
  const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
  if (descriptor && descriptor.set) {
    descriptor.set.call(element, value);
  } else {
    element.value = value;
  }
}

function fillComposer(element, prompt) {
  element.focus();

  if (
    element instanceof HTMLTextAreaElement ||
    element instanceof HTMLInputElement
  ) {
    setNativeValue(element, prompt);
  } else {
    let inserted = false;
    try {
      document.execCommand("selectAll", false, null);
      inserted = document.execCommand("insertText", false, prompt);
    } catch {
      inserted = false;
    }
    if (!inserted || !(element.innerText || "").trim()) {
      element.replaceChildren();
      if (element.classList.contains("ProseMirror") || element.tagName === "DIV") {
        const paragraph = document.createElement("p");
        paragraph.textContent = prompt;
        element.appendChild(paragraph);
      } else {
        element.textContent = prompt;
      }
    }
  }

  try {
    element.dispatchEvent(
      new InputEvent("input", {
        bubbles: true,
        inputType: "insertText",
        data: prompt,
      })
    );
  } catch {
    element.dispatchEvent(new Event("input", { bubbles: true }));
  }
  element.dispatchEvent(new Event("change", { bubbles: true }));
}

function visibleText(element) {
  if (!element) return "";
  const codeBlocks = Array.from(element.querySelectorAll("pre code"))
    .map((node) => (node.innerText || node.textContent || "").trim())
    .filter(Boolean);
  if (codeBlocks.length === 1 && /^[\s]*[\[{]/.test(codeBlocks[0])) {
    return codeBlocks[0];
  }
  return (element.innerText || element.textContent || "").trim();
}

function responseText(node, adapter) {
  for (const selector of adapter.responseBody) {
    if (node.matches && node.matches(selector)) {
      const ownText = visibleText(node);
      if (ownText) return ownText;
    }

    const bodies = Array.from(node.querySelectorAll(selector));
    if (adapter.preferLastResponseBody) {
      bodies.reverse();
    }
    for (const body of bodies) {
      const text = visibleText(body);
      if (text) return text;
    }
  }
  return visibleText(node);
}

function assistantCandidates(adapter) {
  const seen = new Set();
  const result = [];
  for (const selector of adapter.assistant) {
    for (const node of document.querySelectorAll(selector)) {
      if (seen.has(node)) continue;
      seen.add(node);
      const text = responseText(node, adapter);
      if (text) result.push({ node, text });
    }
  }
  return result;
}

function snapshotAssistantTexts(adapter) {
  return assistantCandidates(adapter).map((item) => item.text);
}

function pageIsBusy(adapter) {
  return adapter.busy.some((selector) => {
    return Array.from(document.querySelectorAll(selector)).some((element) => {
      const style = window.getComputedStyle(element);
      return style.display !== "none" && style.visibility !== "hidden";
    });
  });
}

function sendButtonReady(adapter) {
  const button = firstElement(adapter.send);
  return Boolean(
    button &&
    !button.disabled &&
    button.getAttribute("aria-disabled") !== "true"
  );
}

async function waitForSendButton(adapter) {
  return waitForElement(
    adapter.send,
    30000,
    (button) =>
      !button.disabled &&
      button.getAttribute("aria-disabled") !== "true"
  );
}

async function waitForFreshResponse(adapter, beforeTexts) {
  const timeoutMs = 10 * 60 * 1000;
  const started = Date.now();
  let lastText = "";
  let stablePolls = 0;

  while (Date.now() - started < timeoutMs) {
    const candidates = assistantCandidates(adapter);
    let candidate = null;

    if (candidates.length > beforeTexts.length) {
      candidate = candidates[candidates.length - 1];
    } else {
      for (let index = candidates.length - 1; index >= 0; index -= 1) {
        if (!beforeTexts.includes(candidates[index].text)) {
          candidate = candidates[index];
          break;
        }
      }
    }

    if (candidate && candidate.text) {
      if (candidate.text === lastText) {
        stablePolls += 1;
      } else {
        lastText = candidate.text;
        stablePolls = 0;
      }

      const idle = !pageIsBusy(adapter);
      const composerReadyAgain = sendButtonReady(adapter);
      const readyStablePolls = adapter.completionStablePolls || 3;
      const fallbackStablePolls = adapter.fallbackStablePolls || 7;
      if (
        idle &&
        Date.now() - started > 4000 &&
        (
          (stablePolls >= readyStablePolls && composerReadyAgain) ||
          stablePolls >= fallbackStablePolls
        )
      ) {
        return candidate.text;
      }
    }

    await sleep(1000);
  }

  throw new Error(
    "Timed out waiting for the external model response. The chat page may have changed its DOM."
  );
}

async function runJob(message) {
  const adapter = adapterForPage();
  if (!adapter) {
    throw new Error("This external chat site is not supported by the bridge.");
  }

  try {
    const requested = new URL(message.chatUrl);
    if (requested.hostname !== window.location.hostname) {
      throw new Error("The bridge opened the wrong external chat host.");
    }
  } catch (error) {
    if (error instanceof TypeError) {
      throw new Error("Configured external chat URL is invalid.");
    }
    throw error;
  }

  const beforeTexts = snapshotAssistantTexts(adapter);
  const composer = await waitForElement(adapter.composer);
  fillComposer(composer, message.prompt);

  const sendButton = await waitForSendButton(adapter);
  sendButton.click();

  const response = await waitForFreshResponse(adapter, beforeTexts);
  if (!response.trim()) {
    throw new Error(adapter.name + " returned an empty response.");
  }

  await chrome.runtime.sendMessage({
    type: "MRAZ_EXTERNAL_RESULT",
    jobId: message.jobId,
    response,
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "MRAZ_BRIDGE_RUN") return false;

  if (activeJobs.has(message.jobId)) {
    sendResponse({ accepted: true, duplicate: true });
    return false;
  }

  activeJobs.add(message.jobId);
  sendResponse({ accepted: true });

  runJob(message)
    .catch(async (error) => {
      await chrome.runtime.sendMessage({
        type: "MRAZ_EXTERNAL_ERROR",
        jobId: message.jobId,
        error: String(error && error.message ? error.message : error),
      });
    })
    .finally(() => {
      activeJobs.delete(message.jobId);
    });

  return false;
});


safeRuntimeMessage({ type: "MRAZ_EXTERNAL_READY" });

// When this persistent chat tab is left open on the GM machine, its heartbeat
// keeps the MV3 service worker awake enough to notice new HUMAN -> GM autoplay
// work even while the local MRAZ scene tab is in the background. If the
// extension itself is reloaded, the old content script becomes invalid; the
// guarded call below lets that obsolete script go quiet instead of throwing
// every 2.5 seconds until the page is refreshed.
window.setInterval(() => {
  safeRuntimeMessage({ type: "MRAZ_AUTOPLAY_TICK" });
}, 2500);
