import {
  app,
  BrowserWindow,
  ipcMain,
  Menu,
  nativeImage,
  screen,
  shell,
  Tray
} from "electron";
import { createReadStream, createWriteStream, existsSync, mkdirSync, statSync } from "node:fs";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { controlIndicatorDataUrl } from "./control_indicator.mjs";
import { displayForPoint, pointerOverlayDataUrl } from "./pointer_overlay.mjs";

const DESKTOP_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(DESKTOP_DIR, "..", "..");

// The app was renamed from Lumen to Lumen. userData follows productName, and packaged builds keep the
// backend database under it, so pin the folder existing installs already use.
if (app.isPackaged) {
  app.setPath("userData", path.join(app.getPath("appData"), "Lumen"));
}
const API_ORIGIN = "http://127.0.0.1:8001";
const DESKTOP_PORTS = Array.from({ length: 10 }, (_, index) => 3210 + index);
const RUNTIME_SUPERVISOR_INTERVAL_MS = 5000;
const RUNTIME_RECOVERY_FAILURE_THRESHOLD = 3;
const RUNTIME_RECOVERY_BACKOFF_MS = [5000, 15000, 60000];
const CONTROL_INDICATOR_POLL_MS = 750;
const POINTER_POLL_MS = 250;
// The desktop body (its status poll and pointer overlay) runs on Windows and macOS. Calls that only
// exist on Windows are gated on win32 at their own sites.
const DESKTOP_BODY_PLATFORMS = new Set(["win32", "darwin"]);
const hasDesktopBody = () => DESKTOP_BODY_PLATFORMS.has(process.platform);
const MIME_TYPES = new Map([
  [".css", "text/css; charset=utf-8"],
  [".html", "text/html; charset=utf-8"],
  [".ico", "image/x-icon"],
  [".jpeg", "image/jpeg"],
  [".jpg", "image/jpeg"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".map", "application/json; charset=utf-8"],
  [".png", "image/png"],
  [".svg", "image/svg+xml"],
  [".txt", "text/plain; charset=utf-8"],
  [".webp", "image/webp"],
  [".woff", "font/woff"],
  [".woff2", "font/woff2"]
]);

let mainWindow = null;
let tray = null;
let staticServer = null;
let backendProcess = null;
let backendManaged = false;
let backendStartPromise = null;
let runtimeSupervisor = null;
let runtimeRecoveryInFlight = false;
let runtimeHealthFailures = 0;
let runtimeRecoveryAttempts = 0;
let nextRuntimeRecoveryAt = 0;
let quitting = false;
let controlIndicatorSupervisor = null;
let controlIndicatorWindows = [];
let controlIndicatorVisible = false;
let controlIndicatorWindowUpdate = Promise.resolve();
let pointerSupervisor = null;
let pointerOverlayEntries = [];
let pointerLastSeq = 0;
let lastPointerEntry = null;

function runtimeDataDir() {
  return path.join(app.getPath("userData"), "runtime");
}

function backendDataRoot() {
  // Development uses the same repository storage as npm-run API processes so recovery cannot
  // silently switch the desktop onto a second database.
  return app.isPackaged ? runtimeDataDir() : path.join(PROJECT_ROOT, "storage");
}

function logsDir() {
  return path.join(app.getPath("userData"), "logs");
}

function ensureRuntimeDirectories() {
  mkdirSync(backendDataRoot(), { recursive: true });
  mkdirSync(logsDir(), { recursive: true });
}

async function fetchJson(url, timeoutMs = 1000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      signal: controller.signal
    });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

async function postJson(url, timeoutMs = 2000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { Accept: "application/json" },
      signal: controller.signal
    });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

async function waitForBackend(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const health = await fetchJson(`${API_ORIGIN}/health`, 800);
    if (health?.status === "ok") return health;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return null;
}

