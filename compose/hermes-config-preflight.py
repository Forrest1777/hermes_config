import os
import re
import sys
import time
from pathlib import Path

import yaml

HARDENING_MARKER = "HERMES_CONFIG_INTEGRITY_HARDENING_2026_09_14"

CONFIG_PATH = Path(
    os.environ.get("HERMES_CONFIG_PATH", "/opt/data/config.yaml")
)

PROFILES_ROOT = Path(
    os.environ.get(
        "HERMES_CONFIG_PROFILES_ROOT",
        "/opt/data/profiles",
    )
)

ORIGINAL_ENTRYPOINT = Path(
    "/opt/hermes/docker/entrypoint-dispatch.sh"
)

CHECK_INTERVAL_SECONDS = int(
    os.environ.get(
        "HERMES_CONFIG_PREFLIGHT_RETRY_SECONDS",
        "30",
    )
)

ONCE = (
    os.environ.get("HERMES_CONFIG_PREFLIGHT_ONCE", "")
    .strip()
    == "1"
)

ENV_REF_RE = re.compile(
    r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$"
)


def _read_utf8_strict(path: Path) -> str:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="strict")

    for index, char in enumerate(text):
        code = ord(char)

        if code < 0x20 and char not in ("\t", "\n", "\r"):
            raise RuntimeError(
                f"forbidden C0 control U+{code:04X} "
                f"in {path} at index {index}"
            )

        if 0x7F <= code <= 0x9F:
            raise RuntimeError(
                f"forbidden C1 control U+{code:04X} "
                f"in {path} at index {index}"
            )

    return text


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError(
            f"config file not found: {path}"
        )

    raw = _read_utf8_strict(path)
    data = yaml.safe_load(raw)

    if not isinstance(data, dict):
        raise RuntimeError(
            f"config root must be a YAML mapping: {path}"
        )

    return data


def _resolve_env_ref(value: str) -> tuple[str, str | None]:
    value = str(value or "").strip()
    match = ENV_REF_RE.fullmatch(value)

    if not match:
        return value, None

    env_name = match.group(1)
    return str(os.environ.get(env_name) or "").strip(), env_name


def _validate_godot_gwrm_scope(
    path: Path,
    data: dict,
) -> bool:
    lsp = data.get("lsp")

    if lsp is None:
        return False

    if not isinstance(lsp, dict):
        raise RuntimeError(
            f"lsp must be a mapping: {path}"
        )

    servers = lsp.get("servers") or {}

    if not isinstance(servers, dict):
        raise RuntimeError(
            f"lsp.servers must be a mapping: {path}"
        )

    godot = servers.get("godot-gdscript")

    if godot is None:
        return False

    if not isinstance(godot, dict):
        raise RuntimeError(
            f"godot-gdscript must be a mapping: {path}"
        )

    env = godot.get("env")

    if not isinstance(env, dict):
        raise RuntimeError(
            "godot-gdscript.env missing or invalid: "
            f"{path}"
        )

    raw_url = str(
        env.get("GWRM_CONTROL_URL") or ""
    ).strip()

    raw_key = str(
        env.get("GWRM_API_KEY") or ""
    ).strip()

    if not raw_url:
        raise RuntimeError(
            "GWRM_CONTROL_URL missing from "
            f"godot-gdscript.env: {path}"
        )

    if not raw_key:
        raise RuntimeError(
            "GWRM_API_KEY missing from "
            f"godot-gdscript.env: {path}"
        )

    resolved_url, url_env = _resolve_env_ref(raw_url)
    resolved_key, key_env = _resolve_env_ref(raw_key)

    if url_env and not resolved_url:
        raise RuntimeError(
            f"environment variable {url_env} is empty "
            f"for GWRM_CONTROL_URL: {path}"
        )

    if raw_key != "${GWRM_API_KEY}":
        raise RuntimeError(
            "GWRM_API_KEY must use the canonical "
            "${GWRM_API_KEY} placeholder; literal secrets "
            f"are forbidden: {path}"
        )

    if key_env != "GWRM_API_KEY":
        raise RuntimeError(
            "unexpected GWRM_API_KEY env reference: "
            f"{path}"
        )

    if not resolved_key:
        raise RuntimeError(
            "environment variable GWRM_API_KEY is empty "
            f"for godot-gdscript.env: {path}"
        )

    return True


def validate_config() -> dict:
    global_data = _load_yaml(CONFIG_PATH)

    summary = {
        "global": str(CONFIG_PATH),
        "profiles": 0,
        "gwrm_lsp_scopes": 0,
        "non_lsp_profiles": 0,
    }

    if _validate_godot_gwrm_scope(
        CONFIG_PATH,
        global_data,
    ):
        summary["gwrm_lsp_scopes"] += 1

    if PROFILES_ROOT.is_dir():
        for profile_config in sorted(
            PROFILES_ROOT.glob("*/config.yaml")
        ):
            profile_data = _load_yaml(profile_config)
            summary["profiles"] += 1

            if _validate_godot_gwrm_scope(
                profile_config,
                profile_data,
            ):
                summary["gwrm_lsp_scopes"] += 1
            else:
                summary["non_lsp_profiles"] += 1

    return summary


while True:
    try:
        summary = validate_config()
        break
    except Exception as exc:
        print(
            "[HERMES_CONFIG_PREFLIGHT] BLOCKED: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )

        if ONCE:
            sys.exit(78)

        print(
            "[HERMES_CONFIG_PREFLIGHT] Hermes gateway "
            "WILL NOT START while config is invalid. "
            f"Retrying in {CHECK_INTERVAL_SECONDS}s.",
            file=sys.stderr,
            flush=True,
        )

        time.sleep(CHECK_INTERVAL_SECONDS)


print(
    "[HERMES_CONFIG_PREFLIGHT] OK: "
    f"global={summary['global']} "
    f"profiles={summary['profiles']} "
    f"gwrm_lsp_scopes={summary['gwrm_lsp_scopes']} "
    f"non_lsp_profiles={summary['non_lsp_profiles']}",
    flush=True,
)

if ONCE:
    sys.exit(0)

if not ORIGINAL_ENTRYPOINT.is_file():
    print(
        "[HERMES_CONFIG_PREFLIGHT] BLOCKED: "
        f"original entrypoint missing: {ORIGINAL_ENTRYPOINT}",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(78)

os.execv(
    str(ORIGINAL_ENTRYPOINT),
    [str(ORIGINAL_ENTRYPOINT), *sys.argv[1:]],
)