"""Config: what loads, what is refused, and what each refusal names."""

from __future__ import annotations

import pwd
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from orion_browser import config
from orion_browser.codes import BrowserError, Code

BASE = {
    "paths": {"state": "/srv/obtest/state", "profiles": "/srv/obtest/profiles"},
    "browser": [{"name": "work", "screen": 21}],
}
# A root that does not exist, so realpath() returns paths under it unchanged on any machine. Golden tags are pinned on
# it: the tag of /srv/obtest/state would change wherever /srv is a link.
GOLDEN_ROOT = "/nonexistent-orion-browser-golden"


def parse(**over):
    return config.parse({**BASE, **over}, Path("/srv/obtest/config.toml"))


def refused(**over) -> str:
    with pytest.raises(BrowserError) as info:
        parse(**over)
    assert info.value.code is Code.USAGE
    return info.value.message


def test_defaults_and_derived_names():
    cfg = parse()
    assert cfg.chrome == Path("/usr/bin/google-chrome")
    assert (cfg.width, cfg.height, cfg.ttl_minutes, cfg.max_minutes) == (1920, 1080, 30, 240)
    assert (cfg.stop_grace, cfg.stop_timeout, cfg.start_limit) == (15.0, 30.0, 30.0)
    assert cfg.novnc_dir == Path("/usr/share/novnc")
    work = cfg.browser("work")
    # 32408f05 is the first 8 hex digits of sha256(b"/nonexistent-orion-browser-golden/state")
    golden = parse(paths={"state": f"{GOLDEN_ROOT}/state", "profiles": f"{GOLDEN_ROOT}/profiles"})
    assert golden.browser("work").unit == "orion-browser-work-32408f05.service"
    assert work.profile == Path("/srv/obtest/profiles/work")
    assert work.downloads == Path("/srv/obtest/state/downloads/work")
    assert work.port_file == Path("/srv/obtest/profiles/work/DevToolsActivePort")
    assert work.closing_marker == Path("/srv/obtest/state/sessions/work.closing")
    assert work.stop_marker == Path("/srv/obtest/state/sessions/work.stop")
    assert (work.display, work.vnc_port, work.view_port, work.restore_session) == (":21", 5921, 6101, False)
    assert work.view_link(drive=False).endswith(":6101/vnc.html?autoconnect=1&resize=scale&view_only=1")
    assert work.view_link(drive=True).endswith(":6101/vnc.html?autoconnect=1&resize=scale&view_only=0")


@pytest.mark.parametrize("name", ["Dan", "", "a" * 17, "d-n", "../x", 7])
def test_bad_browser_names(name):
    assert "lowercase letters or digits" in refused(browser=[{"name": name, "screen": 21}])


def test_duplicate_name_and_screen():
    assert "appears twice" in refused(browser=[{"name": "work", "screen": 21}, {"name": "work", "screen": 22}])
    assert "belongs to another browser" in refused(
        browser=[{"name": "work", "screen": 21}, {"name": "yin", "screen": 21}]
    )


@pytest.mark.parametrize("screen", [9, 100, True, "21"])
def test_screen_range(screen):
    assert "screen must be a whole number from 10 to 99" in refused(browser=[{"name": "work", "screen": screen}])


def test_screen_is_required():
    assert "screen is required" in refused(browser=[{"name": "work"}])


def test_restore_session_must_be_a_boolean():
    assert "restore_session must be true or false" in refused(
        browser=[{"name": "work", "screen": 21, "restore_session": "yes"}]
    )
    assert parse(browser=[{"name": "work", "screen": 21, "restore_session": True}]).browser("work").restore_session


def test_at_least_one_browser():
    assert "at least one [[browser]]" in refused(browser=[])


def test_lease_limits():
    assert "must not be longer than lease.max_minutes" in refused(lease={"ttl_minutes": 60, "max_minutes": 30})
    assert "lease.ttl_minutes must be a whole number from 1 to 10080" in refused(lease={"ttl_minutes": 0})


def test_stop_timeout_leaves_room_after_the_grace():
    assert "at least 10 seconds longer than timing.stop_grace_seconds" in refused(
        timing={"stop_grace_seconds": 15, "service_stop_timeout_seconds": 24}
    )
    cfg = parse(timing={"stop_grace_seconds": 1, "service_stop_timeout_seconds": 11, "start_limit_seconds": 5})
    assert (cfg.stop_grace, cfg.stop_timeout, cfg.start_limit) == (1.0, 11.0, 5.0)


