"""Execution capabilities are separate from a collaborator's task or label.

Policy checks are metadata-only and fail closed. A successful read is evidence
of configuration at that instant, not an attestation of a later Codex process.
The sandbox constrains Codex command tools; it is not a whole-process security
boundary. In particular, read-only does not mean the rest of HOME is unreadable.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from council_workspace import WorkspaceError

POLICY_KEYS = frozenset({"sandbox", "network", "external_tools", "continuity",
                         "required", "authorization"})
DISABLED_FEATURES = ("apps", "plugins", "browser_use", "browser_use_external",
                     "computer_use", "hooks", "multi_agent")


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


def policy_dict(value: Any = None) -> dict[str, Any]:
    return asdict(parse_policy(value))


def config_overrides(policy: ExecutionPolicy, mcp_names: tuple[str, ...] = ()
                     ) -> tuple[str, ...]:
    mode = "danger-full-access" if policy.sandbox == "bypass" else policy.sandbox
    overrides = [f'sandbox_mode="{mode}"', 'approval_policy="never"',
                 "sandbox_workspace_write.writable_roots=[]",
                 "sandbox_workspace_write.exclude_slash_tmp=true",
                 "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                 f"sandbox_workspace_write.network_access={str(policy.network).lower()}"]
    if policy.external_tools == "deny":
        overrides.extend(f"features.{name}=false" for name in DISABLED_FEATURES)
        overrides.append('web_search="disabled"')
        for name in mcp_names:
            # JSON-quoted ASCII names form TOML basic quoted keys, including
            # dots and quotes; never interpolate an unquoted dotted key.
            if not name or not name.isascii() or not name.isprintable():
                raise WorkspaceError("MCP server name cannot be safely represented")
            overrides.append(f"mcp_servers.{json.dumps(name)}.enabled=false")
    return tuple(overrides)


def command_options(policy: ExecutionPolicy, overrides: tuple[str, ...] = ()
                    ) -> list[str]:
    if policy.sandbox == "bypass":
        result = ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        result = ["--sandbox", policy.sandbox]
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


def inspect_policy(root: str, policy: ExecutionPolicy, *,
                   reader: Callable[[str, tuple[str, ...]], dict[str, Any]] | None = None
                   ) -> dict[str, Any]:
    """Read -> enumerate MCP names -> reread with all requested restrictions.

    No credentials, endpoint values, or raw config leave this function. Metadata
    unavailability blocks a restricted execution instead of enabling bypass.
    """
    if reader is None:
        reader = _read_config
    first = reader(root, config_overrides(policy))
    servers = first.get("mcp_servers")
    if policy.external_tools == "deny" and not isinstance(servers, dict):
        raise WorkspaceError("Codex did not expose an MCP inventory; isolation unverified")
    names = tuple(servers) if isinstance(servers, dict) else ()
    if not all(isinstance(name, str) for name in names):
        raise WorkspaceError("invalid MCP inventory")
    names = tuple(sorted(names))
    overrides = config_overrides(policy, names)
    observed = reader(root, overrides)
    validate_observed(observed, policy, names)
    return {"requested": asdict(policy), "overrides": list(overrides),
            "attestation": "configuration-observed-not-exec-attested",
            "external_tools": policy.external_tools,
            "cwd": root, "checked_at": time.time()}


def _read_config(root: str, overrides: tuple[str, ...]) -> dict[str, Any]:
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
            if session.server_requests:
                raise WorkspaceError("unexpected server request during policy inspection")
            config = result.get("config") if isinstance(result, dict) else None
            if not isinstance(config, dict):
                raise WorkspaceError("config/read did not return an object")
            return config
    except WorkspaceError:
        raise
    except Exception as exc:
        # App-server errors may carry credentials. Expose an exception category,
        # never the raw RPC body, stderr, or merged configuration.
        raise WorkspaceError(f"execution policy inspection failed ({type(exc).__name__})") from None