function backendLaunchSpec() {
  if (!app.isPackaged) {
    return {
      command: process.env.LUMEN_PYTHON || "python",
      args: [
        "-m",
        "uvicorn",
        "app.main:app",
        "--app-dir",
        "apps/api",
        "--host",
        "127.0.0.1",
        "--port",
        "8001"
      ],
      cwd: PROJECT_ROOT
    };
  }

  const executable = path.join(
    process.resourcesPath,
    "binaries",
    process.platform === "win32" ? "lumen-api.exe" : "lumen-api"
  );
  if (!existsSync(executable)) {
    throw new Error(`Bundled Lumen runtime is missing: ${executable}`);
  }
  return { command: executable, args: [], cwd: process.resourcesPath };
}

async function startBackendProcess() {
  const existing = await fetchJson(`${API_ORIGIN}/health`, 800);
  if (existing?.status === "ok") return existing;
  if (backendProcess && backendProcess.exitCode == null) {
    return await waitForBackend();
  }

  ensureRuntimeDirectories();
  const launch = backendLaunchSpec();
  const logStream = createWriteStream(path.join(logsDir(), "lumen-api.log"), { flags: "a" });
  const env = {
    ...process.env,
    LUMEN_DATA_ROOT: backendDataRoot(),
    LUMEN_DESKTOP_CONTROL: "true",
    PYTHONUNBUFFERED: "1"
  };

  const launchedProcess = spawn(launch.command, launch.args, {
    cwd: launch.cwd,
    env,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true
  });
  backendProcess = launchedProcess;
  backendManaged = true;
  launchedProcess.stdout?.pipe(logStream, { end: false });
  launchedProcess.stderr?.pipe(logStream, { end: false });
  // An unhandled child-process "error" event would crash the host; health polling
  // already surfaces a runtime that failed to start.
  launchedProcess.once("error", () => {});
  launchedProcess.once("exit", () => {
    if (backendProcess === launchedProcess) {
      backendProcess = null;
      backendManaged = false;
    }
    logStream.end();
  });

  return await waitForBackend();
}

async function startBackend() {
  if (backendStartPromise) return await backendStartPromise;
  backendStartPromise = startBackendProcess();
  try {
    return await backendStartPromise;
  } finally {
    backendStartPromise = null;
  }
}

function stopBackend() {
  if (!backendManaged || !backendProcess || backendProcess.exitCode != null) return;
  backendProcess.kill();
  backendProcess = null;
  backendManaged = false;
}

async function restartBackend() {
  stopBackend();
  await new Promise((resolve) => setTimeout(resolve, 350));
  return await startBackend();
}

async function superviseBackend() {
  if (quitting || runtimeRecoveryInFlight) return;
  const health = await fetchJson(`${API_ORIGIN}/health`, 900);
  if (health?.status === "ok") {
    runtimeHealthFailures = 0;
    runtimeRecoveryAttempts = 0;
    nextRuntimeRecoveryAt = 0;
    return;
  }

  runtimeHealthFailures += 1;
  if (
    runtimeHealthFailures < RUNTIME_RECOVERY_FAILURE_THRESHOLD
    || Date.now() < nextRuntimeRecoveryAt
  ) return;

  runtimeRecoveryInFlight = true;
  try {
    const recovered = backendProcess && backendProcess.exitCode == null
      ? await restartBackend()
      : await startBackend();
    if (recovered?.status === "ok") {
      runtimeHealthFailures = 0;
      runtimeRecoveryAttempts = 0;
      nextRuntimeRecoveryAt = 0;
      return;
    }

    runtimeRecoveryAttempts += 1;
    const backoffIndex = Math.min(
      runtimeRecoveryAttempts - 1,
      RUNTIME_RECOVERY_BACKOFF_MS.length - 1
    );
    nextRuntimeRecoveryAt = Date.now() + RUNTIME_RECOVERY_BACKOFF_MS[backoffIndex];
  } finally {
    runtimeRecoveryInFlight = false;
  }
}

function startRuntimeSupervisor() {
  if (runtimeSupervisor) return;
  runtimeSupervisor = setInterval(() => {
    void superviseBackend();
  }, RUNTIME_SUPERVISOR_INTERVAL_MS);
}

function closeControlIndicatorWindows() {
  const windows = [
    ...controlIndicatorWindows,
    ...pointerOverlayEntries.map((entry) => entry.window),
  ];
  controlIndicatorWindows = [];
  pointerOverlayEntries = [];
  lastPointerEntry = null;
  for (const window of windows) {
    try {
      if (!window.isDestroyed()) window.destroy();
    } catch {
      // The display or app may already be shutting down.
    }
  }
}

