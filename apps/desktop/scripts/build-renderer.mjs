import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const rendererDir = path.resolve(desktopDir, "..", "renderer");
const result = spawnSync("npm", ["run", "build"], {
  cwd: rendererDir,
  env: {
    ...process.env,
    LUMEN_DESKTOP_BUILD: "1",
    NEXT_PUBLIC_API_URL: "http://127.0.0.1:8001"
  },
  stdio: "inherit",
  shell: process.platform === "win32"
});
if (result.error) console.error(result.error);
process.exit(result.status ?? 1);
