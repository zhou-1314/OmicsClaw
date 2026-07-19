from omicsclaw.skill.execution.environment import scrub_internal_control_credentials


def test_control_credentials_are_scrubbed_case_insensitively() -> None:
    source = {
        "PATH": "/usr/bin",
        "omicsclaw_remote_auth_token": "remote-secret",
        "OmicsClaw_Skill_Evolution_Token": "launch-secret",
        "omicsclaw_skill_evolution_token_fd": "3",
    }

    scrubbed = scrub_internal_control_credentials(source)

    assert scrubbed == {"PATH": "/usr/bin"}
    assert source["omicsclaw_remote_auth_token"] == "remote-secret"