def test_unknown_keys_are_named():
    assert "unknown keys: colour" in refused(screen={"colour": "blue"})
    assert "unknown keys: extra" in refused(extra=1)


def test_relative_paths_are_refused():
    assert "must be an absolute path" in refused(paths={"state": "state", "profiles": "/srv/p"})


def test_unknown_browser_names_the_known_ones():
    with pytest.raises(BrowserError) as info:
        parse().browser("yin")
    assert info.value.code is Code.USAGE and info.value.data == {"browsers": ["work"]}


def test_load_refuses_state_inside_a_git_tree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'[paths]\nstate = "{repo}/state"\nprofiles = "{tmp_path}/profiles"\n\n[[browser]]\nname = "work"\nscreen = 21\n'
    )
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(cfg_file))
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE and "inside a git working tree" in info.value.message


def test_load_names_a_missing_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(tmp_path / "none.toml"))
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE and "no config at" in info.value.message


def test_loopback_override(monkeypatch):
    monkeypatch.setenv("ORION_BROWSER_LOOPBACK", "127.123.4.5")
    assert parse().browser("work").view_link(drive=False).startswith("http://127.123.4.5:6101/")
    monkeypatch.setenv("ORION_BROWSER_LOOPBACK", "10.0.0.1")
    with pytest.raises(BrowserError) as info:
        config.loopback()
    assert info.value.code is Code.USAGE


def test_names_with_digits_and_sixteen_characters_are_accepted():
    cfg = parse(browser=[{"name": "a" * 16, "screen": 21}, {"name": "dan2", "screen": 22}])
    assert sorted(cfg.browsers) == ["a" * 16, "dan2"]


def test_the_lease_default_may_equal_the_maximum():
    cfg = parse(lease={"ttl_minutes": 240, "max_minutes": 240})
    assert (cfg.ttl_minutes, cfg.max_minutes) == (240, 240)


def test_load_refuses_profiles_inside_a_git_tree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'[paths]\nstate = "{tmp_path}/state"\nprofiles = "{repo}/profiles"\n\n[[browser]]\nname = "work"\nscreen = 21\n'
    )
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(cfg_file))
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE and "paths.profiles" in info.value.message


def test_default_paths_are_under_home_whatever_xdg_says(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for variable in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(variable, str(tmp_path / "elsewhere"))
    monkeypatch.delenv("ORION_BROWSER_CONFIG", raising=False)
    cfg = config.parse({"browser": [{"name": "work", "screen": 21}]}, Path("/srv/obtest/config.toml"))
    assert cfg.state_dir == tmp_path / "home" / ".local" / "state" / "orion-browser"
    assert cfg.profiles_dir == tmp_path / "home" / ".local" / "share" / "orion-browser" / "profiles"
    assert config.config_path() == tmp_path / "home" / ".config" / "orion-browser" / "config.toml"


def test_the_config_path_follows_its_variable(monkeypatch):
    monkeypatch.setenv("ORION_BROWSER_CONFIG", "/srv/obtest/custom.toml")
    assert config.config_path() == Path("/srv/obtest/custom.toml")


@pytest.mark.parametrize("key", ["state", "profiles"])
def test_a_state_or_profiles_path_holding_a_space_and_two_hyphens_is_usage(key):
    """Chrome rewrites its process title into one string, where orion-browser tells its profile flag's value from the
    next flag by " --". A profile path holding that could not be read back there, so a Chrome on it would go unseen;
    the state folder holds each browser's download folder, so it is refused the same way. One hyphen, or two with no
    space before them, is fine."""
    paths = {"state": "/srv/obtest/state", "profiles": "/srv/obtest/profiles"}
    message = refused(paths={**paths, key: f"/srv/Work -- Backup/{key}"})
    assert f"paths.{key}" in message and "/srv/Work -- Backup" in message and "' --'" in message, message
    assert "' --'" in refused(paths={**paths, key: f"/srv/Work --Backup/{key}"})
    for fine in (f"/srv/Work - Backup/{key}", f"/srv/Work--Backup/{key}"):
        assert parse(paths={**paths, key: fine})


def test_load_refuses_a_browser_profile_linked_to_a_path_holding_a_space_and_two_hyphens(tmp_path, monkeypatch):
    """A browser's profile can be a link: what it leads to is what a Chrome on that profile is started with, and it
    must not hold " --" either."""
    target = tmp_path / "Work -- Backup" / "work"
    target.mkdir(parents=True)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "work").symlink_to(target)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'[paths]\nstate = "{tmp_path}/state"\nprofiles = "{tmp_path}/profiles"\n\n[[browser]]\nname = "work"\nscreen = 21\n'
    )
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(cfg_file))
    with pytest.raises(BrowserError) as info:
        config.load()
    message = info.value.message
    assert info.value.code is Code.USAGE and "browser work's profile" in message and str(target) in message, message


