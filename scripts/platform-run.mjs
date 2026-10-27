// Runs scripts/<name>.ps1 on Windows and scripts/<name>.sh everywhere else, so one npm script
// (`npm run dev`, `npm run restart`) starts the dev stack on both platforms.
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptsDir = path.dirname(fileURLToPath(import.meta.url));
const name = process.argv[2];
if (!name) {
  console.error("usage: node scripts/platform-run.mjs <script-name> [args...]");
  process.exit(2);
}
const args = process.argv.slice(3);

const result =
  process.platform === "win32"
    ? spawnSync(
        "powershell",
        ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path.join(scriptsDir, `${name}.ps1`), ...args],
        { stdio: "inherit" }
      )
    : spawnSync("bash", [path.join(scriptsDir, `${name}.sh`), ...args], { stdio: "inherit" });

if (result.error) console.error(result.error);
process.exit(result.status ?? 1);
