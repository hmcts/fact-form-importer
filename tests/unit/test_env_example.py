from pathlib import Path


ALLOWED_TEMPLATE_VALUES = {"", "changeme", "placeholder", "example", "false"}


def test_env_example_does_not_contain_real_values():
    env_example = Path(".env.example")

    for line_number, line in enumerate(env_example.read_text().splitlines(), start=1):
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        assert "=" in stripped, f".env.example:{line_number} should use KEY=VALUE format"

        key, value = stripped.split("=", maxsplit=1)
        value = value.strip().strip("\"'")

        assert key, f".env.example:{line_number} should include an environment variable name"
        assert value.lower() in ALLOWED_TEMPLATE_VALUES, (
            f".env.example:{line_number} should not contain a real value for {key}"
        )
