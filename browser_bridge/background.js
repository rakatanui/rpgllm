const JOB_PREFIX = "mraz-bridge-job:";
const AUTOPLAY_SOURCE_KEY = "mraz-autoplay-source";

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

async function saveAutoplaySource(source) {
  await chrome.storage.session.set({ [AUTOPLAY_SOURCE_KEY]: source });
}

async function loadAutoplaySource() {
  const data = await chrome.storage.session.get(AUTOPLAY_SOURCE_KEY);
  return data[AUTOPLAY_SOURCE_KEY] || null;
}

async function clearAutoplaySource() {
  await chrome.storage.session.remove(AUTOPLAY_SOURCE_KEY);
}

async function allJobs() {
  const data = await chrome.storage.session.get(null);
  const jobs = Object.values(data).filter(
    (value) => value && value.jobId && value.sourceTabId
  );
  const staleBefore = Date.now() - 30 * 60 * 1000;
  for (const job of jobs) {
    if (job.createdAt && job.createdAt < staleBefore) {
      await deleteJob(job.jobId);
    }
  }
  return jobs.filter(
    (job) => !job.createdAt || job.createdAt >= staleBefore
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
    return ["chatgpt.com", "claude.ai", "gemini.google.com", "chat.deepseek.com"].includes(host);
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
    // The content script may not exist yet because navigation is still loading,
    // or an already-open tab may still have an invalidated pre-reload script.
  }
  return false;
}

async function notifySource(job, payload) {
  try {
    const response = await chrome.tabs.sendMessage(job.sourceTabId, {
      type: "MRAZ_BRIDGE_RESULT",
      jobId: job.jobId,
      ...payload,
    });
    return Boolean(response && response.accepted);
  } catch (error) {
    console.warn("MRAZ bridge could not notify source tab", error);
    return false;
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
      "Unsupported external chat host. Supported: ChatGPT, Claude, Gemini, DeepSeek."
    );
  }

  const existing = await loadJob(message.jobId);
  if (existing) {
    return { accepted: true, reused: true };
  }

  let target = await findExistingTargetTab(message.chatUrl);
  const reusedExistingTab = Boolean(target);
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
    const sent = await sendJobToExternalTab(job);
    if (!sent && reusedExistingTab) {
      // A tab that survived an extension Reload still contains the old,
      // invalidated content-script context. Reload it once so Manifest V3
      // injects the current external-chat.js, whose MRAZ_EXTERNAL_READY
      // handshake will pick up this waiting job.
      await chrome.tabs.reload(target.id);
    }
  }
  return { accepted: true };
}

async function registerAutoplaySource(message, sender) {
  if (!sender.tab || !sender.tab.id || !message.sourceUrl) {
    return { accepted: false };
  }
  const source = {
    sourceTabId: sender.tab.id,
    sourceUrl: message.sourceUrl,
    registeredAt: Date.now(),
    lastReloadExecution: null,
    lastReloadAt: 0,
  };
  await saveAutoplaySource(source);
  return { accepted: true };
}

async function tickAutoplay() {
  const source = await loadAutoplaySource();
  if (!source || !source.sourceTabId || !source.sourceUrl) {
    return { accepted: false };
  }

  try {
    await chrome.tabs.get(source.sourceTabId);
  } catch {
    await clearAutoplaySource();
    return { accepted: false };
  }

  const jobs = await allJobs();
  if (jobs.some((job) => job.sourceTabId === source.sourceTabId)) {
    return { accepted: true, busy: true };
  }

  let response;
  try {
    response = await fetch(source.sourceUrl, {
      method: "GET",
      cache: "no-store",
      credentials: "include",
      redirect: "follow",
    });
  } catch {
    return { accepted: false };
  }
  if (!response.ok) {
    return { accepted: false };
  }

  const html = await response.text();
  const autostartMatch = html.match(
    /data-mraz-bridge-execution="(\d+)"[\s\S]{0,1200}?data-mraz-bridge-autostart="1"|data-mraz-bridge-autostart="1"[\s\S]{0,1200}?data-mraz-bridge-execution="(\d+)"/
  );
  const executionId = autostartMatch
    ? (autostartMatch[1] || autostartMatch[2])
    : null;

  if (!executionId) {
    if (source.lastReloadExecution !== null) {
      source.lastReloadExecution = null;
      source.lastReloadAt = 0;
      await saveAutoplaySource(source);
    }
    return { accepted: true, pending: false };
  }

  const now = Date.now();
  if (
    source.lastReloadExecution === executionId &&
    now - Number(source.lastReloadAt || 0) < 8000
  ) {
    return { accepted: true, pending: true, rateLimited: true };
  }

  source.lastReloadExecution = executionId;
  source.lastReloadAt = now;
  await saveAutoplaySource(source);
  await chrome.tabs.reload(source.sourceTabId);
  return { accepted: true, pending: true, reloaded: true };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.type) return false;

  if (message.type === "MRAZ_AUTOPLAY_REGISTER") {
    registerAutoplaySource(message, sender)
      .then(sendResponse)
      .catch(() => sendResponse({ accepted: false }));
    return true;
  }

  if (message.type === "MRAZ_AUTOPLAY_TICK") {
    tickAutoplay()
      .then(sendResponse)
      .catch(() => sendResponse({ accepted: false }));
    return true;
  }

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

  if (message.type === "MRAZ_SOURCE_READY") {
    (async () => {
      if (!sender.tab || !sender.tab.id) {
        sendResponse({ accepted: false });
        return;
      }
      const jobs = await allJobs();
      for (const job of jobs) {
        if (
          job.sourceTabId === sender.tab.id &&
          job.state === "result-ready" &&
          job.result
        ) {
          const delivered = await notifySource(job, job.result);
          if (delivered) {
            await deleteJob(job.jobId);
          }
        }
      }
      sendResponse({ accepted: true });
    })();
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

      job.state = "result-ready";
      job.result = {
        ok: message.type === "MRAZ_EXTERNAL_RESULT",
        response: message.response || "",
        error: message.error || "",
      };
      await saveJob(job);
      const delivered = await notifySource(job, job.result);
      if (delivered) {
        await deleteJob(message.jobId);
      }
      sendResponse({ accepted: true, delivered });
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
  const source = await loadAutoplaySource();
  if (source && source.sourceTabId === tabId) {
    await clearAutoplaySource();
  }

  const jobs = await allJobs();
  for (const job of jobs) {
    if (job.sourceTabId === tabId) {
      await deleteJob(job.jobId);
      continue;
    }
    if (job.targetTabId === tabId) {
      await notifySource(job, {
        ok: false,
        error: "External chat tab was closed before the response returned.",
      });
      await deleteJob(job.jobId);
    }
  }
});
