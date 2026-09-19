const JOB_PREFIX = "mraz-bridge-job:";

function jobKey(jobId) {
  return JOB_PREFIX + jobId;
}

async function saveJob(job) {
  await chrome.storage.session.set({ [jobKey(job.jobId)]: job });
}

async function loadJob(jobId) {
  const key = jobKey(jobId);
  const data = await chrome.storage.session.get(key);
  return data[key] || null;
}

async function deleteJob(jobId) {
  await chrome.storage.session.remove(jobKey(jobId));
}

async function allJobs() {
  const data = await chrome.storage.session.get(null);
  return Object.values(data).filter(
    (value) => value && value.jobId && value.sourceTabId
  );
}

function normalizeUrl(url) {
  try {
    const parsed = new URL(url);
    parsed.hash = "";
    return parsed.href.replace(/\/$/, "");
  } catch {
    return "";
  }
}

function supportedExternalHost(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return ["chatgpt.com", "claude.ai", "gemini.google.com"].includes(host);
  } catch {
    return false;
  }
}

async function findExistingTargetTab(chatUrl) {
  const target = normalizeUrl(chatUrl);
  if (!target) return null;

  const tabs = await chrome.tabs.query({});
  const exact = tabs.find((tab) => normalizeUrl(tab.url || "") === target);
  if (exact) return exact;

  try {
    const desired = new URL(chatUrl);
    return tabs.find((tab) => {
      if (!tab.url) return false;
      try {
        const current = new URL(tab.url);
        return (
          current.hostname === desired.hostname &&
          current.pathname === desired.pathname
        );
      } catch {
        return false;
      }
    }) || null;
  } catch {
    return null;
  }
}

async function sendJobToExternalTab(job) {
  if (!job.targetTabId) return false;
  try {
    const response = await chrome.tabs.sendMessage(job.targetTabId, {
      type: "MRAZ_BRIDGE_RUN",
      jobId: job.jobId,
      prompt: job.prompt,
      chatUrl: job.chatUrl,
      label: job.label || "External chat",
    });
    if (response && response.accepted) {
      job.state = "running";
      await saveJob(job);
      return true;
    }
  } catch {
    // The content script may not exist yet because navigation is still loading.
  }
  return false;
}

async function notifySource(job, payload) {
  try {
    await chrome.tabs.sendMessage(job.sourceTabId, {
      type: "MRAZ_BRIDGE_RESULT",
      jobId: job.jobId,
      ...payload,
    });
  } catch (error) {
    console.warn("MRAZ bridge could not notify source tab", error);
  }
}

async function startJob(message, sender) {
  if (!sender.tab || !sender.tab.id) {
    throw new Error("Bridge request did not originate from a browser tab.");
  }
  if (!message.jobId || !message.prompt || !message.chatUrl) {
    throw new Error("Bridge request is missing jobId, prompt, or chatUrl.");
  }
  if (!supportedExternalHost(message.chatUrl)) {
    throw new Error(
      "Unsupported external chat host. Supported: ChatGPT, Claude, Gemini."
    );
  }

  const existing = await loadJob(message.jobId);
  if (existing) {
    return { accepted: true, reused: true };
  }

  let target = await findExistingTargetTab(message.chatUrl);
  if (!target) {
    target = await chrome.tabs.create({
      url: message.chatUrl,
      active: true,
    });
  } else {
    await chrome.tabs.update(target.id, { active: true });
    if (target.windowId) {
      await chrome.windows.update(target.windowId, { focused: true });
    }
  }

  const job = {
    jobId: message.jobId,
    sourceTabId: sender.tab.id,
    targetTabId: target.id,
    chatUrl: message.chatUrl,
    prompt: message.prompt,
    label: message.label || "External chat",
    state: "waiting-tab",
    createdAt: Date.now(),
  };
  await saveJob(job);

  if (target.status === "complete") {
    await sendJobToExternalTab(job);
  }
  return { accepted: true };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.type) return false;

  if (message.type === "MRAZ_BRIDGE_START") {
    startJob(message, sender)
      .then(sendResponse)
      .catch(async (error) => {
        if (message.jobId && sender.tab && sender.tab.id) {
          const job = {
            jobId: message.jobId,
            sourceTabId: sender.tab.id,
          };
          await notifySource(job, {
            ok: false,
            error: String(error && error.message ? error.message : error),
          });
        }
        sendResponse({
          accepted: false,
          error: String(error && error.message ? error.message : error),
        });
      });
    return true;
  }

  if (message.type === "MRAZ_EXTERNAL_READY") {
    (async () => {
      if (!sender.tab || !sender.tab.id) {
        sendResponse({ accepted: false });
        return;
      }
      const jobs = await allJobs();
      for (const job of jobs) {
        if (job.targetTabId === sender.tab.id && job.state === "waiting-tab") {
          await sendJobToExternalTab(job);
        }
      }
      sendResponse({ accepted: true });
    })();
    return true;
  }

  if (
    message.type === "MRAZ_EXTERNAL_RESULT" ||
    message.type === "MRAZ_EXTERNAL_ERROR"
  ) {
    (async () => {
      const job = await loadJob(message.jobId);
      if (!job) {
        sendResponse({ accepted: false, error: "Unknown or expired bridge job." });
        return;
      }

      await notifySource(job, {
        ok: message.type === "MRAZ_EXTERNAL_RESULT",
        response: message.response || "",
        error: message.error || "",
      });
      await deleteJob(message.jobId);
      sendResponse({ accepted: true });
    })();
    return true;
  }

  return false;
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo) => {
  if (changeInfo.status !== "complete") return;
  const jobs = await allJobs();
  for (const job of jobs) {
    if (job.targetTabId === tabId && job.state === "waiting-tab") {
      await sendJobToExternalTab(job);
    }
  }
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const jobs = await allJobs();
  for (const job of jobs) {
    if (job.targetTabId === tabId) {
      await notifySource(job, {
        ok: false,
        error: "External chat tab was closed before the response returned.",
      });
      await deleteJob(job.jobId);
    }
  }
});
