// Shared by the server layout (inline boot script) and the client theme hook. Not a client module.

export const THEME_STORAGE_KEY = "lumen.theme";
export const DEFAULT_THEME = "dark";

/** Runs inline in <head> before first paint; keep it dependency-free and in sync with useTheme. */
export const THEME_BOOT_SCRIPT = `(function(){try{var p=localStorage.getItem("${THEME_STORAGE_KEY}")||"${DEFAULT_THEME}";var t=p==="system"?(matchMedia("(prefers-color-scheme: light)").matches?"light":"dark"):(p==="light"?"light":"dark");document.documentElement.dataset.theme=t;}catch(e){}})();`;
