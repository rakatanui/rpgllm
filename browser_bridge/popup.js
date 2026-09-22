const logBox = document.getElementById("log");
const status = document.getElementById("status");

function formatEntry(entry) {
  const details = entry && entry.details ? JSON.stringify(entry.details) : "{}";
  return `[${entry.at || "?"}] ${entry.component || "?"} :: ${entry.event || "?"} ${details}`;
}

async function refreshLogs() {
  const result = await chrome.runtime.sendMessage({ type: "MRAZ_DEBUG_GET" });
  if (!result || !result.accepted) {
    status.textContent = result && result.error ? result.error : "Could not load logs";
    return;
  }
  const logs = Array.isArray(result.logs) ? result.logs : [];
  logBox.value = logs.map(formatEntry).join("\n");
  logBox.scrollTop = logBox.scrollHeight;
  const version = chrome.runtime.getManifest().version;
  status.textContent = "v" + version + " · " + logs.length + " events";
}

document.getElementById("refresh").addEventListener("click", refreshLogs);

document.getElementById("copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(logBox.value);
    status.textContent = "Copied";
  } catch {
    logBox.focus();
    logBox.select();
    document.execCommand("copy");
    status.textContent = "Copied";
  }
});

document.getElementById("clear").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "MRAZ_DEBUG_CLEAR" });
  await refreshLogs();
});

refreshLogs();
