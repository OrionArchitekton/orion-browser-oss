"""The repository must stay publishable: only the kinds of file it holds (source, tests, Markdown, TOML, workflows,
documentation images), all of them UTF-8 text but the images, no browser artifacts or profile files, no symbolic
links, no personal addresses, no long dashes, and a .gitignore that keeps artifacts out before they are staged."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ARTIFACT_SUFFIXES = IMAGE_SUFFIXES | {".xwd", ".jsonl", ".mp4", ".webm", ".har", ".sqlite", ".db"}
IMAGE_DIR = "docs/assets/"  # documentation images, and nothing else, may live here
# The kinds of file this repository holds. Anything else is refused, whatever it is: an allowlist, so an artifact
# nobody thought to name (a cookie export, a browser database) cannot slip through. Extend it deliberately.
ALLOWED_FILES = {"LICENSE", "MANIFEST.in", ".gitignore", "pyproject.toml", "config.example.toml", ".github/dependabot.yml"}
ALLOWED_PATTERNS = ("*.md", "src/orion_browser/*.py", "tests/*.py", "tests/fakes/*.py", ".github/workflows/*.yml")
FORBIDDEN_PREFIXES = ("docs/superpowers/",)  # internal planning documents never enter the repository
# Files a Chrome profile holds: cookies, saved logins, form data, history, settings, the DevTools port.
PROFILE_FILES = {
    "Cookies", "Cookies-journal", "Login Data", "Login Data-journal", "Web Data", "Web Data-journal",
    "History", "History-journal", "Local State", "Preferences", "Secure Preferences", "DevToolsActivePort",
}
SQLITE_HEADER = b"SQLite format 3\x00"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "users.noreply.github.com")
LONG_DASHES = re.compile("[" + "".join(map(chr, (0x2013, 0x2014, 0x2015))) + "]")  # built from code points


def repo_files() -> list[Path]:
    """Every path git would commit: tracked paths plus untracked paths that .gitignore does not cover, symbolic links
    and gitlinks included."""
    try:
        # -z because plain output C-quotes unusual names (non-ASCII, quotes, tabs) and a quoted name is not a file
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        if os.environ.get("CI"):
            raise
        pytest.skip("not a git checkout")
    names = [os.fsdecode(raw) for raw in out.split(b"\0") if raw]
    paths = sorted({ROOT / name for name in names if (ROOT / name).exists() or (ROOT / name).is_symlink()})
    assert ROOT / "pyproject.toml" in paths, "file listing looks wrong; the hygiene guard would pass vacuously"
    return paths


def regular_files() -> list[Path]:
    return [path for path in repo_files() if path.is_file() and not path.is_symlink()]


def is_documentation_image(path: Path) -> bool:
    return str(path.relative_to(ROOT)).startswith(IMAGE_DIR) and path.suffix.lower() in IMAGE_SUFFIXES


def allowed(path: Path) -> bool:
    name = str(path.relative_to(ROOT))
    if name.startswith(FORBIDDEN_PREFIXES):
        return False
    return (
        name in ALLOWED_FILES
        or is_documentation_image(path)
        or any(fnmatch.fnmatchcase(name, pattern) for pattern in ALLOWED_PATTERNS)
    )


def text_of(path: Path) -> str | None:
    """The file as UTF-8 text, or None when it is not UTF-8 text (undecodable, or holding a NUL byte)."""
    raw = path.read_bytes()
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def texts() -> list[tuple[Path, str]]:
    found = []
    for path in regular_files():
        text = None if is_documentation_image(path) else text_of(path)
        if text is not None:
            found.append((path, text))
    return found


def allowed_email(address: str) -> bool:
    domain = address.rsplit("@", 1)[1].lower()
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS)


def ignored(path: str) -> bool:
    """Whether .gitignore covers the path (checked by rule, whether or not such a file exists)."""
    proc = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT, capture_output=True)
    if proc.returncode not in (0, 1):
        raise AssertionError(f"git check-ignore failed for {path}: {proc.stderr!r}")
    return proc.returncode == 0


def gitlinks() -> list[str]:
    """Paths the index records as gitlinks (mode 160000), whether or not their directory is checked out."""
    out = subprocess.run(["git", "ls-files", "-s", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [os.fsdecode(entry.split(b"\t", 1)[1]) for entry in out.split(b"\0") if entry.startswith(b"160000 ")]


def test_only_the_kinds_of_file_this_repository_holds():
    bad = [str(path.relative_to(ROOT)) for path in repo_files() if not allowed(path)]
    assert not bad, (
        "not a kind of file this repository holds (source, tests, Markdown, TOML, workflows, documentation images); "
        f"if one is real, add it to ALLOWED_FILES or ALLOWED_PATTERNS deliberately: {bad}"
    )


def test_no_symbolic_links():
    links = [str(path.relative_to(ROOT)) for path in repo_files() if path.is_symlink()]
    assert not links, f"symbolic links are not committed (their targets escape every check): {links}"


def test_no_gitlinks():
    on_disk = [str(path.relative_to(ROOT)) for path in repo_files() if path.is_dir() and not path.is_symlink()]
    nested = sorted(set(gitlinks()) | set(on_disk))
    assert not nested, f"nested repositories (gitlinks) are not committed: {nested}"


def test_no_browser_artifacts_or_profile_files_are_committed():
    bad = []
    for path in regular_files():
        name = str(path.relative_to(ROOT))
        if path.suffix.lower() in ARTIFACT_SUFFIXES and not is_documentation_image(path):
            bad.append(name)
        elif path.name in PROFILE_FILES or path.name == "config.toml" or path.read_bytes()[:16] == SQLITE_HEADER:
            bad.append(name)
    assert not bad, f"artifacts, profile files and config must be gitignored or absent, never committed: {bad}"


def test_every_committed_file_is_utf8_text():
    bad = [str(p.relative_to(ROOT)) for p in regular_files() if not is_documentation_image(p) and text_of(p) is None]
    assert not bad, f"only UTF-8 text may be committed (images under {IMAGE_DIR} excepted): {bad}"


def test_no_real_email_addresses():
    hits = [
        f"{path.relative_to(ROOT)}: {match.group()}"
        for path, text in texts()
        for match in EMAIL.finditer(text)
        if not allowed_email(match.group())
    ]
    assert not hits, hits


def test_no_long_dashes():
    hits = [str(path.relative_to(ROOT)) for path, text in texts() if LONG_DASHES.search(text)]
    assert not hits, hits


def test_gitignore_keeps_every_artifact_kind_out():
    repo_files()  # skips outside a git checkout, fails in CI
    must_ignore = [f"probe/file{suffix}" for suffix in sorted(ARTIFACT_SUFFIXES)]
    must_ignore += [f"probe/{name}" for name in sorted(PROFILE_FILES)]
    must_ignore += [
        "probe/config.toml", f"{IMAGE_DIR}session.jsonl", "docs/superpowers/plans/plan.md",
        "probe/Default/Local Storage/leveldb/000003.log", "probe/Profile 2/Login Data For Account",
        "probe/Guest Profile/Login Data For Account", "probe/System Profile/Extension Cookies",
        "probe/storage_state.json", "probe/cookies-export.json", "probe/cookies.txt",
    ]
    missed = [path for path in must_ignore if not ignored(path)]
    assert not missed, f".gitignore lets these through: {missed}"
    for image in (f"{IMAGE_DIR}diagram.png", f"{IMAGE_DIR}demo.gif"):
        assert not ignored(image), f"documentation images must stay committable: {image}"
