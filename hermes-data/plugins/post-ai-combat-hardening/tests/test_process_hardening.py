import importlib.util
import os
from pathlib import Path

PLUGIN = Path(
    "/opt/data/plugins/"
    "post-ai-combat-hardening/__init__.py"
)

spec = importlib.util.spec_from_file_location(
    "post_ai_combat_hardening_test",
    PLUGIN,
)

module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def expect_runtime(code, cfg):
    try:
        module._connection_from_config(cfg)
    except RuntimeError as exc:
        text = str(exc)
        assert text.startswith(code), (code, text)
        return
    raise AssertionError(
        f"expected RuntimeError {code}"
    )


expect_runtime(
    "gwrm_control_url_missing",
    {
        "lsp": {
            "servers": {
                "godot-gdscript": {
                    "env": {
                        "GWRM_API_KEY": "x"
                    }
                }
            }
        }
    },
)

expect_runtime(
    "gwrm_api_key_config_missing",
    {
        "lsp": {
            "servers": {
                "godot-gdscript": {
                    "env": {
                        "GWRM_CONTROL_URL": "http://x"
                    }
                }
            }
        }
    },
)

old = os.environ.pop("GWRM_API_KEY", None)

try:
    expect_runtime(
        "gwrm_api_key_env_missing",
        {
            "lsp": {
                "servers": {
                    "godot-gdscript": {
                        "env": {
                            "GWRM_CONTROL_URL": "http://x",
                            "GWRM_API_KEY": "${GWRM_API_KEY}",
                        }
                    }
                }
            }
        },
    )

    os.environ["GWRM_API_KEY"] = "secret"
    url, key = module._connection_from_config(
        {
            "lsp": {
                "servers": {
                    "godot-gdscript": {
                        "env": {
                            "GWRM_CONTROL_URL": "http://x/",
                            "GWRM_API_KEY": "${GWRM_API_KEY}",
                        }
                    }
                }
            }
        }
    )

    assert url == "http://x"
    assert key == "secret"

finally:
    if old is None:
        os.environ.pop("GWRM_API_KEY", None)
    else:
        os.environ["GWRM_API_KEY"] = old


print("[OK] post-ai-combat-hardening diagnostics")
