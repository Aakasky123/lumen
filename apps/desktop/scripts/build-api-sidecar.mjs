import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const python = process.env.LUMEN_PYTHON || (process.platform === "win32" ? "python.exe" : "python3");

const version = spawnSync(python, ["-m", "PyInstaller", "--version"], {
  cwd: desktopDir,
  encoding: "utf8"
});
if (version.status !== 0) {
  console.error("PyInstaller is required for desktop packaging.");
  console.error(`Install it with: ${python} -m pip install -r apps/desktop/requirements-build.txt`);
  process.exit(1);
}

const args = [
  "-m",
  "PyInstaller",
  "--noconfirm",
  "--distpath",
  "binaries",
  "--workpath",
  ".pyinstaller",
  "lumen-api.spec"
];
if (process.env.LUMEN_CLEAN_DESKTOP_BUILD === "1") args.splice(3, 0, "--clean");

const result = spawnSync(
  python,
  args,
  { cwd: desktopDir, stdio: "inherit" }
);
process.exit(result.status ?? 1);
