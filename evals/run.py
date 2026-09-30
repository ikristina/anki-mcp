"""Eval runner: send each case's prompt through `claude -p`, capture the tool calls, score them.

Each run gets a fresh temp workdir holding a copy of .claude/skills (with `model:` rewritten to the model under test)
and an mcp.json with only the two fakes: `anki` (evals/fake_server.py, FakeAnki) and `obsidian` (evals/fake_obsidian.py).
Nothing reaches the real collection or vault.

  uv run python evals/run.py --models haiku sonnet --repeats 5 [--case es-batch] [--skill language-cards] [--jobs 4]
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"
EXPECTED_SERVERS = {"anki", "obsidian"}
REPO_SKILLS = {"flashcards", "language-cards"}
# Skills bundled with Claude Code 2.1.x, seen in probe runs' init events (plugin-authoring appeared in 2.1.286). Anything else (user skills, plugin skills)
# would change model behavior, so it fails the isolation check. Extend this when Claude Code ships new built-ins.
BUILTIN_SKILLS = {"deep-research", "design", "design-sync", "dataviz", "update-config", "verify", "debug", "code-review",
                  "simplify", "batch", "fewer-permission-prompts", "doctor", "loop", "schedule", "claude-api",
                  "workflow-authoring", "run", "run-skill-generator", "plugin-authoring"}


@dataclass
class Call:
    name: str  # without the mcp__<server>__ prefix for MCP tools
    server: str | None
    input: dict
    result: str = ""
    is_error: bool = False


@dataclass
class Run:
    calls: list[Call] = field(default_factory=list)
    text: str = ""  # all assistant text, joined
    init: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)

    def tool(self, name: str, server: str | None = None) -> list[Call]:
        return [c for c in self.calls if c.name == name and (server is None or c.server == server)]


def parse(lines) -> Run:
    """Stream-json events -> Run. Tolerates non-JSON lines (e.g. stderr noise)."""
    run, by_id = Run(), {}
    texts = []
    for line in lines:
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            run.init = ev
        elif kind == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    m = re.fullmatch(r"mcp__(.+?)__(.+)", block["name"])
                    call = Call(m[2] if m else block["name"], m[1] if m else None, block.get("input") or {})
                    run.calls.append(call)
                    by_id[block["id"]] = call
                elif block.get("type") == "text":
                    texts.append(block["text"])
        elif kind == "user":
            content = ev.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in by_id:
                    c = by_id[block["tool_use_id"]]
                    body = block.get("content")
                    c.result = body if isinstance(body, str) else "\n".join(b.get("text", "") for b in body or [])
                    c.is_error = bool(block.get("is_error"))
        elif kind == "result":
            run.result = ev
    run.text = "\n".join(texts)
    return run


def isolation_problems(run: Run) -> list[str]:
    """Why this run can't be trusted as isolated (empty list = fine)."""
    servers = {s["name"] for s in run.init.get("mcp_servers", [])}
    problems = []
    if servers != EXPECTED_SERVERS:
        problems.append(f"MCP servers {sorted(servers)} != {sorted(EXPECTED_SERVERS)}")
    if bad := [s["name"] for s in run.init.get("mcp_servers", []) if s.get("status") != "connected"]:
        problems.append(f"not connected: {bad}")
    if plugins := [p["name"] for p in run.init.get("plugins", []) if p.get("path") != "builtin"]:
        problems.append(f"non-builtin plugins loaded: {plugins}")
    skills = set(run.init.get("skills", []))
    if missing := REPO_SKILLS - skills:
        problems.append(f"repo skills missing: {sorted(missing)}")
    if extra := skills - REPO_SKILLS - BUILTIN_SKILLS:
        problems.append(f"unexpected skills: {sorted(extra)}")
    return problems


def workdir(model: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="anki-eval-"))
    shutil.copytree(ROOT / ".claude" / "skills", d / ".claude" / "skills")
    for skill in (d / ".claude" / "skills").glob("*/SKILL.md"):
        skill.write_text(re.sub(r"(?m)^model: .*$", f"model: {model}", skill.read_text()))
    uv = shutil.which("uv") or "uv"
    servers = {
        "anki": {"command": uv, "args": ["--directory", str(ROOT), "run", "python", "evals/fake_server.py"]},
        "obsidian": {"command": uv, "args": ["--directory", str(ROOT), "run", "python", "evals/fake_obsidian.py"],
                     "env": {"EVAL_OBSIDIAN_LOG": str(d / "obsidian.jsonl")}},
    }
    (d / "mcp.json").write_text(json.dumps({"mcpServers": servers}))
    return d


