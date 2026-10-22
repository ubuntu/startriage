import tomllib

from startriage.config import load_config
from startriage.init import build_init_config, write_init_config


def _answers(*answers: str):
    it = iter(answers)
    return lambda _prompt: next(it)


def test_init_known_team_defaults(tmp_path):
    defaults = load_config(None, defaults_only=True)
    data = build_init_config(defaults, _answers("ubuntu-server", "", "", ""))
    assert data == {"general": {"default_team": "ubuntu-server"}}

    path = tmp_path / "startriage.toml"
    write_init_config(path, data)
    assert load_config(path).general.default_team == "ubuntu-server"


def test_init_new_team_with_ai(tmp_path):
    defaults = load_config(None, defaults_only=True)
    savebugs = tmp_path / "saved"
    data = build_init_config(
        defaults,
        _answers(
            "myteam",  # team name
            "my-lp-team",  # lp_team
            "",  # todo tag -> myteam-todo
            "",  # freezer tag
            "org/a, org/b",  # github repos
            "todo",  # github labels
            "",  # github team
            "project/foo",  # discourse
            "",  # proposed migration
            str(savebugs),  # savebugs dir
            "y",  # create dir
            "y",  # ai
            "openrouter",
            "some/model",
        ),
    )
    path = tmp_path / "startriage.toml"
    write_init_config(path, data)

    written = tomllib.loads(path.read_text())
    assert written["team"]["myteam"] == {
        "lp_team": "my-lp-team",
        "lp_todo_tag": "myteam-todo",
        "github_repos": ["org/a", "org/b"],
        "github_todo_labels": ["todo"],
        "discourse_categories": ["project/foo"],
    }
    assert written["ai"] == {"provider": "openrouter", "model": "some/model"}
    assert savebugs.is_dir()
    config = load_config(path)
    assert config.team["myteam"].lp_team == "my-lp-team"


def test_find_user_config_order(tmp_path, monkeypatch):
    from startriage import config

    snap, home = tmp_path / "snap.toml", tmp_path / "home.toml"
    monkeypatch.setattr(config, "DEFAULT_USER_CONFIG", snap)
    monkeypatch.setattr(config, "USER_CONFIG_PATHS", (snap, home))
    assert config.find_user_config() == snap
    home.touch()
    assert config.find_user_config() == home
    snap.touch()
    assert config.find_user_config() == snap
