import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
for (const script of ["generate:icons", "build:renderer", "build:api"]) {
  const result = spawnSync("npm", ["run", script], {
    cwd: desktopDir,
    stdio: "inherit",
    shell: process.platform === "win32"
  });
  if (result.error) console.error(result.error);
  if (result.status !== 0) process.exit(result.status ?? 1);
}