def claude(prompt: str, model: str, timeout: int = 600) -> tuple[Run, list[str]]:
    d = workdir(model)
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "stream-json", "--verbose",
           "--strict-mcp-config", "--mcp-config", str(d / "mcp.json"), "--setting-sources", "project",
           "--allowedTools", "mcp__anki__*", "mcp__obsidian__*", "Skill",
           "--disallowedTools", "Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch",
           "--no-session-persistence", "--max-budget-usd", "2"]
    try:
        out = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL).stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    lines = out.splitlines()
    shutil.rmtree(d, ignore_errors=True)
    return parse(lines), lines


def score(case: dict, run: Run) -> dict:
    checks = {}
    for name, fn in case["checks"]:
        try:
            checks[name] = bool(fn(run))
        except Exception as e:  # a check that crashes on unexpected args is a failed check, not a crashed suite
            checks[name] = False
            checks[f"{name} (crash: {type(e).__name__}: {e})"] = False
    return checks


def one(case: dict, model: str, rep: int) -> dict:
    t = time.time()
    run, lines = claude(case["prompt"], model)
    problems = isolation_problems(run)
    checks = {} if problems else score(case, run)
    return {
        "case": case["id"], "skill": case["skill"], "model": model, "rep": rep,
        "claude_code": run.init.get("claude_code_version"),
        "passed": not problems and bool(checks) and all(checks.values()),
        "checks": checks, "isolation": problems,
        "fake_gaps": [c.result for c in run.calls if "eval fake:" in c.result],
        "calls": [{"tool": f"{c.server + ':' if c.server else ''}{c.name}", "input": c.input, "is_error": c.is_error}
                  for c in run.calls],
        "cost_usd": run.result.get("total_cost_usd"), "turns": run.result.get("num_turns"),
        "duration_s": round(time.time() - t, 1), "final": run.result.get("result", "")[-2000:],
        "stream": lines,
    }


def main():
    sys.path.insert(0, str(ROOT / "evals"))
    from cases import CASES

    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["haiku"])
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--case", nargs="*", help="case ids (default: all)")
    ap.add_argument("--skill", help="only cases for this skill")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--rescore", type=Path, help="re-score a results .jsonl from its stored streams (no claude runs)")
    a = ap.parse_args()
    if a.rescore:
        return rescore(a.rescore, {c["id"]: c for c in CASES})

    cases = [c for c in CASES if (not a.case or c["id"] in a.case) and (not a.skill or c["skill"] == a.skill)]
    if not cases:
        sys.exit("no cases match")
    jobs = [(c, m, r) for m in a.models for c in cases for r in range(a.repeats)]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{'-'.join(a.models)}.jsonl"
    print(f"{len(jobs)} runs -> {out}", file=sys.stderr)

    rows = []
    with ThreadPoolExecutor(a.jobs) as pool, out.open("w") as f:
        for r in pool.map(lambda j: one(*j), jobs):
            rows.append(r)
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            failed = [k for k, v in r["checks"].items() if not v] + r["isolation"]
            print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['model']:7} {r['case']:22} #{r['rep']}  "
                  f"{r['turns']} turns  {r['duration_s']}s  {'; '.join(failed)}", file=sys.stderr)
    print(summary(rows))


def rescore(path: Path, cases: dict) -> None:
    """Apply the current checks to stored runs, so a fixed check never needs a re-run. Rewrites the file in place."""
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for r in rows:
        run = parse(r["stream"])
        r["isolation"] = isolation_problems(run)
        r["checks"] = {} if r["isolation"] else score(cases[r["case"]], run)
        r["passed"] = not r["isolation"] and bool(r["checks"]) and all(r["checks"].values())
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(summary(rows))


def summary(rows: list[dict]) -> str:
    models = sorted({r["model"] for r in rows}, key=[r["model"] for r in rows].index)
    case_ids = list(dict.fromkeys(r["case"] for r in rows))
    lines = ["| case | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
    for cid in case_ids:
        cells = []
        for m in models:
            rs = [r for r in rows if r["case"] == cid and r["model"] == m]
            cells.append(f"{sum(r['passed'] for r in rs)}/{len(rs)}" if rs else "")
        lines.append(f"| {cid} | " + " | ".join(cells) + " |")
    for label, key in [("mean cost $", "cost_usd"), ("mean turns", "turns"), ("mean time s", "duration_s")]:
        cells = []
        for m in models:
            vals = [r[key] for r in rows if r["model"] == m and r[key] is not None]
            cells.append(f"{sum(vals) / len(vals):.3g}" if vals else "")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    versions = sorted({str(r.get("claude_code")) for r in rows})
    lines.append(f"\nClaude Code {', '.join(versions)}" + ("  **WARNING: version changed during the run**" if len(versions) > 1 else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    main()
