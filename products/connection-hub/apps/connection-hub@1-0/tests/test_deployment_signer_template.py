from pathlib import Path

import yaml


def test_deployment_template_declares_matching_signer_slots():
    config_dir = Path(__file__).resolve().parents[1] / "config"
    bundles = yaml.safe_load((config_dir / "bundles.template.yaml").read_text())
    secrets = yaml.safe_load((config_dir / "bundles.secrets.template.yaml").read_text())
    config = next(item["config"] for item in bundles["bundles"]["items"] if item["id"] == "connection-hub@1-0")
    slots = next(item["secrets"] for item in secrets["bundles"]["items"] if item["id"] == "connection-hub@1-0")
    delegated = config["connections"]["delegated_credentials"]
    for path in (
        delegated["oauth"]["original_exchange"]["refresh_signing_secret_ref"],
        delegated["admission"]["services"]["kdcube-management"]["secret_ref"],
        "connections.delegated_credentials.grantor_authority_secret",
    ):
        value = slots
        for part in path.split("."):
            value = value[part]
        assert isinstance(value, str) and value.startswith("<") and value.endswith(">")
