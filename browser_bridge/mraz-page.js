const BRIDGE_BUTTON_SELECTOR = ".mraz-browser-bridge-button";
const STATUS_SELECTOR = ".mraz-browser-bridge-status";
const AUTOPLAY_SELECTOR = '[data-mraz-gm-autoplay="1"]';
const AUTOSTART_CARD_SELECTOR = '[data-mraz-bridge-card][data-mraz-bridge-autostart="1"]';

function setBridgeReady() {
  document.documentElement.dataset.mrazBrowserBridge = "ready";
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

function trace(event, details = {}) {
  const entry = {
    component: "mraz-page",
    event,
    details: {
      pageUrl: window.location.href,
      ...details,
    },
  };
  console.log("[MRAZ Bridge source]", event, entry.details);
  return safeRuntimeMessage({
    type: "MRAZ_DEBUG_LOG",
    ...entry,
  });
}

function bridgeCardFromButton(button) {
  return button.closest("[data-mraz-bridge-card]");
}

function statusElement(card) {
  return card ? card.querySelector(STATUS_SELECTOR) : null;
}

function setStatus(card, text, state = "") {
  const status = statusElement(card);
  if (!status) return;
  status.textContent = text;
  status.dataset.state = state;
}

function makeJobId(card) {
  const kind = card.dataset.mrazBridgeKind || "manual";
  const execution = card.dataset.mrazBridgeExecution || "unknown";
  return kind + ":" + execution;
}

async function startBridge(button) {
  const card = bridgeCardFromButton(button);
  if (!card) {
    trace("bridge-result-card-missing", {
      jobId: message.jobId || "",
      kind,
      execution,
    });
    return false;
  }
  if (card.dataset.mrazBridgeJob) return true;

  const promptElement = card.querySelector("[data-mraz-bridge-prompt]");
  const responseForm = card.querySelector("[data-mraz-bridge-response-form]");
  const chatUrl = card.dataset.mrazBridgeChatUrl || "";
  const label = card.dataset.mrazBridgeLabel || "External chat";

  if (!promptElement || !responseForm) {
    setStatus(card, "Bridge markup is incomplete.", "error");
    return;
  }
  if (!chatUrl) {
    setStatus(card, "No external chat URL configured.", "error");
    return;
  }

  const prompt = "value" in promptElement
    ? promptElement.value
    : promptElement.textContent || "";
  if (!prompt.trim()) {
    setStatus(card, "Prompt is empty.", "error");
    return;
  }

  const jobId = makeJobId(card);
  card.dataset.mrazBridgeJob = jobId;
  trace("bridge-start", {
    jobId,
    kind: card.dataset.mrazBridgeKind || "",
    execution: card.dataset.mrazBridgeExecution || "",
    chatUrl,
    label,
    promptLength: prompt.length,
  });
  button.disabled = true;
  setStatus(card, "Opening external chat…", "running");

  try {
    const result = await chrome.runtime.sendMessage({
      type: "MRAZ_BRIDGE_START",
      jobId,
      prompt,
      chatUrl,
      label,
    });
    if (!result || !result.accepted) {
      throw new Error(result && result.error ? result.error : "Bridge rejected the job.");
    }
    trace("bridge-start-accepted", {
      jobId,
      reused: Boolean(result.reused),
      state: result.state || "",
    });
    setStatus(card, "Prompt sent. Waiting for model response…", "running");
  } catch (error) {
    trace("bridge-start-error", {
      jobId,
      error: String(error && error.message ? error.message : error),
    });
    button.disabled = false;
    setStatus(
      card,
      String(error && error.message ? error.message : error),
      "error"
    );
  }
}

function submitBridgeResult(message) {
  trace("bridge-result-received", {
    jobId: message.jobId || "",
    ok: Boolean(message.ok),
    responseLength: message.response ? message.response.length : 0,
    error: message.error || "",
  });
  const [kind, execution] = String(message.jobId || "").split(":", 2);
  const cards = document.querySelectorAll("[data-mraz-bridge-card]");
  const card = Array.from(cards).find(
    (candidate) =>
      candidate.dataset.mrazBridgeKind === kind &&
      candidate.dataset.mrazBridgeExecution === execution
  );
  if (!card) return false;

  const button = card.querySelector(BRIDGE_BUTTON_SELECTOR);
  if (button) button.disabled = false;

  if (!message.ok) {
    setStatus(card, message.error || "External chat bridge failed.", "error");
    return true;
  }

  const form = card.querySelector("[data-mraz-bridge-response-form]");
  const response = form && form.querySelector('textarea[name="response"]');
  if (!form || !response) {
    setStatus(card, "Response returned, but the import form is missing.", "error");
    return false;
  }

  response.value = message.response || "";
  response.dispatchEvent(new Event("input", { bubbles: true }));
  setStatus(card, "Response received. Importing…", "done");

  // Let the extension message acknowledgement return before navigation tears
  // down this content script. The normal Django form remains the authority.
  trace("bridge-import-submit", {
    jobId: message.jobId || "",
    responseLength: response.value.length,
  });
  window.setTimeout(() => form.requestSubmit(), 0);
  return true;
}

document.addEventListener("click", (event) => {
  const button = event.target.closest(BRIDGE_BUTTON_SELECTOR);
  if (!button) return;
  event.preventDefault();
  startBridge(button);
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "MRAZ_BRIDGE_RESULT") {
    sendResponse({ accepted: submitBridgeResult(message) });
    return false;
  }
  return false;
});

function autoStartBridgeIfPresent() {
  const card = document.querySelector(AUTOSTART_CARD_SELECTOR);
  if (!card || card.dataset.mrazBridgeJob) return false;
  trace("autostart-card-found", {
    kind: card.dataset.mrazBridgeKind || "",
    execution: card.dataset.mrazBridgeExecution || "",
    chatUrl: card.dataset.mrazBridgeChatUrl || "",
  });
  const button = card.querySelector(BRIDGE_BUTTON_SELECTOR);
  if (!button || button.disabled) return false;
  startBridge(button);
  return true;
}

function autoplayEnabled() {
  return Boolean(document.querySelector(AUTOPLAY_SELECTOR));
}

async function registerAutoplaySource() {
  if (!autoplayEnabled()) return;
  await safeRuntimeMessage({
    type: "MRAZ_AUTOPLAY_REGISTER",
    sourceUrl: window.location.href,
  });
}

function tickAutoplay() {
  if (!autoplayEnabled()) return;
  safeRuntimeMessage({ type: "MRAZ_AUTOPLAY_TICK" });
}

setBridgeReady();
trace("content-script-ready");
autoStartBridgeIfPresent();
safeRuntimeMessage({ type: "MRAZ_SOURCE_READY" });
registerAutoplaySource().then(tickAutoplay).catch(() => {});
window.setInterval(tickAutoplay, 2500);
