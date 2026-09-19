const BRIDGE_BUTTON_SELECTOR = ".mraz-browser-bridge-button";
const STATUS_SELECTOR = ".mraz-browser-bridge-status";

function setBridgeReady() {
  document.documentElement.dataset.mrazBrowserBridge = "ready";
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
  if (!card) return false;

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
    setStatus(card, "Prompt sent. Waiting for model response…", "running");
  } catch (error) {
    button.disabled = false;
    setStatus(
      card,
      String(error && error.message ? error.message : error),
      "error"
    );
  }
}

function submitBridgeResult(message) {
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
  form.requestSubmit();
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

setBridgeReady();
chrome.runtime.sendMessage({ type: "MRAZ_SOURCE_READY" }).catch(() => {});
