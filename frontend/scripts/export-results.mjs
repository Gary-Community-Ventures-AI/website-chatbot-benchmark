import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const root = execFileSync("git", ["rev-parse", "--show-toplevel"], {
  cwd: frontend,
  encoding: "utf8",
}).trim();
const git = (...args) =>
  execFileSync("git", args, { cwd: root, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
const paths = git(
  "ls-tree",
  "-r",
  "--name-only",
  "-z",
  "HEAD",
  "--",
  "benchmarks",
)
  .split("\0")
  .filter(Boolean);
const projects = new Set();
const runs = [];
for (const path of paths) {
  const parts = path.split("/");
  if (
    parts.length < 3 ||
    parts.some((part) => part === "logs" || part.endsWith("-logs"))
  )
    continue;
  projects.add(parts[1]);
  if (!path.endsWith(".json")) continue;
  const report = JSON.parse(git("show", `HEAD:${path}`));
  if (!Array.isArray(report.results)) continue;
  const modified =
    Number(git("log", "-1", "--format=%ct", "HEAD", "--", path).trim()) * 1000;
  runs.push({ project: parts[1], filename: parts.at(-1), modified, report });
}
runs.sort((a, b) => b.modified - a.modified);
const destination = resolve(frontend, "public/data");
mkdirSync(destination, { recursive: true });
writeFileSync(
  resolve(destination, "runs.json"),
  JSON.stringify({
    projects: [...projects].sort(),
    runs,
    errors: [],
    commit: git("rev-parse", "HEAD").trim(),
  }),
);
console.log(
  `Exported ${runs.length} committed runs from ${projects.size} projects.`,
);
