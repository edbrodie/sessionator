"""Shared extraction engine for the per-harness adapters.

The deterministic field extraction (keywords, commits, PRs, repo/branch, files,
tests, resolved, the capped excerpt) is identical across harnesses — Claude and
Codex differ only in transcript *shape*, not in what a "commit" or a "test run"
looks like once seen. That common logic is the proven walker ported from
``session_report_lib.py`` and reorganized here as a ``Walker`` the two adapters
drive with harness-specific line loops. Report/markdown rendering and the old
store shape are dropped.

Every text value entering the walker is passed through ``strip_private`` at the
point of ingestion, so keywords, the excerpt, and everything downstream are free
of ``<private>`` spans (T-003).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ..privacy import strip_private

# --- Curated keyword dictionary (deterministic, order-preserving) ----------
KEYWORDS = [
    "docker", "kubernetes", "k8s", "terraform", "ansible", "helm", "aws", "gcp",
    "vercel", "netlify", "nginx", "postgres", "postgresql", "mysql", "sqlite",
    "redis", "mongodb", "graphql", "rest", "grpc", "webhook", "oauth", "jwt",
    "auth", "migration", "cron", "launchd", "systemd", "python", "typescript",
    "javascript", "react", "nextjs", "node", "rust", "go", "tailwind", "css",
    "eslint", "prettier", "pytest", "jest", "playwright", "puppeteer", "threejs",
    "webgl", "shader", "glsl", "r3f", "framer", "gsap", "animation", "linear",
    "telegram", "granola", "affinity", "slack", "github", "git", "seo", "geo",
    "palette", "dashboard", "chart", "dataviz", "prisma", "drizzle", "supabase",
    "firebase", "sentry", "datadog", "grafana", "prometheus", "otel", "telemetry",
    "ffmpeg", "pdf", "mcp", "plugin", "worktree", "codex", "hook",
]
KEYWORD_CAP = 15
GO_TEXT_RX = re.compile(r"go\.mod|golang|goroutine", re.IGNORECASE)
REST_TEXT_RX = re.compile(r"restful|REST API|rest_framework", re.IGNORECASE)

COMMIT_RX = re.compile(r"\[[\w./-]+\s+([0-9a-f]{7,40})\]\s+(.+)")
PR_RX = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
PUSH_RX = re.compile(r"\bgit push\b")
GIT_OP_RX = re.compile(r"\bgit push\b|\bgh pr (?:create|merge)\b")
GITISH_RX = re.compile(r"\bgit\b|\bgh\b")
RM_RX = re.compile(r"\brm\b|\bgit rm\b")

REPO_RX = re.compile(
    r"github\.com[:/]([\w.-]+)/([\w][\w.-]*?)(?:\.git)?(?=[/\s\"']|$)"
)
BRANCH_RXS = [
    re.compile(r"git\s+checkout\s+-b\s+([\w./][\w./-]*)"),
    re.compile(r"git\s+switch\s+-c\s+([\w./][\w./-]*)"),
    re.compile(r"git\s+checkout\s+([\w./][\w./-]*)"),
    re.compile(r"git\s+switch\s+([\w./][\w./-]*)"),
    re.compile(r"git\s+push\s+\S+\s+([\w./][\w./-]*)"),
    re.compile(r"--head\s+([\w./][\w./-]*)"),
    re.compile(r"Switched to (?:a new )?branch\s+['\"]?([\w./][\w./-]*)"),
    re.compile(r"On branch\s+([\w./][\w./-]*)"),
]
BRANCH_REJECT = {"--", "HEAD", "head", "origin", "."}

OPEN_RX = re.compile(
    r"left off|pending|awaiting|next session|\bTODO\b|blocked|on your word|"
    r"awaiting (commit|approval|permission)", re.IGNORECASE)
DONE_RX = re.compile(
    r"all tests pass|tests green|✅|\bdone\b|verified|merged|committed|pushed|"
    r"complete", re.IGNORECASE)
PERM_RX = re.compile(
    r"(shall|should|may|can|would|do)\s+(i|you)\b|permission|let me know|"
    r"awaiting your|could ?n.?t find|missing file|not found|want me to|"
    r"which .{0,40}would you", re.IGNORECASE)

TEST_CMD_RX = re.compile(
    r"\b(pytest|jest|vitest(?:\s+run)?|mocha|go\s+test|cargo\s+test|"
    r"npm\s+(?:run\s+)?test|yarn\s+test|pnpm\s+(?:run\s+)?test|npm\s+run\s+build|"
    r"yarn\s+build|pnpm\s+build|next\s+build|tsc\b|playwright\s+test|"
    r"make\s+test\b|python3?\s+(?:\S*/)?test[\w.]*\.py)\b",
    re.IGNORECASE)
TEST_FAIL_RX = re.compile(r"(\d+)\s+fail(?:ed|ing|s)?\b", re.IGNORECASE)
TEST_PASS_RX = re.compile(r"(\d+)\s+pass(?:ed|ing|es)?\b", re.IGNORECASE)
TEST_FAILWORD_RX = re.compile(r"\bFAIL(?:ED)?\b|✗|✘|\bTraceback\b|\bError:", re.IGNORECASE)
TEST_OKWORD_RX = re.compile(r"\ball tests? pass|\bPASS\b|✓|\bok\b|\bsucce", re.IGNORECASE)

# Sentinel line every sessionator summarizer prompt opens with. The prose
# openers below are ^-anchored and so never matched the real prompt once its
# wording drifted; an explicit, unanchored sentinel cannot drift and survives a
# harness wrapping the prompt in its own preamble. Both prompt builders in
# summarize.py emit it as their first line.
SUMMARIZER_SENTINEL = "@@SESSIONATOR-SUMMARIZER@@"

# First genuine human turn signatures of sessionator's OWN summarizer calls
# (self-pollution guard — T-007 rule 3). The sentinel is matched anywhere in the
# turn; the batch marker and the legacy prompt openers stay for transcripts
# recorded before the sentinel existed.
SUMMARIZER_PROMPT_RX = re.compile(
    re.escape(SUMMARIZER_SENTINEL)
    + r"|^\s*@@S\d+@@"
    r"|^\s*(?:You\s+)?refine bullets for\b"
    r"|^\s*You (?:refine|summari[sz]e) \w+ .*session summaries\b"
    r"|^\s*Summari[sz]e this Claude Code session transcript\b",
    re.IGNORECASE)

EXCERPT_LIMIT = 36000

_PATCH_OP = {"add": "C", "update": "M", "delete": "D"}


def parse_ts(s):
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def mcp_label(name):
    """``mcp__plugin_chrome-devtools-mcp_chrome-devtools__take_screenshot`` ->
    ``chrome-devtools``."""
    rest = name[len("mcp__"):]
    server = rest.split("__", 1)[0]
    if server.startswith("plugin_"):
        server = server[len("plugin_"):]
        parts = server.split("_", 1)
        return parts[1] if len(parts) > 1 else parts[0]
    return server


def parse_rm_paths(cmd):
    """Best-effort file paths from an ``rm`` / ``git rm`` command."""
    paths = []
    for seg in re.split(r"&&|\|\||;|\|", cmd):
        seg = seg.strip()
        m = re.match(r"(?:git\s+)?rm\b(.*)", seg)
        if not m:
            continue
        for tok in m.group(1).split():
            if tok.startswith("-"):
                continue
            if any(ch in tok for ch in ("$", "*", "`", "<", ">", "{", "}", "(", ")")):
                continue
            tok = tok.strip("'\"")
            if tok:
                paths.append(tok)
    return paths


def parse_test_result(cmd, out):
    """Best-effort test/build result from a command + its output, or None when
    the COMMAND is not a test/build runner (output-only markers never count)."""
    if not TEST_CMD_RX.search(cmd or ""):
        return None
    if not isinstance(out, str) or not out:
        return {"text": "ran, no output", "broken": False}
    fail_m = TEST_FAIL_RX.search(out)
    pass_m = TEST_PASS_RX.search(out)
    has_failword = bool(TEST_FAILWORD_RX.search(out))
    has_okword = bool(TEST_OKWORD_RX.search(out))
    n_fail = int(fail_m.group(1)) if fail_m else 0
    n_pass = int(pass_m.group(1)) if pass_m else 0
    broken = n_fail > 0 or (has_failword and n_pass == 0 and not has_okword)
    if n_fail and n_pass:
        text = f"{n_pass} passed, {n_fail} failed"
    elif n_fail:
        text = f"{n_fail} failed"
    elif n_pass:
        text = f"{n_pass} passed"
    elif broken:
        text = "failed"
    else:
        text = "passed" if has_okword else "ran"
    return {"text": text, "broken": broken}


class Walker:
    """Accumulates the deterministic record fields as an adapter feeds it the
    events of one transcript. All harness-shared harvesting lives here; the
    adapters only translate their format into ``add_*`` / ``set_*`` calls."""

    def __init__(self):
        self.cwd = None
        self.turns = []          # ordered ("USER"/"ASSISTANT", text)
        self.boundaries = []     # cut points seen in the transcript itself
        self.n_user = 0
        self.first_ts = None
        self.last_ts = None
        self.model = None
        self.parse_warnings = 0

        self.commit_subj = {}
        self.commit_order = []
        self.pr_urls = []
        self._pr_seen = set()
        self._git_op_ids = set()
        self._cmd_by_id = {}     # tool/call id -> command string
        self.skills = []
        self._skills_seen = set()
        self.sub_counts = {}
        self.sub_order = []
        self.mcp_labels = []
        self._mcp_seen = set()
        self.raw_files = []      # (op, path)
        self.read_paths = set()
        self._kw_parts = []
        self._repo_slug = None
        self._repo_prose = None
        self.branch = None

        self._cur_ev = 0
        self._last_write_ev = None
        self._last_commit_ev = None
        self.last_todos = None
        self.tests_state = None

    # --- lifecycle --------------------------------------------------------
    def tick(self):
        self._cur_ev += 1

    def note_warning(self):
        self.parse_warnings += 1

    def mark_boundary(self, event, trigger=None):
        """Note a cut point at the current turn ordinal — a compaction marker
        the transcript carries. For a session no hook ever saw, this is the only
        record of where its history was dropped, and so of where a summary
        segment should end. Repeated markers at the same ordinal collapse."""
        turn = len(self.turns)
        if turn <= 0:
            return
        if self.boundaries and self.boundaries[-1]["turn"] == turn:
            return
        self.boundaries.append({"event": event, "trigger": trigger, "turn": turn})

    def set_cwd(self, cwd):
        if self.cwd is None and isinstance(cwd, str) and cwd:
            self.cwd = cwd

    # --- turns ------------------------------------------------------------
    def add_user(self, text, ts=None):
        text = strip_private(text)
        if not isinstance(text, str):
            return
        text = text.strip()
        if not text or text.startswith("<"):
            return
        self.turns.append(("USER", text))
        self._kw_parts.append(text)
        self.n_user += 1
        if ts:
            if self.first_ts is None:
                self.first_ts = ts
            self.last_ts = ts

    def add_assistant(self, text):
        text = strip_private(text)
        if not isinstance(text, str):
            return
        text = text.strip()
        if not text:
            return
        self.turns.append(("ASSISTANT", text))
        self._kw_parts.append(text)

    def set_model(self, mdl):
        # "<synthetic>" marks harness-injected placeholders (API-error retries).
        if isinstance(mdl, str) and mdl and not mdl.startswith("<"):
            self.model = mdl

    # --- tool activity ----------------------------------------------------
    def add_command(self, cmd, call_id=None):
        """A shell command (Claude Bash / Codex exec_command)."""
        cmd = strip_private(cmd)
        if not isinstance(cmd, str) or not cmd:
            return
        self._kw_parts.append(cmd)
        self._harvest_repo(cmd, strong=True)
        self._harvest_branch(cmd)
        if call_id:
            self._cmd_by_id[call_id] = cmd
        if GIT_OP_RX.search(cmd):
            if call_id:
                self._git_op_ids.add(call_id)
            self._harvest_prs(cmd)
        if RM_RX.search(cmd):
            for p in parse_rm_paths(cmd):
                self.raw_files.append(("D", p))

    def add_command_output(self, out, call_id=None):
        """The output of a previously-seen command."""
        out = strip_private(out)
        if not isinstance(out, str) or not out:
            return
        cmd = self._cmd_by_id.get(call_id)
        gitish = bool(cmd and GITISH_RX.search(cmd))
        self._scan_text(out)
        self._harvest_repo(out, strong=gitish)
        self._harvest_branch(out)
        if call_id in self._git_op_ids:
            self._harvest_prs(out)
        if cmd is not None:
            tr = parse_test_result(cmd, out)
            if tr is not None:
                tr["ev"] = self._cur_ev
                self.tests_state = tr

    def add_text(self, text):
        """Free assistant/tool text scanned for commits, repos (weak)."""
        text = strip_private(text)
        self._scan_text(text)
        self._harvest_repo(text)

    def add_file(self, op, path):
        if not isinstance(path, str) or not path:
            return
        self.raw_files.append((op, path))
        if op in ("C", "M") and self.cwd and path.startswith(self.cwd.rstrip("/") + "/"):
            self._last_write_ev = self._cur_ev

    def add_patch_changes(self, changes):
        """Codex patch_apply_end changes dict -> file ops."""
        if not isinstance(changes, dict):
            return
        for path, info in changes.items():
            ctype = info.get("type") if isinstance(info, dict) else None
            op = _PATCH_OP.get(ctype)
            if op:
                self.add_file(op, path)

    def add_read(self, path):
        if isinstance(path, str) and path:
            self.read_paths.add(path)

    def add_skill(self, name):
        if isinstance(name, str) and name and name not in self._skills_seen:
            self._skills_seen.add(name)
            self.skills.append(name)

    def add_subagent(self, st):
        if isinstance(st, str) and st:
            if st not in self.sub_counts:
                self.sub_counts[st] = 0
                self.sub_order.append(st)
            self.sub_counts[st] += 1

    def add_mcp(self, label):
        if isinstance(label, str) and label and label not in self._mcp_seen:
            self._mcp_seen.add(label)
            self.mcp_labels.append(label)

    def set_todos(self, todos):
        if isinstance(todos, list):
            self.last_todos = todos

    # --- harvest helpers --------------------------------------------------
    def _scan_text(self, txt):
        if not isinstance(txt, str) or not txt:
            return
        for m in COMMIT_RX.finditer(txt):
            sha = m.group(1)
            subj = m.group(2).strip()
            if len(subj) > 70:
                subj = subj[:70].rstrip() + "…"
            if sha not in self.commit_subj:
                self.commit_subj[sha] = subj
                self.commit_order.append(sha)
                self._last_commit_ev = self._cur_ev

    def _harvest_prs(self, txt):
        if not isinstance(txt, str) or not txt:
            return
        for m in PR_RX.finditer(txt):
            url = m.group(0)
            if url not in self._pr_seen:
                self._pr_seen.add(url)
                self.pr_urls.append(url)

    def _harvest_repo(self, txt, strong=False):
        if self._repo_slug is not None:
            return
        if not strong and self._repo_prose is not None:
            return
        if not isinstance(txt, str) or not txt:
            return
        m = REPO_RX.search(txt)
        if m:
            owner, repo = m.group(1), m.group(2).rstrip(".-")
            if owner and repo and repo not in ("pull", "tree", "blob"):
                slug = f"{owner}/{repo}"
                if strong:
                    self._repo_slug = slug
                else:
                    self._repo_prose = slug

    def _harvest_branch(self, txt):
        if self.branch is not None or not isinstance(txt, str) or not txt:
            return
        for rx in BRANCH_RXS:
            m = rx.search(txt)
            if m:
                b = m.group(1)
                if b and b not in BRANCH_REJECT and any(ch.isalnum() for ch in b):
                    self.branch = b
                    return

    # --- finalize ---------------------------------------------------------
    def is_summarizer_pollution(self):
        """True if the first genuine human turn is one of sessionator's own
        summarizer batch prompts (self-pollution)."""
        for role, text in self.turns:
            if role != "USER":
                continue
            t = text.strip()
            if not t or t.startswith("/") or t.startswith("<"):
                continue
            if t.startswith("Caveat:") or t.startswith("[Request interrupted"):
                continue
            return bool(SUMMARIZER_PROMPT_RX.search(t))
        return False

    def finish(self, *, fallback_day=None):
        """Assemble the deterministic fields into a plain dict the adapter turns
        into a Record. Returns None when there is no genuine human input."""
        if self.n_user == 0:
            return None
        if self.is_summarizer_pollution():
            return None

        cwd = self.cwd or ""

        def rel(path):
            if cwd and isinstance(path, str) and path.startswith(cwd.rstrip("/") + "/"):
                return path[len(cwd.rstrip("/")) + 1:]
            return path

        # Files manifest: dedup by displayed path, precedence C > M > D.
        rank = {"C": 3, "M": 2, "D": 1}
        file_ops = {}
        file_order = []
        for op, p in self.raw_files:
            disp = rel(p)
            if not disp:
                continue
            if disp in file_ops:
                if rank[op] > rank[file_ops[disp]]:
                    file_ops[disp] = op
            else:
                file_ops[disp] = op
                file_order.append(disp)
        files = [[file_ops[d], d] for d in file_order]

        # Keywords: dictionary match, order preserved, cap; go/rest guarded.
        kw_text = "\n".join(self._kw_parts)
        keywords = []
        if kw_text:
            has_go_file = any(
                isinstance(p, str) and p.lower().endswith(".go")
                for _op, p in self.raw_files
            )
            go_ok = bool(has_go_file or GO_TEXT_RX.search(kw_text))
            rest_ok = bool(REST_TEXT_RX.search(kw_text))
            for term in KEYWORDS:
                if term == "go":
                    matched = go_ok
                elif term == "rest":
                    matched = rest_ok
                else:
                    matched = bool(
                        re.search(r"\b" + re.escape(term) + r"\b", kw_text, re.IGNORECASE)
                    )
                if matched:
                    keywords.append(term)
                    if len(keywords) >= KEYWORD_CAP:
                        break

        # Branch fallback: worktree dir name from cwd.
        branch = self.branch
        if branch is None and cwd:
            m = re.search(r"\.worktrees/([\w.-]+)", cwd) or re.search(
                r"--worktrees-([\w.-]+)", cwd)
            if m:
                branch = m.group(1)

        # Excerpt (capped, private already stripped at ingest). The untrimmed
        # text is returned alongside it: a segment sidecar is cut from the real
        # turns, not from a 36k middle-trim of them.
        excerpt_full = "\n\n".join(f"{role}: {text}" for role, text in self.turns)
        excerpt = excerpt_full
        if len(excerpt) > EXCERPT_LIMIT:
            excerpt = excerpt[:24000] + "\n...[trimmed]...\n" + excerpt[-12000:]

        # Resolved: deterministic trust flag from the transcript tail.
        tail4k = excerpt[-4000:]
        final_is_perm = False
        if self.turns:
            lrole, ltext = self.turns[-1]
            if lrole == "ASSISTANT" and PERM_RX.search(ltext or ""):
                final_is_perm = True
        if OPEN_RX.search(tail4k) or final_is_perm:
            resolved = "open"
        elif DONE_RX.search(tail4k):
            resolved = "done"
        else:
            resolved = "unknown"

        # open_todos: last todo/plan, non-completed entries.
        open_todos = []
        if isinstance(self.last_todos, list):
            for td in self.last_todos:
                if isinstance(td, dict) and td.get("status") != "completed":
                    c = td.get("content")
                    if isinstance(c, str) and c.strip():
                        open_todos.append(c.strip())

        tests = None
        if isinstance(self.tests_state, dict):
            tests = {
                "text": self.tests_state.get("text"),
                "broken": bool(self.tests_state.get("broken")),
            }

        # Dates.
        first_dt = _to_local(parse_ts(self.first_ts))
        last_dt = _to_local(parse_ts(self.last_ts))
        date = first_dt.strftime("%Y-%m-%d") if first_dt else (fallback_day or "")
        last_active = last_dt.isoformat() if last_dt else ""

        return {
            "cwd": cwd,
            "model": self.model,
            "date": date,
            "last_active": last_active,
            "repo": self._repo_slug or self._repo_prose,
            "branch": branch,
            "files": files,
            "commits": [[sha, self.commit_subj[sha]] for sha in self.commit_order],
            "prs": list(self.pr_urls),
            "keywords": keywords,
            "skills": list(self.skills),
            "subagents": [[st, self.sub_counts[st]] for st in self.sub_order],
            "mcp": list(self.mcp_labels),
            "open_todos": open_todos,
            "tests": tests,
            "resolved": resolved,
            "excerpt": excerpt,
            "excerpt_full": excerpt_full,
            "turn_count": len(self.turns),
            "boundaries": list(self.boundaries),
            "parse_warnings": self.parse_warnings,
        }


def _to_local(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def iter_jsonl(path, walker):
    """Yield parsed dict objects from a JSONL file, counting malformed lines as
    parse warnings on the walker (T-007 rule 1) and skipping them."""
    with open(path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                walker.note_warning()
                continue
            if isinstance(obj, dict):
                yield obj