def test_a_nul_character_in_a_path_is_usage():
    assert "must not contain a NUL character" in refused(paths={"state": "/srv/a\x00b", "profiles": "/srv/p"})
    assert "must not contain a NUL character" in refused(chrome={"path": "/usr/bin/goo\x00gle"})


@pytest.mark.parametrize("host", ["127.0.0.\u00b2", "127.0.0.\u0663"])
def test_loopback_takes_ascii_digits_only(monkeypatch, host):
    monkeypatch.setenv("ORION_BROWSER_LOOPBACK", host)
    with pytest.raises(BrowserError) as info:
        config.loopback()
    assert info.value.code is Code.USAGE


def test_an_uninspectable_dot_git_refuses_the_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").symlink_to(repo / ".git")  # a symlink loop: stat raises ELOOP
    assert "could not be checked for a git working tree" in config._git_tree_problem(repo / "state")


def test_a_path_that_cannot_be_resolved_is_usage(tmp_path, monkeypatch):
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'[paths]\nstate = "{tmp_path}/loop/state"\nprofiles = "{tmp_path}/profiles"\n\n[[browser]]\nname = "work"\nscreen = 21\n'
    )
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(cfg_file))
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE and "paths.state" in info.value.message


def test_a_gitdir_file_marks_a_linked_worktree(tmp_path):
    tree = tmp_path / "linked"
    tree.mkdir()
    (tree / ".git").write_text("gitdir: /srv/obtest/repo/.git/worktrees/linked\n")
    assert config._git_tree_problem(tree / "state") == config.INSIDE_GIT_TREE
    (tree / ".git").write_text("not a gitdir line\n")
    assert config._git_tree_problem(tree / "state") is None


def test_refuse_inside_git_tree_names_the_label(tmp_path):
    tree = tmp_path / "linked"
    tree.mkdir()
    (tree / ".git").write_text("gitdir: /srv/obtest/repo/.git/worktrees/linked\n")
    with pytest.raises(BrowserError) as info:
        config.refuse_inside_git_tree(tree / "downloads", "directory")
    assert info.value.code is Code.USAGE and info.value.message.startswith("directory ")
    config.refuse_inside_git_tree(tmp_path / "elsewhere", "directory")  # outside any tree: no error


def test_no_home_directory_is_usage_unless_paths_are_set(monkeypatch):
    def no_entry(uid):
        raise KeyError(f"getpwuid(): uid not found: {uid}")

    monkeypatch.delenv("HOME", raising=False)  # a set HOME is used as it is; the user database is asked only without it
    monkeypatch.setattr(pwd, "getpwuid", no_entry)  # and it has no entry for this account
    assert "no home directory" in refused(paths={})
    assert parse().state_dir == Path("/srv/obtest/state")  # both paths set: no home needed


@pytest.mark.parametrize("state, profiles", [("/srv/x", "/srv/x"), ("/srv/x", "/srv/x/profiles"), ("/srv/x/state", "/srv/x")])
def test_state_and_profiles_must_not_overlap(state, profiles):
    assert "must be separate folders" in refused(paths={"state": state, "profiles": profiles})


