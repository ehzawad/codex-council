"""Execution capabilities are separate from a collaborator's task or label.

Policy checks are metadata-only and fail closed. A successful read is evidence
of configuration at that instant, not an attestation of a later Codex process.
The sandbox constrains Codex command tools; it is not a whole-process security
boundary. In particular, read-only does not mean the rest of HOME is unreadable.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from council_workspace import WorkspaceError

POLICY_KEYS = frozenset({"sandbox", "network", "external_tools", "continuity",
                         "required", "authorization"})
# The default-on external surfaces examined on codex-cli 0.163.0 that reach
# outside the command sandbox or install things. This is not a promise about
# tool classes a later Codex adds; a new surface needs its own review.
DISABLED_FEATURES = ("apps", "plugins", "browser_use", "browser_use_external",
                     "computer_use", "hooks", "multi_agent", "image_generation",
                     "tool_suggest", "skill_mcp_dependency_install", "remote_plugin",
                     "workspace_dependencies")
# Codex splits a -c key on every "." and has no quoted-key syntax there, so only
# these names can be addressed; any other configured server blocks the run.
MCP_NAME = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class ExecutionPolicy:
    sandbox: str = "read-only"
    network: bool = False
    external_tools: str = "deny"
    continuity: str = "fresh"
    required: bool = True
    authorization: str | None = None


def parse_policy(value: Any = None) -> ExecutionPolicy:
    if value is None:
        return ExecutionPolicy()
    if not isinstance(value, dict) or set(value) - POLICY_KEYS:
        raise WorkspaceError("execution must be an object containing only "
                             + ", ".join(sorted(POLICY_KEYS)))
    policy = ExecutionPolicy(**value)
    if policy.sandbox not in ("read-only", "workspace-write", "bypass"):
        raise WorkspaceError("execution.sandbox must be read-only, workspace-write, or bypass")
    if type(policy.network) is not bool or type(policy.required) is not bool:
        raise WorkspaceError("execution.network and execution.required must be booleans")
    if policy.external_tools not in ("deny", "native"):
        raise WorkspaceError("execution.external_tools must be deny or native")
    if policy.continuity not in ("fresh", "resume"):
        raise WorkspaceError("execution.continuity must be fresh or resume")
    if policy.authorization is not None and (
            not isinstance(policy.authorization, str) or not policy.authorization.strip()):
        raise WorkspaceError("execution.authorization must be a nonempty string")
    if (policy.sandbox == "bypass" or policy.network or policy.external_tools == "native"
            ) and not policy.authorization:
        raise WorkspaceError("bypass, network, and native external tools require an "
                             "execution.authorization explaining the user's permission")
    if policy.sandbox == "bypass" and not policy.network:
        raise WorkspaceError("bypass cannot enforce network=false; explicitly authorize "
                             "unrestricted network access too")
    if policy.sandbox == "read-only" and policy.network:
        raise WorkspaceError("network access requires workspace-write or bypass")
    return policy


def effects_possible(policy: ExecutionPolicy | None) -> bool:
    """Whether a tool call under this policy can leave effects behind.

    codex exec --json does not report every tool call (a sandbox-denied
    code-mode command emits no item), so the event stream cannot prove that
    no tool ran. Only a read-only sandbox with external tools denied (and so
    no network) makes an unseen call harmless to repeat.
    """
    return policy is not None and (policy.sandbox != "read-only"
                                   or policy.external_tools != "deny")


def policy_dict(value: Any = None) -> dict[str, Any]:
    return asdict(parse_policy(value))


def config_overrides(policy: ExecutionPolicy, mcp_names: tuple[str, ...] = ()
                     ) -> tuple[str, ...]:
    mode = "danger-full-access" if policy.sandbox == "bypass" else policy.sandbox
    overrides = [f'sandbox_mode="{mode}"', 'approval_policy="never"',
                 "sandbox_workspace_write.writable_roots=[]",
                 "sandbox_workspace_write.exclude_slash_tmp=true",
                 "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                 f"sandbox_workspace_write.network_access={str(policy.network).lower()}",
                 # Memories carry earlier sessions into a fresh contribution.
                 "features.memories=false"]
    if policy.external_tools == "deny":
        overrides.extend(f"features.{name}=false" for name in DISABLED_FEATURES)
        overrides.append('web_search="disabled"')
        # notify runs a host program after every turn, outside the sandbox.
        overrides.append("notify=[]")
        for name in mcp_names:
            if not MCP_NAME.fullmatch(name):
                raise WorkspaceError(
                    "a configured MCP server name cannot be disabled with a Codex -c "
                    "override (only letters, digits, '_' and '-' are addressable); "
                    "external-tool isolation cannot be established")
            overrides.append(f"mcp_servers.{name}.enabled=false")
    return tuple(overrides)


def command_options(policy: ExecutionPolicy, overrides: tuple[str, ...] = ()
                    ) -> list[str]:
    if policy.sandbox == "bypass":
        result = ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        # A user or project execpolicy rule with decision="allow" runs the
        # matching command outside the sandbox; never load those rules.
        result = ["--sandbox", policy.sandbox, "--ignore-rules"]
    for setting in overrides or config_overrides(policy):
        result.extend(("-c", setting))
    return result


def _at(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def validate_observed(config: Any, policy: ExecutionPolicy,
                      mcp_names: tuple[str, ...]) -> None:
    if not isinstance(config, dict):
        raise WorkspaceError("Codex did not return a usable execution configuration")
    expected: dict[str, Any] = {
        "sandbox_mode": "danger-full-access" if policy.sandbox == "bypass" else policy.sandbox,
        "approval_policy": "never",
        "features.memories": False,
    }
    if policy.sandbox == "workspace-write":
        expected.update({
            "sandbox_workspace_write.writable_roots": [],
            "sandbox_workspace_write.exclude_slash_tmp": True,
            "sandbox_workspace_write.exclude_tmpdir_env_var": True,
            "sandbox_workspace_write.network_access": policy.network,
        })
    if policy.external_tools == "deny":
        expected.update({f"features.{name}": False for name in DISABLED_FEATURES})
        expected["web_search"] = "disabled"
        expected["notify"] = []
    for key, wanted in expected.items():
        actual = _at(config, key)
        if type(actual) is not type(wanted) or actual != wanted:
            raise WorkspaceError(f"Codex cannot establish requested execution policy: {key}")
    if policy.external_tools == "deny":
        servers = config.get("mcp_servers")
        if not isinstance(servers, dict) or set(servers) != set(mcp_names):
            raise WorkspaceError("MCP inventory changed or is unknown; prepare policy again")
        if any(not isinstance(value, dict) or value.get("enabled") is not False
               for value in servers.values()):
            raise WorkspaceError("an MCP server is not explicitly disabled")


def validate_requirements(requirements: Any, policy: ExecutionPolicy) -> None:
    """Refuse when managed requirements forbid what this policy sends."""
    if requirements is None:
        return
    if not isinstance(requirements, dict):
        raise WorkspaceError("Codex returned unusable managed requirements")
    mode = "danger-full-access" if policy.sandbox == "bypass" else policy.sandbox
    wanted = [("allowedSandboxModes", mode), ("allowedApprovalPolicies", "never")]
    if policy.external_tools == "deny":
        wanted.append(("allowedWebSearchModes", "disabled"))
    for key, value in wanted:
        allowed = requirements.get(key)
        if allowed is not None and (not isinstance(allowed, list) or value not in allowed):
            raise WorkspaceError(f"managed requirements forbid the requested policy: {key}")
    features = requirements.get("featureRequirements")
    if features is None:
        return
    if not isinstance(features, dict):
        raise WorkspaceError("managed requirements: unusable featureRequirements")
    disabled = ("memories", *(DISABLED_FEATURES if policy.external_tools == "deny" else ()))
    for name in disabled:
        if features.get(name) is True:
            raise WorkspaceError(f"managed requirements force a disabled feature on: {name}")


def inspect_policy(root: str, policy: ExecutionPolicy, *,
                   reader: Callable[[str, tuple[str, ...]], tuple[dict[str, Any], Any]]
                   | None = None) -> dict[str, Any]:
    """Read -> enumerate MCP names -> reread with all requested restrictions.

    The reader returns (effective config, managed requirements). No credentials,
    endpoint values, or raw config leave this function. Metadata unavailability
    blocks a restricted execution instead of enabling bypass.
    """
    if reader is None:
        reader = _read_config
    first, requirements = reader(root, config_overrides(policy))
    validate_requirements(requirements, policy)
    servers = first.get("mcp_servers")
    if policy.external_tools == "deny" and not isinstance(servers, dict):
        raise WorkspaceError("Codex did not expose an MCP inventory; isolation unverified")
    names = tuple(servers) if isinstance(servers, dict) else ()
    if not all(isinstance(name, str) for name in names):
        raise WorkspaceError("invalid MCP inventory")
    names = tuple(sorted(names))
    overrides = config_overrides(policy, names)
    observed, requirements = reader(root, overrides)
    validate_requirements(requirements, policy)
    validate_observed(observed, policy, names)
    return {"requested": asdict(policy), "overrides": list(overrides),
            "attestation": "configuration-observed-not-exec-attested",
            "external_tools": policy.external_tools,
            "cwd": root, "checked_at": time.time()}


def _read_config(root: str, overrides: tuple[str, ...]) -> tuple[dict[str, Any], Any]:
    # Imported lazily so pure policy/workspace tests need no runner installation.
    from council_discovery import _app_server_session

    executable = shutil.which("codex")
    if executable is None:
        raise WorkspaceError("Codex CLI is not installed")
    try:
        with _app_server_session(executable, time.monotonic() + 20,
                                 cwd=root, config_overrides=overrides) as session:
            session.request("initialize", {
                "clientInfo": {"name": "codex-council-policy", "version": "1"},
                "capabilities": {"experimentalApi": False},
            })
            session.notify("initialized")
            result = session.request("config/read", {"cwd": root, "includeLayers": False})
            managed = session.request("configRequirements/read", None)
            if session.server_requests:
                raise WorkspaceError("unexpected server request during policy inspection")
            config = result.get("config") if isinstance(result, dict) else None
            if not isinstance(config, dict):
                raise WorkspaceError("config/read did not return an object")
            if not isinstance(managed, dict) or "requirements" not in managed:
                raise WorkspaceError("configRequirements/read did not return an object")
            return config, managed["requirements"]
    except WorkspaceError:
        raise
    except Exception as exc:
        # App-server errors may carry credentials. Expose only discovery's
        # machine-safe problem/stderr categories, never raw bodies or stderr.
        problem = getattr(exc, "problem", None)
        detail = getattr(exc, "detail", None)
        category = ":".join(str(part) for part in (problem, detail) if part) or type(exc).__name__
        raise WorkspaceError(f"execution policy inspection failed ({category})") from None
