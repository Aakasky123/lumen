const { contextBridge, ipcRenderer } = require("electron");

const invoke = (channel) => (...args) => ipcRenderer.invoke(channel, ...args);

contextBridge.exposeInMainWorld("lumenDesktop", Object.freeze({
  isDesktop: true,
  getStatus: invoke("desktop:get-status"),
  startRuntime: invoke("desktop:start-runtime"),
  restartRuntime: invoke("desktop:restart-runtime"),
  setComputerUseEnabled: invoke("desktop:set-computer-use-enabled"),
  openChromeExtensions: invoke("desktop:open-chrome-extensions"),
  openDataFolder: invoke("desktop:open-data-folder")
}));