def _repo(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return root


def test_a_folder_reached_through_a_symlink_into_a_repository_is_refused(tmp_path):
    repo = _repo(tmp_path / "dotfiles")
    (repo / "browser").mkdir()
    (tmp_path / "link").symlink_to(repo / "browser")
    with pytest.raises(BrowserError) as info:
        config.refuse_inside_git_tree(tmp_path / "link" / "state", "paths.state")
    assert "inside a git working tree" in info.value.message


def test_a_folder_that_is_itself_a_repository_root_is_refused(tmp_path):
    repo = _repo(tmp_path / "repo")
    with pytest.raises(BrowserError) as info:
        config.refuse_inside_git_tree(repo, "paths.profiles")
    assert "inside a git working tree" in info.value.message


def test_the_raw_check_walks_a_relative_path_from_the_working_directory(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")
    assert config._git_tree_problem(Path("state")) == config.INSIDE_GIT_TREE


@pytest.mark.parametrize("content", [b"[screen\nwidth = 1", b"\xff\xfe"])
def test_an_unreadable_config_is_usage(tmp_path, monkeypatch, content):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_bytes(content)
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(cfg_file))
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE and "cannot read config" in info.value.message


def test_a_directory_at_the_config_path_is_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(tmp_path))
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE and "cannot read config" in info.value.message


@pytest.mark.parametrize("table, where", [
    ("chrome", "[chrome]"), ("screen", "[screen]"), ("lease", "[lease]"), ("timing", "[timing]"),
    ("paths", "[paths]"), ("view", "[view]"),
])
def test_an_unknown_key_in_any_table_is_named(table, where):
    assert f"{where} has unknown keys: tpyo" in refused(**{table: {"tpyo": 1}})


def test_an_unknown_key_in_a_browser_is_named():
    message = refused(browser=[{"name": "work", "screen": 21, "restore_sesion": True}])
    assert "[[browser]] #1 has unknown keys: restore_sesion" in message


@pytest.mark.parametrize("over, expected", [
    ({"lease": {"ttl_minutes": True}}, "lease.ttl_minutes must be a whole number"),
    ({"lease": {"ttl_minutes": 1.5}}, "lease.ttl_minutes must be a whole number"),
    ({"chrome": {"path": 7}}, "chrome.path must be a non-empty string"),
    ({"chrome": {"path": ""}}, "chrome.path must be a non-empty string"),
    ({"screen": "wide"}, "[screen] must be a table"),
    ({"browser": "work"}, "at least one [[browser]]"),
    ({"browser": ["work"]}, "[[browser]] #1 must be a table"),
])
def test_type_mismatches_are_usage_naming_the_key(over, expected):
    assert expected in refused(**over)


def test_a_tilde_user_that_names_nobody_is_usage():
    assert "cannot expand ~" in refused(paths={"state": "~nosuchuser_obtest/state", "profiles": "/srv/p"})


@pytest.mark.parametrize("over, check", [
    ({"lease": {"ttl_minutes": 10080, "max_minutes": 10080}}, lambda c: c.ttl_minutes == 10080),
    ({"screen": {"width": 640, "height": 480}}, lambda c: (c.width, c.height) == (640, 480)),
    ({"screen": {"width": 7680, "height": 4320}}, lambda c: (c.width, c.height) == (7680, 4320)),
    ({"timing": {"stop_grace_seconds": 300, "service_stop_timeout_seconds": 600, "start_limit_seconds": 600}},
     lambda c: (c.stop_grace, c.stop_timeout, c.start_limit) == (300.0, 600.0, 600.0)),
    ({"browser": [{"name": "work", "screen": 99}]}, lambda c: c.browser("work").screen == 99),
])
def test_the_ends_of_each_range_are_accepted(over, check):
    assert check(parse(**over))


@pytest.mark.parametrize("over, label", [
    ({"lease": {"ttl_minutes": 10081}}, "lease.ttl_minutes"),
    ({"screen": {"width": 639}}, "screen.width"),
    ({"screen": {"height": 4321}}, "screen.height"),
    ({"timing": {"stop_grace_seconds": 301, "service_stop_timeout_seconds": 600}}, "timing.stop_grace_seconds"),
    ({"timing": {"start_limit_seconds": 601}}, "timing.start_limit_seconds"),
])
def test_one_past_each_range_is_refused(over, label):
    assert f"{label} must be a whole number" in refused(**over)


def test_loopback_refuses_octal_looking_addresses_and_defaults_when_empty(monkeypatch):
    monkeypatch.setenv("ORION_BROWSER_LOOPBACK", "127.0.0.010")
    with pytest.raises(BrowserError):
        config.loopback()
    monkeypatch.setenv("ORION_BROWSER_LOOPBACK", "")
    assert config.loopback() == "127.0.0.1"


