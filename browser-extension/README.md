# Filenergy Browser Extension

Chrome MV3 popup that POSTs the current tab's URL to a Filenergy
workspace via the `/file/from_url/` endpoint.

## Install (Chrome / Edge / Brave)

1. `chrome://extensions/` → enable Developer Mode.
2. "Load unpacked" → select this folder.
3. Open the popup, paste your Filenergy server URL and a workspace API
   key (mint one at `/settings/keys`).
4. Click "Save page" while on any tab.

The base URL and API key are stored in `chrome.storage.sync`.

## Notes

- We hit the cookie-authenticated `/file/from_url/` endpoint with an
  `Authorization: Bearer` header — the server's API-key middleware
  validates it.
- The extension does not request `<all_urls>` host permissions; only
  `activeTab` so it can read the current tab's URL.
- Works against any Filenergy server you can reach over HTTPS.
