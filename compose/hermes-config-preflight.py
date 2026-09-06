import os
import sys
import time
from pathlib import Path

import yaml

CONFIG_PATH = Path(
    os.environ.get("HERMES_CONFIG_PATH", "/opt/data/config.yaml")
)

ORIGINAL_ENTRYPOINT = Path(
    "/opt/hermes/docker/entrypoint-dispatch.sh"
)

CHECK_INTERVAL_SECONDS = 30
ONCE = os.environ.get(
    "HERMES_CONFIG_PREFLIGHT_ONCE", ""
).strip() == "1"


def validate_config() -> None:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(
            f"config file not found: {CONFIG_PATH}"
        )

    raw = CONFIG_PATH.read_text(encoding="utf-8-sig")
    data = yaml.safe_load(raw)

    if not isinstance(data, dict):
        raise RuntimeError(
            "config root must be a YAML mapping"
        )


while True:
    try:
        validate_config()
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
    f"[HERMES_CONFIG_PREFLIGHT] OK: {CONFIG_PATH}",
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
