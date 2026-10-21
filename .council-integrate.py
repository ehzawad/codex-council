"""One-use, base-checked source integration; removed from the resulting commit.

The authoring environment has no network-enabled checkout. This transformation
runs once on a feature branch, never main. It refuses a different base runner,
preserves its existing process supervisor, and produces ordinary source changes.
"""
import ast
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / 'plugins/codex-council/skills/codex-council/scripts'
BASE_BLOB = '2a9c1bb27e18693a86dd14998a8d3f599c1deb42'
blob = subprocess.check_output(['git', 'hash-object', str(SCRIPTS/'codex_council.py')], text=True).strip()
if blob != BASE_BLOB:
    raise SystemExit('Refusing to integrate against an unexpected runner blob: ' + blob)


class Source:
    def __init__(self, path):
        self.path = path
        self.text = path.read_text(encoding='utf-8')

    def replace(self, old, new, count=1):
        if self.text.count(old) != count:
            raise AssertionError(f'{self.path.name}: expected {count} matches for {old[:100]!r}; got {self.text.count(old)}')
        self.text = self.text.replace(old, new)
        return self

    def function(self, name, new=None, transform=None):
        nodes = [n for n in ast.parse(self.text).body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        if len(nodes) != 1:
            raise AssertionError(f'{self.path.name}: function {name} is not unique')
        node = nodes[0]
        lines = self.text.splitlines(keepends=True)
        old = ''.join(lines[node.lineno-1:node.end_lineno])
        value = transform(old) if transform else new
        self.text = ''.join(lines[:node.lineno-1]) + value.rstrip() + '\n' + ''.join(lines[node.end_lineno:])
        return self

    def save(self):
        ast.parse(self.text, filename=str(self.path))
        self.path.write_text(self.text, encoding='utf-8')


s = Source(SCRIPTS/'council_selection.py')
s.replace('    decision: SelectionDecision | None = None\n', '''    decision: SelectionDecision | None = None
    execution: dict | None = None
    workdir: str | None = None
    evidence_dir: str | None = None
    execution_overrides: tuple = ()
    task_id: str | None = None
    input_snapshot: str | None = None
''')
s.replace('def _resolve_run_selections(roles, run_dir, routing_mode, at_launch):',
          'def _resolve_run_selections(roles, run_dir, routing_mode, at_launch, project_root=None):')
s.replace('        launch = _discover(routing_mode)\n', '''        launch = (_discover(routing_mode) if project_root is None else
                  _discover(routing_mode, project_root=project_root))
''')
s.save()

s = Source(SCRIPTS/'council_discovery.py')
s.replace('def _execution_context(deadline=None):', 'def _execution_context(deadline=None, project_root=None):')
s.replace('"project_root": _project_root(deadline),',
          '"project_root": _project_root(deadline) if project_root is None else project_root,')
s.replace('"launch_cwd": os.getcwd(),',
          '"launch_cwd": os.getcwd() if project_root is None else project_root,')
s.function('_app_server_session', transform=lambda text: text.replace(
    'def _app_server_session(executable, deadline):',
    'def _app_server_session(executable, deadline, cwd=None, config_overrides=()):').replace(
    '[executable, "app-server", "--listen", "stdio://"],',
    '[executable, "app-server", "--listen", "stdio://",\n'
    '             *(arg for setting in config_overrides for arg in ("-c", setting))],').replace(
    'stderr=subprocess.PIPE, start_new_session=True,',
    'stderr=subprocess.PIPE, start_new_session=True,\n'
    '            **({"cwd": cwd} if cwd is not None else {}),'))
s.replace('def _observe_codex(deadline):', 'def _observe_codex(deadline, project_root=None):')
s.replace('    context = _execution_context(deadline)\n',
          '    context = (_execution_context(deadline) if project_root is None else\n'
          '               _execution_context(deadline, project_root=project_root))\n')
s.replace('        with _app_server_session(executable, deadline) as session:\n',
          '        with _app_server_session(executable, deadline,\n'
          '                                 **({"cwd": project_root} if project_root is not None else {})) as session:\n')
s.replace('def _discover(routing_mode):', 'def _discover(routing_mode, project_root=None):')
s.replace('        observed = _observe_codex(deadline)\n',
          '        observed = (_observe_codex(deadline) if project_root is None else\n'
          '                    _observe_codex(deadline, project_root=project_root))\n')
s.replace('def _discover_command(run_dir):', 'def _discover_command(run_dir, project_root=None):')
s.replace('    snapshot = _discover(_model_routing_mode())\n',
          '    snapshot = (_discover(_model_routing_mode()) if project_root is None else\n'
          '                _discover(_model_routing_mode(), project_root=project_root))\n')
s.save()

s = Source(SCRIPTS/'codex_council.py')
header = ast.parse(s.text).body[0]
assert isinstance(header, ast.Expr) and isinstance(header.value, ast.Constant)
lines = s.text.splitlines(keepends=True)
s.text = ''.join(lines[:header.lineno-1]) + '"""Adaptive Codex collaboration with captured inputs and explicit execution policy.\n\n' + (
    'Claude decides when a task-shaped contribution is useful. This runner captures\n'
    'current inputs into independent copies, resolves per-role model choices in the\n'
    'actual execution cwd, launches the existing detached supervisor, and records\n'
    'raw protocol evidence separately from collaborator interpretation. It neither\n'
    'judges substantive claims nor applies candidate changes to the live project.\n\n'
    'Prepared flow: --prepare, --discover, --check-staging-dir, --start; then\n'
    '--follow/--status and --freshness before reconciliation. --cancel and --reap\n'
    'retain their verified process-identity and supervisor-lock guarantees.\n'
    'Legacy direct --roles-file/--context-file remains available with read-only\n'
    'command defaults and without a captured-evidence contract.\n\n'
    'All retry paths require both a transient failure and replay safety. A role\n'
    'that may have performed tool work is never automatically replayed, including\n'
    'a stale-looking resume failure. Replies survive state-persistence failures.\n\n'
    'POSIX/Python 3.12+; standard-library only. A copied workspace is not a whole\n'
    'process sandbox, configuration inspection is not runtime attestation, and\n'
    'observed-stable capture is not atomic against arbitrary external writers.\n'
    'See DESIGN.md and references/invocation-contract.md for limits and protocol.\n'
    '"""\n') + ''.join(lines[header.end_lineno:])
s.replace('from dataclasses import dataclass\n', 'from dataclasses import dataclass\nfrom pathlib import Path\n')
s.replace('    _resolve_run_selections,\n', '')
s.replace('sys.dont_write_bytecode = _DONT_WRITE_BYTECODE\n', '''from council_workspace import WorkspaceError  # noqa: E402
from council_policy import command_options, parse_policy, policy_dict  # noqa: E402
from council_evidence import AttemptEvidence, write_result  # noqa: E402
from council_workflow import (  # noqa: E402
    prepare, load_plan, discover_prepared, resolve_selections, plan_lines,
    freshness_command,
)
sys.dont_write_bytecode = _DONT_WRITE_BYTECODE
''')
s.replace('SKILL_CONTRACT_EPOCH = 4\n', 'SKILL_CONTRACT_EPOCH = 5\n')
s.replace('_SUPERVISOR = {"lock_fd": None}\n', '''_SUPERVISOR = {"lock_fd": None}
# One owner, as for _RUN. CLI invocations are separate processes. Captured task
# identity is not the temporary execution cwd, and settled evidence survives an
# interruption without implying that unfinished contributions completed.
_WORKFLOW = {"plan": None, "run_dir": None, "settled": {}, "result": None}


def _logical_project_root():
    plan = _WORKFLOW["plan"]
    return plan["snapshot"]["source_root"] if plan else _project_root()


def _workflow_selection(roles, run_dir, routing_mode, at_launch, body, probe_policy=True):
    try:
        return resolve_selections(
            roles, run_dir, routing_mode, at_launch, body,
            probe_policy=probe_policy,
            on_progress=_RUN.publish if at_launch and _WORKFLOW["plan"] else None)
    except (OSError, ValueError) as exc:
        if at_launch and _WORKFLOW["plan"] is not None:
            _finish_evidence(state="refused")
            _RUN.finish("aborted", 2)
            _diag("[codex-council] runner aborted exit=2: captured invocation refused before workers")
        _usage_exit("invocation contract: " + _report_inline(exc)
                    + "; prepare a fresh run directory; never enable bypass to recover.")


def _finish_evidence(results=None, state="done"):
    if _WORKFLOW["plan"] is None:
        return None
    if results is None:
        results = list(_WORKFLOW["settled"].values())
    try:
        value = write_result(Path(_WORKFLOW["run_dir"]), _WORKFLOW["plan"],
                             results, execution_state=state)
        _WORKFLOW["result"] = value
        return value
    except (OSError, ValueError, TypeError) as exc:
        _diag("[codex-council] evidence incomplete: result.json could not be "
              "finalized (" + type(exc).__name__ + "); inspect per-attempt logs")
        return None
''')
s.function('_project_key', transform=lambda text: text.replace(
    'base = hashlib.sha256(_project_root().encode()).hexdigest()[:16]',
    'base = hashlib.sha256(_logical_project_root().encode()).hexdigest()[:16]\n'
    '    if _WORKFLOW["plan"] is not None:\n'
    '        base += "-" + hashlib.sha256(_WORKFLOW["plan"]["task_id"].encode()).hexdigest()[:16]'))
s.function('save_session', transform=lambda text: text.replace(
    '"project_path": _project_root(),', '"project_path": _logical_project_root(),').replace(
    '    session_key = _session_key()',
    '    if _WORKFLOW["plan"] is not None:\n'
    '        meta["task_id"] = _WORKFLOW["plan"]["task_id"]\n'
    '        meta["input_snapshot"] = _WORKFLOW["plan"]["snapshot"]["snapshot_id"]\n'
    '    session_key = _session_key()'))
s.function('_fresh_cmd', new='''def _fresh_cmd(root, model=None, effort=None, execution_options=None):
    # Policy switches are on the parent exec command, as are model overrides.
    # Bare direct CLI is now read-only, never implicitly unrestricted.
    options = (["--sandbox", "read-only", "-c", 'approval_policy="never"']
               if execution_options is None else execution_options)
    return ["codex", "exec", "-C", root, *_model_overrides(model, effort),
            *options, "--json", "--skip-git-repo-check", "-"]
''')
s.function('_resume_cmd', new='''def _resume_cmd(root, session_id, model=None, effort=None, execution_options=None):
    options = (["--sandbox", "read-only", "-c", 'approval_policy="never"']
               if execution_options is None else execution_options)
    return ["codex", "exec", "-C", root, *_model_overrides(model, effort),
            *options, "resume", session_id, "--skip-git-repo-check", "--json", "-"]
''')
s.function('_compose_prompt', new='''def _compose_prompt(role, body):
    """Current context wins over thread history; permissions are enforced in argv.

    A task-shaped collaborator can investigate, test, compare, or contribute an
    implementation. No fixed persona, debate round, or task-specific grader.
    """
    workspace = ""
    if role.workdir:
        workspace = (
            f"\\nCaptured task: {role.task_id}; input snapshot: {role.input_snapshot}.\\n"
            f"Your execution workspace is {role.workdir}. Use its relative paths. "
            "Historical absolute paths may identify the live project: do not use "
            "them as substitutes for captured inputs. Changes belong only in "
            "this experimental workspace. Do not edit another role's workspace "
            "or council control files. Never apply or publish changes to the live "
            "project. When empirical work would decide the question, establish "
            "a baseline, run a discriminating check, inspect its actual output, "
            "and report commands, results, changed artifacts and limitations. "
            "Never claim execution when a check was only proposed.\\n"
        )
    return (f"{role.instruction}\\n\\n{COLLABORATION_BRIEF}\\n{workspace}\\n"
            f"## Shared working context\\n\\n{body}\\n\\n{role.instruction}")
''')
s.replace('"independent cross-model check on Claude Code\'s work; you may be the "',
          '"independent cross-model collaborator advancing the current task; you may be the "')
s.replace('"questions."\n)', '''"questions. If nothing material falls in your lens, say so clearly. "
    "Thoroughness beats speed."
)''')
s.function('_validate_role_instruction', new='''def _validate_role_instruction(instruction, ctx):
    """Validate content, not magic prose; shared cadence is runner-generated."""
    if not instruction.strip():
        _roles_usage_exit(f"--roles-file {ctx}: instruction must be non-empty.")
    try:
        instruction.encode("utf-8")
    except UnicodeEncodeError:
        _roles_usage_exit(f"--roles-file {ctx}: instruction must be encodable UTF-8.")
''')
s.replace('OPTIONAL_ROLE_FIELDS = ("model", "effort", "selection")',
          'OPTIONAL_ROLE_FIELDS = ("model", "effort", "selection", "execution")')
s.replace('        roles.append(Role(rid, label, instruction, model, effort, selection))\n', '''        try:
            if "execution" in entry and not isinstance(entry["execution"], dict):
                raise WorkspaceError("execution must be an object when present")
            execution = policy_dict(entry["execution"]) if "execution" in entry else None
        except WorkspaceError as exc:
            _roles_usage_exit(f"--roles-file {ctx}: {exc}.")
        roles.append(Role(rid, label, instruction, model, effort, selection,
                          execution=execution))
''')
s.replace('async def _run_codex_subprocess(cmd, prompt, role_id=""):',
          'async def _run_codex_subprocess(cmd, prompt, role_id="", *, evidence=None, cwd=None):')
s.function('_run_codex_subprocess', transform=lambda text: text.replace(
    '        start_new_session=True,\n',
    '        start_new_session=True,\n        **({"cwd": cwd} if cwd is not None else {}),\n', 1).replace(
    '    _RUN.spawned(role_id, proc.pid, pgid)\n',
    '    _RUN.spawned(role_id, proc.pid, pgid)\n'
    '    if evidence is not None:\n        evidence.spawned(proc.pid, pgid)\n', 1).replace(
    '            buf.extend(chunk)\n',
    '            buf.extend(chunk)\n'
    '            if evidence is not None:\n'
    '                evidence.feed("stdout" if feed_scanner else "stderr", chunk)\n', 1))
s.function('_run_role_once', new='''async def _run_role_once(role, prompt, attempt):
    """Observe every invocation, then gate *all* retries on replay safety."""
    runs = []

    async def run_codex(cmd):
        observer = (AttemptEvidence(Path(role.evidence_dir), cmd, prompt)
                    if role.evidence_dir else None)
        run = None
        try:
            if observer is None:
                run = await _run_codex_subprocess(cmd, prompt, role_id=role.id)
            else:
                run = await _run_codex_subprocess(
                    cmd, prompt, role_id=role.id, evidence=observer, cwd=role.workdir)
            runs.append(run)
            return run
        finally:
            if observer is not None:
                try:
                    observed = observer.finish(
                        returncode=run.returncode if run else None,
                        state="exited" if run else "interrupted-or-failed",
                        stalled=run.stalled if run else False,
                        unsafe_to_replay=run.unsafe_to_replay if run else True)
                    if run is not None and not observed.get("evidence_complete"):
                        run.warning = _append_warning(run.warning, "attempt evidence incomplete")
                        run.unsafe_to_replay = True
                except (OSError, ValueError):
                    if run is not None:
                        run.warning = _append_warning(run.warning, "attempt evidence not finalized")
                        run.unsafe_to_replay = True

    result = await _run_role_invocation(role, attempt, run_codex)
    for run in runs:
        result.warning = _append_warning(result.warning, run.warning)
    if result.retriable and any(run.unsafe_to_replay for run in runs):
        result.retriable = False
        if result.error and result.error.startswith("[retriable:"):
            result.error = result.error.replace("[retriable:", "[replay-blocked:", 1)
        result.error = (result.error or "transient failure") + (
            "; not replayed because tool work may already have happened; "
            "inspect partial effects before a deliberate recovery")
    return result
''')
s.function('_run_role_invocation', transform=lambda text: text.replace(
    '    root = _project_root()\n', '    root = role.workdir or _project_root()\n', 1).replace(
    '    session_id, meta = load_session(role.id)\n',
    '    policy = parse_policy(role.execution) if role.execution is not None else None\n'
    '    session_id, meta = ((None, None) if policy and policy.continuity == "fresh"\n'
    '                        else load_session(role.id))\n'
    '    execution = ({"execution_options": command_options(policy, role.execution_overrides)}\n'
    '                 if policy is not None else {})\n', 1).replace(
    '_resume_cmd(root, session_id, model, effort)',
    '_resume_cmd(root, session_id, model, effort, **execution)').replace(
    '_fresh_cmd(root, model, effort)', '_fresh_cmd(root, model, effort, **execution)'))
s.replace('        # Stale: log, warn, clear, fall through to fresh.', '''        # A stale-looking transport failure after tool work is not evidence that
        # replay is safe. Preserve the saved thread and all partial effects.
        if run.unsafe_to_replay:
            return RoleResult(
                role=role, ok=False,
                error="[replay-blocked:stale] saved thread could not be resumed after "
                      "possible tool work; inspect partial effects before recovery",
                elapsed_seconds=time.monotonic() - started, attempts=attempt,
            )
        # Stale: log, warn, clear, fall through to fresh.''')
s.function('_run_role_attempts', transform=lambda text: text.replace(
    '    attempt = 1\n', '    role_started = time.monotonic()\n    attempt = 1\n', 1).replace(
    '            return result\n',
    '            result.elapsed_seconds = time.monotonic() - role_started\n'
    '            return result\n', 1))
s.replace('    def _reply_suffix(result):\n', '''    def _reply_suffix(result):
        if _WORKFLOW["plan"] is not None:
            _WORKFLOW["settled"][result.role.id] = result
''')
s.replace('    args = parser.parse_args(argv)\n', '''    parser.add_argument("--prepare", metavar="RUNDIR",
                        help="Capture request.json inputs and create independent experiment workspaces.")
    parser.add_argument("--freshness", metavar="RUNDIR",
                        help="Compare captured inputs with current source state (0 unchanged, 1 stale/unknown).")
    args = parser.parse_args(argv)
''')
s.replace('    commands = (\n        ("--discover", args.discover),',
          '    commands = (\n        ("--prepare", args.prepare),\n'
          '        ("--freshness", args.freshness),\n        ("--discover", args.discover),')
s.function('_validate_staging_dir', transform=lambda text: text.replace(
    '    _read_context_file(context_path)\n', '    body = _read_context_file(context_path)\n', 1).replace(
    '    roles, _ = _resolve_run_selections(\n'
    '        roles, path, routing_mode, at_launch=False\n    )',
    '    roles, _, _ = _workflow_selection(\n'
    '        roles, path, routing_mode, at_launch=False, body=body,\n'
    '        probe_policy=prefix != "--start: "\n    )'))
s.function('_check_staging_dir', transform=lambda text: text + '''
    plan = load_plan(path)
    if plan is not None:
        for line in plan_lines(plan, roles, _stall_secs()):
            print(_report_inline(line))
''')
s.function('main', transform=lambda text: text.replace(
    '    args = _parse_args(sys.argv[1:])\n', '''    args = _parse_args(sys.argv[1:])
    # Preserve relative CODEX_HOME semantics when execution moves to a scratch
    # cwd. No config file is edited and credentials are never copied.
    home = os.environ.get("CODEX_HOME")
    if home and not os.path.isabs(home):
        os.environ["CODEX_HOME"] = os.path.abspath(home)
    if args.prepare is not None:
        try:
            directory = _check_private_dir(args.prepare, prefix="--prepare: ")
            _usage_exit_if_launched(directory, "--prepare: ")
            roles = _parse_roles_json(_read_roles_file(os.path.join(directory, "roles.json")))
            body = _read_context_file(os.path.join(directory, "context.md"))
            plan = prepare(directory, roles, body)
        except (OSError, ValueError) as exc:
            _usage_exit("--prepare: " + _report_inline(exc)
                        + "; pause overlapping writers and start over in a new private directory.")
        print(f"[codex-council] captured snapshot={plan['snapshot']['snapshot_id']} "
              f"files={len(plan['snapshot']['files'])} roles={len(roles)} "
              f"omitted={len(plan['snapshot']['omitted'])} version={_plugin_version()}")
        return
    if args.freshness is not None:
        try:
            code = freshness_command(args.freshness)
        except (OSError, ValueError) as exc:
            _usage_exit("--freshness: " + _report_inline(exc))
        sys.exit(code)
''', 1).replace(
    '                _discover_command(args.discover)\n', '''                directory = _check_private_dir(args.discover, prefix="--discover: ")
                _usage_exit_if_launched(directory, "--discover: ")
                if os.path.lexists(os.path.join(directory, "prepare.lock")):
                    try:
                        roles = _parse_roles_json(_read_roles_file(os.path.join(directory, "roles.json")))
                        body = _read_context_file(os.path.join(directory, "context.md"))
                        discover_prepared(directory, roles, body)
                    except (OSError, ValueError) as exc:
                        _usage_exit("--discover: " + _report_inline(exc))
                else:
                    _discover_command(args.discover)
''', 1).replace(
    '            roles, launch = _resolve_run_selections(\n'
    '                roles, run_dir, routing_mode, at_launch=True\n            )',
    '            roles, launch, plan = _workflow_selection(\n'
    '                roles, run_dir, routing_mode, at_launch=True, body=body\n            )\n'
    '            _WORKFLOW.update(plan=plan, run_dir=run_dir, settled={}, result=None)', 1).replace(
    '        _RUN.finish("interrupted", 130)\n',
    '        _finish_evidence(state="interrupted")\n        _RUN.finish("interrupted", 130)\n', 1).replace(
    '        _RUN.finish("aborted", 1)\n',
    '        _finish_evidence(state="aborted")\n        _RUN.finish("aborted", 1)\n', 1).replace(
    '        _RUN.finish("interrupted", 128 + int(signum))\n',
    '        _finish_evidence(state="interrupted")\n        _RUN.finish("interrupted", 128 + int(signum))\n', 1).replace(
    '    elapsed = time.monotonic() - started\n    try:\n',
    '    elapsed = time.monotonic() - started\n'
    '    evidence_result = _finish_evidence(results)\n    try:\n', 1).replace(
    '        sys.stdout.flush()\n',
    '        if plan is not None:\n'
    '            print("\\n## Captured evidence\\n\\nRead `result.json` and per-attempt logs before "\n'
    '                  "reconciliation. Run `--freshness` before applying any candidate change. "\n'
    '                  "Execution success is not a substantive verification verdict.")\n'
    '        sys.stdout.flush()\n', 1).replace(
    '    exit_code = 0 if successes else 1\n',
    '    exit_code = 0 if successes else 1\n'
    '    if plan is not None and (evidence_result is None\n'
    '                             or not evidence_result["required_contributions_complete"]):\n'
    '        exit_code = 1\n', 1))
s.function('main', transform=lambda text: text.replace(
    '    # Authoring defects exit 2 here, before any worker; automatic\n',
    '    try:\n'
    '        preparing = load_plan(run_dir, roles, body)\n'
    '    except (OSError, ValueError) as exc:\n'
    '        _usage_exit("invocation contract: " + _report_inline(exc))\n'
    '    if preparing is not None:\n'
    '        _WORKFLOW.update(plan=preparing, run_dir=run_dir, settled={}, result=None)\n'
    '        _RUN.attach(os.path.join(run_dir, STATUS_FILENAME),\n'
    '                    mode="detached" if _SUPERVISOR["lock_fd"] is not None else None)\n'
    '        _RUN.begin([role.id for role in roles])\n'
    '        _diag(f"[codex-council] preparing {len(roles)} captured contributions; "\n'
    '              "no worker dispatched yet")\n'
    '    # Authoring defects exit 2 here, before any worker; automatic\n', 1).replace(
    '        _diag("\\n[codex-council] interrupted by user")\n        sys.exit(130)\n',
    '        _finish_evidence(state="interrupted")\n'
    '        _RUN.finish("interrupted", 130)\n'
    '        _diag("\\n[codex-council] interrupted by user")\n        sys.exit(130)\n', 1).replace(
    '        _exit_interrupted(stop.signum, "\\n[codex-council] interrupted by")\n',
    '        _finish_evidence(state="interrupted")\n'
    '        _RUN.finish("interrupted", 128 + int(stop.signum))\n'
    '        _exit_interrupted(stop.signum, "\\n[codex-council] interrupted by")\n', 1))
s.function('_become_supervisor', transform=lambda text: text.replace(
    '    try:\n        _atomic_write_private(sup_path, json.dumps(record).encode("utf-8"))',
    '    if not record.get("start_identity"):\n'
    '        _diag("[codex-council] cannot establish supervisor process identity; "\n'
    '              "stopped before any worker; inspect ps support on this host")\n'
    '        sys.exit(2)\n'
    '    try:\n        _atomic_write_private(sup_path, json.dumps(record).encode("utf-8"))', 1))
s.save()

s = Source(SCRIPTS/'council_liveness.py')
s.replace('                FOLLOW_DISPATCH_PREFIX):',
          '                (FOLLOW_DISPATCH_PREFIX, "[codex-council] preparing ")):')
s.save()

s = Source(ROOT/'tests/test_module_layout.py')
s.replace('    "council_selection", "council_failures", "council_liveness",\n)',
          '    "council_selection", "council_failures", "council_liveness",\n'
          '    "council_workspace", "council_policy", "council_evidence", "council_workflow",\n)')
s.replace('    "council_common": set(),', '''    "council_workspace": set(),
    "council_policy": {"council_workspace", "council_discovery"},
    "council_evidence": {"council_workspace"},
    "council_workflow": {"council_workspace", "council_policy", "council_discovery", "council_selection"},
    "council_common": set(),''')
s.replace('        "council_failures", "council_liveness",\n    },',
          '        "council_failures", "council_liveness", "council_workspace",\n'
          '        "council_policy", "council_evidence", "council_workflow",\n    },')
s.save()

p = ROOT/'tests/test_codex_council_cli.py'
t = p.read_text()
t = t.replace('test_the_previous_epoch_3_is_a_stale_pair_on_every_path',
              'test_the_previous_epoch_4_is_a_stale_pair_on_every_path')
t = t.replace('self.assertEqual(council_testlib.EPOCH, "4")',
              'self.assertEqual(council_testlib.EPOCH, "5")')
t = t.replace('"--skill-contract",\n                                                  "3"',
              '"--skill-contract",\n                                                  "4"')
t = t.replace('"--skill-contract 3 does not match this "\n                              "script\'s contract epoch 4:',
              '"--skill-contract 4 does not match this "\n                              "script\'s contract epoch 5:')
p.write_text(t)

# Replace obsolete epoch-4 prose/PDF contracts with executable schema/CLI tests.
# Preserve every other runtime, parser, transport and process test class.
p = ROOT / "tests/test_codex_council.py"
text = p.read_text()
lines = text.splitlines(keepends=True)
obsolete = {"DocsContractTests", "ContextRecipeBehaviorTests"}
removed = []
for node in sorted(ast.parse(text).body, key=lambda n: n.lineno, reverse=True):
    if isinstance(node, ast.ClassDef) and node.name in obsolete:
        removed.append(node.name)
        del lines[node.lineno-1:node.end_lineno]
if "DocsContractTests" not in removed:
    raise AssertionError("documentation contract class moved; review before integrating")
p.write_text("".join(lines))
print("Replaced obsolete prose/PDF contracts:", sorted(removed))

(ROOT / "docs/codex-council.pdf").unlink()
p = ROOT / ".gitignore"
p.write_text(p.read_text().rstrip() + "\n/docs/codex-council.pdf\n")
p = ROOT/'plugins/codex-council/.claude-plugin/plugin.json'
meta = json.loads(p.read_text())
meta['version'] = '1.3.0-rc.1'
meta['description'] = 'Adaptive, on-demand Codex collaboration for Claude Code with captured inputs, disposable experiments, explicit execution permissions, and evidence-based reconciliation.'
p.write_text(json.dumps(meta, indent=2)+'\n')
print('Integrated source: capture, per-role policies, evidence, continuity and replay safety.')