function createProtectedOverlayWindow(bounds) {
  const overlay = new BrowserWindow({
    x: bounds.x,
    y: bounds.y,
    width: bounds.width,
    height: bounds.height,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    show: false,
    focusable: false,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    hasShadow: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  // The constructor clamps the size to the primary display's work area, so on mixed-DPI
  // setups a secondary monitor got a primary-sized box and the taskbar strip was left
  // uncovered. setBounds after construction applies the target display's own metrics.
  overlay.setBounds(bounds);
  overlay.setIgnoreMouseEvents(true, { forward: true });
  overlay.setAlwaysOnTop(true, "screen-saver");
  overlay.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  // Keep Lumen's own safety chrome out of the visual evidence used to choose and verify actions.
  overlay.setContentProtection(true);
  return overlay;
}

async function showControlIndicatorWindows() {
  if (!hasDesktopBody()) return;
  closeControlIndicatorWindows();
  const overlays = [];
  const pointerEntries = [];
  for (const display of screen.getAllDisplays()) {
    const bounds = display.bounds;
    const overlay = createProtectedOverlayWindow(bounds);
    await overlay.loadURL(controlIndicatorDataUrl());
    const pointerOverlay = createProtectedOverlayWindow(bounds);
    await pointerOverlay.loadURL(pointerOverlayDataUrl());
    if (!controlIndicatorVisible || quitting) {
      overlay.destroy();
      pointerOverlay.destroy();
      continue;
    }
    overlay.showInactive();
    pointerOverlay.showInactive();
    overlays.push(overlay);
    pointerEntries.push({ window: pointerOverlay, bounds });
  }
  if (!controlIndicatorVisible || quitting) {
    for (const overlay of overlays) {
      if (!overlay.isDestroyed()) overlay.destroy();
    }
    for (const entry of pointerEntries) {
      if (!entry.window.isDestroyed()) entry.window.destroy();
    }
    return;
  }
  controlIndicatorWindows = overlays;
  pointerOverlayEntries = pointerEntries;
}

function queueControlIndicatorWindowUpdate() {
  controlIndicatorWindowUpdate = controlIndicatorWindowUpdate
    .catch(() => {})
    .then(async () => {
      if (controlIndicatorVisible && !quitting) {
        await showControlIndicatorWindows();
      } else {
        closeControlIndicatorWindows();
      }
    });
  return controlIndicatorWindowUpdate;
}

function forwardControlStatus(message, keepAwake) {
  const text = String(message || "").trim();
  const awake = Boolean(keepAwake);
  for (const window of controlIndicatorWindows) {
    if (window.isDestroyed()) continue;
    window.webContents
      .executeJavaScript(
        `window.__lumenControlStatus && window.__lumenControlStatus(${JSON.stringify(text)}, ${awake})`,
        true,
      )
      .catch(() => {});
  }
}

let desktopSurfaceMinimized = false;

function syncDesktopSurfaceStage(desktopSurfaceActive) {
  // While a run drives the REAL desktop, Lumen gets off the stage: a human operator
  // would move aside too, and the agent must never fight this chat window for the
  // screen it is working on. Restores when the run ends, unless the user already
  // brought the window back themselves.
  if (desktopSurfaceActive && !desktopSurfaceMinimized) {
    desktopSurfaceMinimized = true;
    if (mainWindow && !mainWindow.isDestroyed() && !mainWindow.isMinimized()) {
      mainWindow.minimize();
    }
  } else if (!desktopSurfaceActive && desktopSurfaceMinimized) {
    desktopSurfaceMinimized = false;
    if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isMinimized()) {
      mainWindow.restore();
    }
  }
}

// A poll that failed (timeout, backend busy) says nothing about control. Reading it as "inactive"
// restored the Lumen window over a desktop task after one slow response; only a few misses in a
// row (the backend really gone) or an affirmative inactive status end the indicator.
const STATUS_MISSES_BEFORE_INACTIVE = 3;
let consecutiveStatusMisses = 0;

async function syncControlIndicator() {
  if (quitting || !hasDesktopBody()) return;
  const status = await fetchJson(`${API_ORIGIN}/operator/desktop/status`, 650);
  if (!status) {
    consecutiveStatusMisses += 1;
    if (consecutiveStatusMisses < STATUS_MISSES_BEFORE_INACTIVE) return;
  } else {
    consecutiveStatusMisses = 0;
  }
  const shouldShow = Boolean(
    status?.enabled
    && status?.control_indicator?.active,
  );
  syncDesktopSurfaceStage(Boolean(shouldShow && status?.control_indicator?.desktop_surface_active));
  if (shouldShow && controlIndicatorVisible) {
    // The pill mirrors the active run's live status line on every poll.
    forwardControlStatus(status?.control_indicator?.message, status?.keep_awake);
  }
  if (shouldShow === controlIndicatorVisible) return;
  controlIndicatorVisible = shouldShow;
  await queueControlIndicatorWindowUpdate();
}

function forwardPointerEvent(event) {
  if (!pointerOverlayEntries.length) return;
  let entry = lastPointerEntry && !lastPointerEntry.window.isDestroyed()
    ? lastPointerEntry
    : pointerOverlayEntries[0];
  let local = { ...event };
  if (Number.isFinite(event?.x) && Number.isFinite(event?.y)) {
    // The body publishes absolute physical virtual-screen pixels; overlay windows are
    // positioned in DIP display bounds.
    // Windows reports physical pixels that need converting to DIPs. macOS publishes global points, which
    // already are the display coordinates Electron uses, so the conversion is the identity there.
    const dip = process.platform === "win32" && typeof screen.screenToDipPoint === "function"
      ? screen.screenToDipPoint({ x: Math.round(event.x), y: Math.round(event.y) })
      : { x: event.x, y: event.y };
    entry = displayForPoint(pointerOverlayEntries, dip) || entry;
    local = { ...event, x: dip.x - entry.bounds.x, y: dip.y - entry.bounds.y };
  }
  lastPointerEntry = entry;
  if (entry.window.isDestroyed()) return;
  entry.window.webContents
    .executeJavaScript(
      `window.__lumenDesktopPointer && window.__lumenDesktopPointer(${JSON.stringify(local)})`,
      true,
    )
    .catch(() => {});
}

let pointerFeedPrimed = false;

async function syncDesktopPointer() {
  if (quitting || !controlIndicatorVisible || !pointerOverlayEntries.length) return;
  const payload = await fetchJson(
    `${API_ORIGIN}/operator/desktop/pointer?since=${pointerLastSeq}`,
    500,
  );
  if (!payload || !Number.isFinite(Number(payload.last_seq))) return;
  const lastSeq = Number(payload.last_seq);
  if (!pointerFeedPrimed) {
    // Never replay gestures recorded before this host attached to the feed.
    pointerFeedPrimed = true;
    pointerLastSeq = lastSeq;
    return;
  }
  pointerLastSeq = lastSeq;
  for (const event of Array.isArray(payload.events) ? payload.events : []) {
    forwardPointerEvent(event);
  }
}

function startControlIndicatorSupervisor() {
  if (controlIndicatorSupervisor || !hasDesktopBody()) return;
  void syncControlIndicator();
  controlIndicatorSupervisor = setInterval(() => {
    void syncControlIndicator();
  }, CONTROL_INDICATOR_POLL_MS);
  pointerSupervisor = setInterval(() => {
    void syncDesktopPointer();
  }, POINTER_POLL_MS);
  const refreshDisplays = () => {
    if (controlIndicatorVisible) void queueControlIndicatorWindowUpdate();
  };
  screen.on("display-added", refreshDisplays);
  screen.on("display-removed", refreshDisplays);
  screen.on("display-metrics-changed", refreshDisplays);
}

async function desktopStatus() {
  const [health, model, extension, computerUse] = await Promise.all([
    fetchJson(`${API_ORIGIN}/health`, 900),
    fetchJson(`${API_ORIGIN}/health/model`, 2200),
    fetchJson(`${API_ORIGIN}/operator/extension/status`, 900),
    fetchJson(`${API_ORIGIN}/operator/desktop/status`, 1400)
  ]);
  const modelConfigured = Boolean(model?.configured && model?.key_present && model?.live_enabled);
  // No vision section means the active provider IS the vision provider (one probe covers
  // both); only an explicit vision section can be independently unhealthy.
  const visionReady = model?.vision ? model.vision.status === "healthy" : true;
  return {
    backendHealthy: health?.status === "ok",
    modelConfigured,
    modelReady: Boolean(modelConfigured && model?.validated && visionReady),
    modelDisplayName: model?.display_name ?? "",
    modelError: model?.last_validation_error ?? model?.metadata?.error ?? "",
    extensionConnected: Boolean(extension?.connected),
    liveActionsReady: Boolean(extension?.live_actions_ready),
    computerUseAvailable: Boolean(computerUse?.available),
    computerUseEnabled: Boolean(computerUse?.enabled)
  };
}

function safeRendererPath(rendererRoot, requestUrl) {
  const pathname = decodeURIComponent(new URL(requestUrl, "http://127.0.0.1").pathname);
  const relative = pathname === "/" ? "index.html" : pathname.replace(/^\/+/, "");
  let candidate = path.resolve(rendererRoot, relative);
  const rootWithSeparator = `${path.resolve(rendererRoot)}${path.sep}`;
  if (candidate !== path.resolve(rendererRoot) && !candidate.startsWith(rootWithSeparator)) return null;
  if (!existsSync(candidate) || !statSync(candidate).isFile()) {
    if (!path.extname(candidate)) {
      const htmlCandidate = `${candidate}.html`;
      if (existsSync(htmlCandidate) && statSync(htmlCandidate).isFile()) candidate = htmlCandidate;
      else candidate = path.join(rendererRoot, "index.html");
    } else {
      return null;
    }
  }
  return candidate;
}

function listen(server, port) {
  return new Promise((resolve, reject) => {
    const onError = (error) => {
      server.off("listening", onListening);
      reject(error);
    };
    const onListening = () => {
      server.off("error", onError);
      resolve();
    };
    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(port, "127.0.0.1");
  });
}

async function startStaticServer() {
  const rendererRoot = app.isPackaged
    ? path.join(process.resourcesPath, "renderer")
    : path.resolve(DESKTOP_DIR, "..", "renderer", "out");
  if (!existsSync(path.join(rendererRoot, "index.html"))) {
    throw new Error(`Lumen desktop renderer is missing: ${rendererRoot}`);
  }
  staticServer = createServer((request, response) => {
    const filePath = safeRendererPath(rendererRoot, request.url || "/");
    if (!filePath) {
      response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      response.end("Not found");
      return;
    }
    response.writeHead(200, {
      "Cache-Control": path.basename(filePath) === "index.html" ? "no-store" : "public, max-age=31536000, immutable",
      "Content-Type": MIME_TYPES.get(path.extname(filePath).toLowerCase()) || "application/octet-stream",
      "X-Content-Type-Options": "nosniff"
    });
    createReadStream(filePath).pipe(response);
  });

  for (const port of DESKTOP_PORTS) {
    try {
      await listen(staticServer, port);
      return `http://127.0.0.1:${port}`;
    } catch (error) {
      if (error?.code !== "EADDRINUSE") throw error;
    }
  }
  throw new Error("Lumen could not reserve a local desktop UI port.");
}

function showMainWindow() {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

async function openChromeExtensions() {
  if (process.platform === "win32") {
    const candidates = [
      process.env.PROGRAMFILES && path.join(process.env.PROGRAMFILES, "Google", "Chrome", "Application", "chrome.exe"),
      process.env["PROGRAMFILES(X86)"] && path.join(process.env["PROGRAMFILES(X86)"], "Google", "Chrome", "Application", "chrome.exe"),
      process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "Google", "Chrome", "Application", "chrome.exe")
    ].filter(Boolean);
    const executable = candidates.find((candidate) => existsSync(candidate));
    if (executable) {
      const chrome = spawn(executable, ["chrome://extensions/"], {
        detached: true,
        stdio: "ignore",
        windowsHide: false
      });
      chrome.unref();
      return true;
    }
  }
  if (process.platform === "darwin") {
    // macOS opens chrome:// only when Chrome is named: no app claims that scheme.
    const chrome = spawn("/usr/bin/open", ["-a", "Google Chrome", "chrome://extensions/"], { detached: true, stdio: "ignore" });
    chrome.unref();
    return true;
  }
  await shell.openExternal("chrome://extensions/");
  return true;
}

async function createMainWindow() {
  const iconPath = path.join(DESKTOP_DIR, "build", "icon.png");
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    title: "Lumen",
    autoHideMenuBar: true,
    backgroundColor: "#0b0d0c",
    icon: existsSync(iconPath) ? iconPath : undefined,
    webPreferences: {
      preload: path.join(DESKTOP_DIR, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });
  mainWindow.setMenuBarVisibility(false);

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://") || url.startsWith("http://")) void shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    const current = mainWindow?.webContents.getURL() || "";
    if (current && new URL(url).origin !== new URL(current).origin) {
      event.preventDefault();
      if (url.startsWith("https://") || url.startsWith("http://")) void shell.openExternal(url);
    }
  });
  mainWindow.once("ready-to-show", () => mainWindow?.show());
  mainWindow.on("close", (event) => {
    if (!quitting) {
      event.preventDefault();
      mainWindow?.hide();
    }
  });

  const appUrl = await startStaticServer();
  await mainWindow.loadURL(appUrl);
}

