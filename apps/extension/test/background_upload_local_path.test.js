const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const SRC = fs.readFileSync(path.join(__dirname, "..", "src", "background.js"), "utf8");

function fnSource(name) {
  const asyncStart = SRC.indexOf(`async function ${name}`);
  const start = asyncStart >= 0 ? asyncStart : SRC.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `${name} not found`);
  const bodyStart = SRC.indexOf("{", start);
  let depth = 0;
  for (let i = bodyStart; i < SRC.length; i += 1) {
    if (SRC[i] === "{") depth += 1;
    if (SRC[i] === "}") depth -= 1;
    if (depth === 0) return SRC.slice(start, i + 1);
  }
  throw new Error("unclosed");
}

// uploadFile with every browser call stubbed: the page accepts the injected File but cannot prove it (Paycom's autofill
// modal showed the file while the input read back empty), so the debugger fallback runs.
function harness() {
  const calls = { downloads: [], cdp: [] };
  const env = {
    getConfig: async () => ({ backend: "http://127.0.0.1:8001" }),
    fetch: async () => ({ ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(4), headers: { get: () => "application/pdf" } }),
    _arrayBufferToBase64: () => "AAAA",
    chrome: { scripting: { executeScript: async ({ func }) => [{ result: func === "inject" ? { ok: true, value_present: false } : {} }] } },
    _injectFileIntoInput: "inject",
    _uploadPageEvidence: "evidence",
    _uploadEvidencePresent: () => false,
    _downloadToLocal: async (url, filename) => { calls.downloads.push([url, filename]); return "/Users/x/Downloads/lumen-upload/" + filename; },
    acquireDebugger: async () => {},
    releaseDebugger: () => {},
    _cdp: async (target, method, params) => {
      calls.cdp.push([method, params]);
      if (method === "DOM.getDocument") return { root: { nodeId: 1 } };
      if (method === "DOM.querySelector") return { nodeId: 7 };
      return {};
    },
    errorMessage: (error) => String((error && error.message) || error),
    setTimeout: (fn) => fn(),
  };
  const names = Object.keys(env);
  const uploadFile = new Function(...names, `return (${fnSource("uploadFile")});`)(...names.map((name) => env[name]));
  return { uploadFile, calls };
}

test("the backend's own copy is attached by path with no download, so Chrome never asks where to save", async () => {
  const { uploadFile, calls } = harness();
  const result = await uploadFile(5, {
    selector: "#resume", filename: "Jordan_Park_Resume.pdf", download_path: "/resume-artifacts/1/download",
    local_path: "/Users/me/Projects/lumen/storage/artifacts/resumes/1/Jordan_Park_Resume.pdf",
  });
  assert.equal(result.ok, true);
  assert.equal(result.mode, "cdp_set_file");
  assert.equal(result.local_path_used, true);
  assert.deepEqual(calls.downloads, [], "no staging download");
  const setFiles = calls.cdp.find(([method]) => method === "DOM.setFileInputFiles");
  assert.deepEqual(setFiles[1].files, ["/Users/me/Projects/lumen/storage/artifacts/resumes/1/Jordan_Park_Resume.pdf"]);
});

test("without a usable local path the staging download is still the fallback", async () => {
  for (const localPath of [undefined, "", "relative/Jordan_Park_Resume.pdf"]) {
    const { uploadFile, calls } = harness();
    const result = await uploadFile(5, { selector: "#resume", filename: "Jordan_Park_Resume.pdf", download_path: "/resume-artifacts/1/download", local_path: localPath });
    assert.equal(result.ok, true);
    assert.equal(result.local_path_used, false);
    assert.equal(calls.downloads.length, 1);
  }
});
