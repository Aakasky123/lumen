import { spawn, spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import electron from "electron";

const desktopDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const rendererBuild = spawnSync("npm", ["run", "build:renderer"], {
  cwd: desktopDir,
  stdio: "inherit",
  shell: process.platform === "win32"
});
if (rendererBuild.error) console.error(rendererBuild.error);
if (rendererBuild.status !== 0) process.exit(rendererBuild.status ?? 1);

let desktop;
try {
  desktop = spawn(electron, ["."], {
    cwd: desktopDir,
    stdio: "inherit"
  });
} catch (error) {
  console.error(error);
  process.exit(1);
}

const shutdown = () => {
  desktop.kill();
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

desktop.on("exit", (code) => {
  process.exit(code ?? 0);
});