function createTray() {
  const iconPath = path.join(DESKTOP_DIR, "build", "icon.png");
  const icon = existsSync(iconPath) ? nativeImage.createFromPath(iconPath).resize({ width: 20, height: 20 }) : nativeImage.createEmpty();
  tray = new Tray(icon);
  tray.setToolTip("Lumen");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Open Lumen", click: showMainWindow },
    {
      label: "Restart runtime",
      click: async () => {
        await restartBackend();
        showMainWindow();
      }
    },
    { label: "Open data folder", click: () => void shell.openPath(app.getPath("userData")) },
    { type: "separator" },
    {
      label: "Quit",
      click: () => {
        quitting = true;
        app.quit();
      }
    }
  ]));
  tray.on("double-click", showMainWindow);
}

function registerIpc() {
  ipcMain.handle("desktop:get-status", desktopStatus);
  ipcMain.handle("desktop:start-runtime", async () => {
    await startBackend();
    return await desktopStatus();
  });
  ipcMain.handle("desktop:restart-runtime", async () => {
    await restartBackend();
    return await desktopStatus();
  });
  ipcMain.handle("desktop:open-chrome-extensions", async () => {
    return await openChromeExtensions();
  });
  ipcMain.handle("desktop:open-data-folder", async () => {
    ensureRuntimeDirectories();
    const error = await shell.openPath(app.getPath("userData"));
    return { ok: !error, error };
  });
  ipcMain.handle("desktop:set-computer-use-enabled", async (_event, enabled) => {
    const result = await postJson(
      `${API_ORIGIN}/operator/desktop/control?enabled=${enabled === true ? "true" : "false"}`
    );
    return result ?? { available: false, enabled: false };
  });
}

const singleInstance = app.requestSingleInstanceLock();
if (!singleInstance) {
  app.quit();
} else {
  app.on("second-instance", showMainWindow);
  app.whenReady().then(async () => {
    ensureRuntimeDirectories();
    registerIpc();
    await startBackend();
    startRuntimeSupervisor();
    await createMainWindow();
    createTray();
    startControlIndicatorSupervisor();
  }).catch(() => app.quit());
}

app.on("activate", showMainWindow);
app.on("before-quit", () => {
  quitting = true;
  if (runtimeSupervisor) {
    clearInterval(runtimeSupervisor);
    runtimeSupervisor = null;
  }
  if (controlIndicatorSupervisor) {
    clearInterval(controlIndicatorSupervisor);
    controlIndicatorSupervisor = null;
  }
  if (pointerSupervisor) {
    clearInterval(pointerSupervisor);
    pointerSupervisor = null;
  }
  controlIndicatorVisible = false;
  closeControlIndicatorWindows();
  stopBackend();
  staticServer?.close();
});
app.on("window-all-closed", () => {
  // Lumen remains available in the tray. The explicit Quit action terminates the runtime.
});