def test_load_checks_the_loopback_override(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'[paths]\nstate = "{tmp_path}/state"\nprofiles = "{tmp_path}/profiles"\n\n[[browser]]\nname = "work"\nscreen = 21\n'
    )
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(cfg_file))
    monkeypatch.setenv("ORION_BROWSER_LOOPBACK", "10.0.0.1")
    with pytest.raises(BrowserError) as info:
        config.load()
    assert "ORION_BROWSER_LOOPBACK" in info.value.message


def _write_config(folder: Path, paths: str = "") -> Path:
    """A config file with one browser and, when given, a [paths] table, for load() to be pointed at."""
    cfg_file = folder / "config.toml"
    cfg_file.write_text(paths + '[[browser]]\nname = "work"\nscreen = 21\n')
    return cfg_file


def _paths_in(folder: Path) -> str:
    return f'[paths]\nstate = "{folder}/state"\nprofiles = "{folder}/profiles"\n\n'


@pytest.mark.parametrize("home", ["", "relhome"])
def test_a_home_that_is_empty_or_relative_is_usage_through_load(tmp_path, monkeypatch, home):
    monkeypatch.chdir(tmp_path)  # a relative home would resolve against this folder: two callers, two lease stores
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(_write_config(tmp_path)))
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE
    assert "HOME must be an absolute path" in info.value.message and repr(home) in info.value.message


@pytest.mark.parametrize("home", ["", "relhome"])
def test_a_bad_home_is_refused_before_the_default_config_is_looked_for(tmp_path, monkeypatch, home):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", home)
    monkeypatch.delenv("ORION_BROWSER_CONFIG", raising=False)
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE and "HOME must be an absolute path" in info.value.message


@pytest.mark.parametrize("tilde", ["~", "~/s"])
def test_a_tilde_path_under_an_empty_home_is_refused_not_mapped_to_the_root(monkeypatch, tilde):
    monkeypatch.setenv("HOME", "")  # parse(), not load(): load() refuses an empty HOME before it reads any path
    message = refused(paths={"state": tilde, "profiles": "/srv/obtest/profiles"})
    assert "paths.state" in message and "HOME must be an absolute path" in message


@pytest.mark.parametrize("raw, below_home", [("~", ""), ("~/s", "s"), ("~//s", "s"), ("~/a/b", "a/b")])
def test_a_tilde_expands_to_the_home_the_defaults_use(tmp_path, monkeypatch, raw, below_home):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert parse(view={"novnc_dir": raw}).novnc_dir == tmp_path / "home" / below_home


def test_without_home_the_account_home_from_the_user_database_is_used(tmp_path, monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path / "account")))
    cfg = parse(paths={}, view={"novnc_dir": "~/novnc"})
    assert cfg.state_dir == tmp_path / "account" / ".local" / "state" / "orion-browser"
    assert cfg.novnc_dir == tmp_path / "account" / "novnc"


@pytest.mark.parametrize("table", ["chrome", "lease", "timing", "paths", "view"])
def test_a_section_that_is_not_a_table_is_named(table):
    assert f"[{table}] must be a table" in refused(**{table: "wide"})


def test_a_relative_config_variable_is_usage_even_when_that_file_exists(tmp_path, monkeypatch):
    (tmp_path / "rel").mkdir()
    _write_config(tmp_path / "rel", _paths_in(tmp_path))  # valid, so only its relative name can be why it is refused
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORION_BROWSER_CONFIG", "rel/config.toml")
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE and "ORION_BROWSER_CONFIG must be an absolute path" in info.value.message


@pytest.mark.parametrize("profiles", [
    "/srv/x/state/../state/profiles",  # the state folder comes first, so even a plain text comparison sees the overlap
    "/srv/x/other/../state/profiles",  # only a normalised comparison sees this one
])
def test_overlap_through_dot_dot_is_refused(profiles):
    assert "must be separate folders" in refused(paths={"state": "/srv/x/state", "profiles": profiles})


