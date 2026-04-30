// Minimal Filenergy "Save current tab" popup.
// Persists base URL + API key across sessions; POSTs to /file/from_url/.

const $ = (id) => document.getElementById(id);

async function init() {
  const stored = await chrome.storage.sync.get(["base", "token"]);
  if (stored.base) $("base").value = stored.base;
  if (stored.token) $("token").value = stored.token;

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab && tab.url) {
    $("current-url").textContent = tab.url;
  }

  $("save").addEventListener("click", () => save(tab));
}

async function save(tab) {
  const base = $("base").value.trim().replace(/\/+$/, "");
  const token = $("token").value.trim();
  const status = $("status");
  status.className = "status";
  status.textContent = "";

  if (!base || !token || !tab || !tab.url) {
    status.textContent = "Missing server, token, or active tab.";
    status.className = "status err";
    return;
  }

  await chrome.storage.sync.set({ base, token });
  $("save").disabled = true;
  status.textContent = "Sending...";

  try {
    // Call the workspace-scoped browser endpoint via the API key path.
    // /api/v1 doesn't expose URL ingestion yet, so we use the cookie-less
    // /file/from_url/ endpoint with form-urlencoded auth via X-API-Key.
    const form = new URLSearchParams({ url: tab.url });
    const resp = await fetch(`${base}/file/from_url/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Authorization: `Bearer ${token}`,
      },
      body: form,
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`HTTP ${resp.status}: ${text.slice(0, 100)}`);
    }
    const body = await resp.json().catch(() => ({}));
    status.textContent = `Saved as ${body.name || "(ok)"}`;
    status.className = "status ok";
  } catch (e) {
    status.textContent = String(e.message || e);
    status.className = "status err";
  } finally {
    $("save").disabled = false;
  }
}

init();
