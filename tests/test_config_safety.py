import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _example_environment() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name] = value
    values.update(
        {
            "TELEGRAM_TOKEN": "test-token",
            "ADMIN_TELEGRAM_IDS": "123456789",
            "TELEGRAM_CHAT_ID": "",
            "BYBIT_API_KEY": "test-key",
            "BYBIT_API_SECRET": "test-secret",
            "BYBIT_TESTNET": "false",
            "DEEPSEEK_API_KEY": "test-deepseek-key",
            "DRY_RUN": "true",
        }
    )
    return values


def _run_config(**overrides: str) -> dict:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "COMSPEC",
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "WINDIR",
        }
    }
    environment.update(_example_environment())
    environment.update(overrides)
    script = f"""
import json
import sys
import types

sys.path.insert(0, {str(ROOT)!r})
dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda *args, **kwargs: False
sys.modules["dotenv"] = dotenv

import config

print(json.dumps({{
    "mode": config.TRADING_MODE,
    "dry_run": config.DRY_RUN,
    "bybit_env": config.BYBIT_ENV,
    "bybit_base_url": config.BYBIT_BASE_URL,
    "errors": config.validate_config("telegram"),
}}))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


class ConfigSafetyTests(unittest.TestCase):
    def test_trading_mode_typo_fails_dry_and_reports_error(self):
        result = _run_config(TRADING_MODE="lvie")

        self.assertEqual(result["mode"], "dry")
        self.assertTrue(result["dry_run"])
        self.assertTrue(
            any("TRADING_MODE" in error for error in result["errors"]),
            result["errors"],
        )

    def test_live_requires_credentials_confirmation_and_admin(self):
        valid_live = {
            "TRADING_MODE": "live",
            "BYBIT_API_KEY": "test-key",
            "BYBIT_API_SECRET": "test-secret",
            "LIVE_TRADING_CONFIRMATION": "I_ACCEPT_LIVE_TRADING_RISK",
            "ADMIN_TELEGRAM_IDS": "123456789",
        }
        self.assertEqual(_run_config(**valid_live)["errors"], [])

        cases = (
            (
                {"BYBIT_API_KEY": "", "BYBIT_API_SECRET": ""},
                "BYBIT_API_KEY",
            ),
            (
                {"LIVE_TRADING_CONFIRMATION": ""},
                "LIVE_TRADING_CONFIRMATION",
            ),
            (
                {"ADMIN_TELEGRAM_IDS": ""},
                "ADMIN_TELEGRAM_IDS",
            ),
        )
        for missing, marker in cases:
            with self.subTest(marker=marker):
                result = _run_config(**(valid_live | missing))
                self.assertFalse(result["dry_run"])
                self.assertTrue(
                    any(marker in error for error in result["errors"]),
                    result["errors"],
                )

    def test_bybit_environment_maps_only_to_official_hosts(self):
        official_hosts = {
            "mainnet": "https://api.bybit.com",
            "testnet": "https://api-testnet.bybit.com",
            "demo": "https://api-demo.bybit.com",
        }
        for environment, expected_url in official_hosts.items():
            with self.subTest(environment=environment):
                result = _run_config(BYBIT_ENV=environment)
                self.assertEqual(result["bybit_base_url"], expected_url)
                self.assertFalse(
                    any("BYBIT_ENV" in error for error in result["errors"]),
                    result["errors"],
                )

        rejected = _run_config(BYBIT_ENV="https://attacker.invalid")
        self.assertEqual(rejected["bybit_base_url"], official_hosts["mainnet"])
        self.assertTrue(
            any("BYBIT_ENV" in error for error in rejected["errors"]),
            rejected["errors"],
        )


if __name__ == "__main__":
    unittest.main()