@pytest.mark.parametrize("real, link, target", [
    ("state", "profiles", "state/inner"),  # profiles is a link to a folder inside state
    ("state", "profiles", "state"),  # profiles is a link to state itself
    ("profiles", "state", "profiles/inner"),  # state is a link to a folder inside profiles
])
def test_folders_that_overlap_once_links_are_resolved_are_refused(tmp_path, monkeypatch, real, link, target):
    (tmp_path / real / "inner").mkdir(parents=True)
    (tmp_path / link).symlink_to(tmp_path / target)
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(_write_config(tmp_path, _paths_in(tmp_path))))
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE and "must be separate folders" in info.value.message


def test_load_accepts_separate_folders_even_when_one_is_a_link(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "profiles").symlink_to(tmp_path / "elsewhere")
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(_write_config(tmp_path, _paths_in(tmp_path))))
    cfg = config.load()
    assert (cfg.state_dir, cfg.profiles_dir) == (tmp_path / "state", tmp_path / "profiles")


@pytest.mark.parametrize("pw_dir", ["", "relhome"])
def test_an_account_home_that_is_empty_or_relative_is_usage_through_load(tmp_path, monkeypatch, pw_dir):
    monkeypatch.chdir(tmp_path)  # a relative home would resolve against this folder: two callers, two lease stores
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=pw_dir))
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(_write_config(tmp_path)))  # no [paths]: the defaults need a home
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE
    assert "the account's home in the user database must be an absolute path" in info.value.message
    assert repr(pw_dir) in info.value.message


@pytest.mark.parametrize("pw_dir", ["", "relhome"])
def test_a_bad_account_home_is_refused_before_the_default_config_is_looked_for(tmp_path, monkeypatch, pw_dir):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("ORION_BROWSER_CONFIG", raising=False)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=pw_dir))
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE
    assert "the account's home in the user database must be an absolute path" in info.value.message


def test_a_valid_home_is_used_without_asking_the_user_database(tmp_path, monkeypatch):
    def forbidden(uid):
        raise AssertionError("the user database must not be asked while HOME is set")

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(pwd, "getpwuid", forbidden)
    assert parse(paths={}).state_dir == tmp_path / "home" / ".local" / "state" / "orion-browser"


@pytest.mark.parametrize("home", ["", "relhome"])
def test_a_bad_home_is_refused_even_when_every_path_is_explicit(tmp_path, monkeypatch, home):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(_write_config(tmp_path, _paths_in(tmp_path))))  # nothing needs it
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE
    assert "HOME must be an absolute path" in info.value.message and repr(home) in info.value.message


def test_no_home_at_all_still_loads_when_every_path_is_explicit(tmp_path, monkeypatch):
    def no_entry(uid):
        raise KeyError(f"getpwuid(): uid not found: {uid}")

    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(pwd, "getpwuid", no_entry)
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(_write_config(tmp_path, _paths_in(tmp_path))))
    cfg = config.load()
    assert (cfg.state_dir, cfg.profiles_dir) == (tmp_path / "state", tmp_path / "profiles")


def test_a_dot_git_directory_without_head_still_refuses_the_folders_below_it(tmp_path, monkeypatch):
    (tmp_path / "damaged" / ".git" / "objects").mkdir(parents=True)  # no HEAD, but content: a damaged repository
    paths = f'[paths]\nstate = "{tmp_path}/damaged/state"\nprofiles = "{tmp_path}/profiles"\n\n'
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(_write_config(tmp_path, paths)))
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE
    assert "paths.state" in info.value.message and "inside a git working tree" in info.value.message


def test_the_unit_name_is_the_browser_and_a_tag_of_its_lease_store():
    cfg = parse(browser=[{"name": "work", "screen": 21}, {"name": "yin", "screen": 22}])
    work, yin = cfg.browser("work").unit, cfg.browser("yin").unit
    assert re.fullmatch(r"orion-browser-work-[0-9a-f]{8}\.service", work)
    assert re.fullmatch(r"orion-browser-yin-[0-9a-f]{8}\.service", yin)
    assert work.split("-")[-1] == yin.split("-")[-1]  # one config, one lease store, one tag


@pytest.mark.parametrize(
    "spelling",
    ["/srv/obtest/./state", "/srv/obtest/state/", "/srv/obtest/other/../state", "//srv/obtest/state"],
)
def test_the_same_state_folder_spelled_differently_gives_the_same_unit(spelling):
    same = parse(paths={"state": spelling, "profiles": "/srv/obtest/profiles"})
    assert same.browser("work").unit == parse().browser("work").unit


