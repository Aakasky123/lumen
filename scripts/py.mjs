// Runs a Python command with the first interpreter that exists: the repo .venv (POSIX or Windows layout),
// then python3, then python. `node scripts/py.mjs -m pytest ...` stands in for a bare `python`, which
// macOS does not ship.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// A git worktree of the repo (.claude/worktrees/<name>) has no .venv of its own: look in each parent too.
const candidates = [];
for (let dir = root; ; dir = path.dirname(dir)) {
  candidates.push(path.join(dir, ".venv", "bin", "python"), path.join(dir, ".venv", "Scripts", "python.exe"));
  if (path.dirname(dir) === dir) break;
}
candidates.push("python3", "python");
const args = process.argv.slice(2);
for (const candidate of candidates) {
  if (path.isAbsolute(candidate) && !existsSync(candidate)) continue;
  const result = spawnSync(candidate, args, { stdio: "inherit", cwd: process.cwd() });
  if (result.error && result.error.code === "ENOENT") continue;
  process.exit(result.status ?? 1);
}
console.error("No Python interpreter found (.venv, python3, python).");
process.exit(127);