def test_different_state_folders_give_different_units_for_the_same_browser():
    first = parse(paths={"state": f"{GOLDEN_ROOT}/state", "profiles": f"{GOLDEN_ROOT}/profiles"})
    other = parse(paths={"state": f"{GOLDEN_ROOT}/state-b", "profiles": f"{GOLDEN_ROOT}/profiles"})
    assert other.browser("work").unit == "orion-browser-work-ba79e5cf.service"  # sha256(b"<root>/state-b")
    assert other.browser("work").unit != first.browser("work").unit


def test_the_profiles_folder_does_not_change_the_unit():
    moved = parse(paths={"state": "/srv/obtest/state", "profiles": "/srv/obtest/elsewhere"})
    assert moved.browser("work").unit == parse().browser("work").unit


@pytest.mark.parametrize("reach", ["state-link", "alias/state"])
def test_a_state_folder_reached_through_a_link_gives_the_same_unit(tmp_path, reach):
    (tmp_path / "real" / "state").mkdir(parents=True)
    (tmp_path / "state-link").symlink_to(tmp_path / "real" / "state")  # the folder itself is a link
    (tmp_path / "alias").symlink_to(tmp_path / "real")  # a folder above it is a link
    profiles = str(tmp_path / "profiles")
    direct = parse(paths={"state": str(tmp_path / "real" / "state"), "profiles": profiles}).browser("work").unit
    through = parse(paths={"state": str(tmp_path / reach), "profiles": profiles}).browser("work").unit
    assert through == direct


def test_a_link_gives_the_unit_of_the_folder_it_points_to_not_one_of_its_own(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "two")
    units = {
        name: parse(paths={"state": str(tmp_path / name), "profiles": str(tmp_path / "profiles")}).browser("work").unit
        for name in ("one", "two", "link")
    }
    assert units["link"] == units["two"]
    assert units["link"] != units["one"] and units["two"] != units["one"]


def test_an_empty_dot_git_directory_is_not_a_repository(tmp_path, monkeypatch):
    (tmp_path / "mount" / ".git").mkdir(parents=True)  # what another program's sandbox leaves in place for a moment
    paths = f'[paths]\nstate = "{tmp_path}/mount/state"\nprofiles = "{tmp_path}/profiles"\n\n'
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(_write_config(tmp_path, paths)))
    assert config.load().state_dir == tmp_path / "mount" / "state"


def test_an_empty_dot_git_directory_does_not_hide_a_repository_above_it(tmp_path, monkeypatch):
    _repo(tmp_path / "repo")
    (tmp_path / "repo" / "sub" / ".git").mkdir(parents=True)  # empty: skipped, and the walk goes on up
    paths = f'[paths]\nstate = "{tmp_path}/repo/sub/state"\nprofiles = "{tmp_path}/profiles"\n\n'
    monkeypatch.setenv("ORION_BROWSER_CONFIG", str(_write_config(tmp_path, paths)))
    with pytest.raises(BrowserError) as info:
        config.load()
    assert info.value.code is Code.USAGE and "inside a git working tree" in info.value.message


def test_a_dot_git_directory_that_vanishes_during_the_listing_counts_as_absent(tmp_path, monkeypatch):
    (tmp_path / "tree" / ".git").mkdir(parents=True)
    real = config.os.scandir

    def vanishing(path, *args, **kwargs):
        if Path(path) == tmp_path / "tree" / ".git":
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(config.os, "scandir", vanishing)
    assert config._git_tree_problem(tmp_path / "tree" / "state") is None


def test_a_dot_git_directory_that_cannot_be_listed_refuses_the_path(tmp_path, monkeypatch):
    (tmp_path / "tree" / ".git").mkdir(parents=True)
    real = config.os.scandir

    def unreadable(path, *args, **kwargs):
        if Path(path) == tmp_path / "tree" / ".git":
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(config.os, "scandir", unreadable)
    assert "could not be checked for a git working tree" in config._git_tree_problem(tmp_path / "tree" / "state")


def test_the_shipped_example_config_parses():
    import tomllib

    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    cfg = config.parse(tomllib.loads(example.read_text()), example)
    assert sorted(cfg.browsers) == ["anon", "work"]
