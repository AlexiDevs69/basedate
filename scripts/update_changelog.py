"""Generate an AlexiHub release entry from the commits in a Git range.

The script intentionally uses only the Python standard library so it can run
inside GitHub Actions without installing project dependencies.

Preferred commit format:
    feat: додано пошук публічних серверів
    fix(dm): виправлено завантаження профілю
    perf!: змінено протокол синхронізації

If commit messages are generic (for example, "update" or
"Add files via upload"), the script falls back to the changed file paths and
creates a conservative user-facing summary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RELEASES_PATH = REPO_ROOT / "community" / "releases.json"
ZERO_SHA = "0" * 40

CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(?P<type>feat|fix|perf|security|refactor|docs|style|test|chore|ci|build)"
    r"(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s*(?P<message>.+)$",
    re.IGNORECASE,
)

SKIPPED_TYPES = {"docs", "style", "test", "chore", "ci", "build"}
GENERIC_SUBJECTS = {
    "add files via upload",
    "change files",
    "changes",
    "fix",
    "fixed",
    "initial commit",
    "merge",
    "patch",
    "update",
    "updated",
    "updates",
}
IGNORED_PATHS = {
    ".github/workflows/auto-changelog.yml",
    "community/releases.json",
    "scripts/update_changelog.py",
}


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str


def run_git(*args: str, allow_failure: bool = False) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode and not allow_failure:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def normalize_base(base: str, head: str) -> str:
    base = (base or "").strip()
    if base and base != ZERO_SHA:
        valid = subprocess.run(
            ["git", "rev-parse", "--verify", f"{base}^{{commit}}"],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if valid.returncode == 0:
            return base

    parent = run_git("rev-parse", f"{head}^", allow_failure=True)
    return parent or head


def collect_commits(base: str, head: str) -> list[Commit]:
    if base == head:
        shas = [head]
    else:
        shas = run_git("rev-list", "--reverse", f"{base}..{head}").splitlines()

    commits: list[Commit] = []
    for sha in shas:
        sha = sha.strip()
        if not sha:
            continue
        commits.append(
            Commit(
                sha=sha,
                subject=run_git("show", "-s", "--format=%s", sha),
                body=run_git("show", "-s", "--format=%b", sha),
            )
        )
    return commits


def changed_paths(base: str, head: str) -> list[str]:
    if base == head:
        output = run_git(
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            head,
        )
    else:
        output = run_git("diff", "--name-only", base, head)
    return sorted(
        {
            path.strip()
            for path in output.splitlines()
            if path.strip() and path.strip() not in IGNORED_PATHS
        }
    )


def sentence(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip(" .;:-")
    if not cleaned:
        return ""
    cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned if cleaned.endswith((".", "!", "?")) else f"{cleaned}."


def humanize_commit(kind: str, message: str) -> str:
    message = re.sub(r"^(?:add|added|fix|fixed|improve|improved)\s*:\s*", "", message, flags=re.I)
    lowered = message.casefold()
    ready_prefixes = (
        "додано",
        "додали",
        "виправлено",
        "виправили",
        "змінено",
        "оновлено",
        "покращено",
        "added",
        "fixed",
        "improved",
        "updated",
    )
    if lowered.startswith(ready_prefixes):
        return sentence(message)

    prefixes = {
        "feat": "Додано",
        "fix": "Виправлено",
        "perf": "Покращено продуктивність",
        "security": "Покращено безпеку",
        "refactor": "Покращено внутрішню реалізацію",
    }
    return sentence(f"{prefixes.get(kind, 'Оновлено')}: {message}")


def parse_commit(commit: Commit) -> tuple[str | None, str | None, bool]:
    subject = commit.subject.strip()
    if "[skip changelog]" in subject.casefold():
        return None, None, False

    match = CONVENTIONAL_COMMIT_RE.match(subject)
    if match:
        kind = match.group("type").casefold()
        breaking = bool(match.group("breaking")) or "breaking change" in commit.body.casefold()
        if kind in SKIPPED_TYPES:
            return None, None, breaking
        return kind, humanize_commit(kind, match.group("message")), breaking

    if subject.casefold().startswith("merge "):
        body_title = next((line.strip() for line in commit.body.splitlines() if line.strip()), "")
        nested = CONVENTIONAL_COMMIT_RE.match(body_title)
        if nested:
            kind = nested.group("type").casefold()
            breaking = bool(nested.group("breaking")) or "breaking change" in commit.body.casefold()
            if kind not in SKIPPED_TYPES:
                return kind, humanize_commit(kind, nested.group("message")), breaking
        return None, None, False

    normalized = re.sub(r"\s+", " ", subject).strip().casefold()
    if normalized in GENERIC_SUBJECTS or len(normalized) < 8:
        return None, None, False

    plain_patterns = (
        (r"^(?:add|added|create|created|implement|implemented|introduce|introduced)\s+(.+)$", "feat"),
        (r"^(?:fix|fixed|repair|repaired|resolve|resolved)\s+(.+)$", "fix"),
        (r"^(?:improve|improved|optimize|optimized)\s+(.+)$", "perf"),
        (r"^(?:secure|secured|harden|hardened)\s+(.+)$", "security"),
        (r"^(?:refactor|refactored|rework|reworked)\s+(.+)$", "refactor"),
    )
    for pattern, kind in plain_patterns:
        plain_match = re.match(pattern, subject, flags=re.I)
        if plain_match:
            return (
                kind,
                humanize_commit(kind, plain_match.group(1)),
                "breaking change" in commit.body.casefold(),
            )

    return "fix", sentence(subject), "breaking change" in commit.body.casefold()


def fallback_changes(paths: list[str]) -> list[str]:
    path_set = set(paths)
    changes: list[str] = []

    def has(*needles: str) -> bool:
        return any(any(needle in path for needle in needles) for path in path_set)

    if has("dm_chat", "dm_", "friend"):
        changes.append("Оновлено особисті повідомлення, друзів і профілі співрозмовників.")
    if has("travel", "server_onboarding"):
        changes.append("Оновлено каталог публічних серверів та знайомство зі спільнотами.")
    if has("server_channel", "server_home", "server_settings", "server_create"):
        changes.append("Оновлено сервери, канали та їхні налаштування.")
    if has("public_profile", "profile"):
        changes.append("Оновлено профілі користувачів і пов’язані дії.")
    if "community/templates/home.html" in path_set:
        changes.append("Оновлено головну сторінку AlexiHub.")
    if has("router.py", "crud.py", "models.py", "database.py"):
        changes.append("Покращено серверну логіку, роботу з даними та стабільність.")
    if has("static/", ".css", ".svg", ".png", ".webp"):
        changes.append("Покращено оформлення та візуальні елементи інтерфейсу.")
    if has("requirements", "pyproject", "package.json", "Cargo.toml"):
        changes.append("Оновлено технічні залежності застосунку.")

    if not changes and paths:
        changes.append("Внесено технічні покращення та виправлення стабільності.")
    return changes


def deduplicate(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def parse_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", value.strip())
    if not match:
        raise ValueError(f"Unsupported version: {value!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def bump_version(current: str, bump: str) -> str:
    major, minor, patch = parse_version(current)
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def choose_bump(kinds: list[str], breaking: bool, forced: str) -> str:
    if forced != "auto":
        return forced
    if breaking:
        return "major"
    if "feat" in kinds:
        return "minor"
    return "patch"


def release_title(kinds: list[str], bump: str) -> str:
    if bump == "major":
        return "Велике оновлення AlexiHub"
    if "feat" in kinds:
        return "Нові можливості AlexiHub"
    if kinds and all(kind in {"fix", "security"} for kind in kinds):
        return "Виправлення та стабільність"
    return "Оновлення AlexiHub"


def write_github_output(changed: bool, version: str = "", change_count: int = 0) -> None:
    output_path = os.getenv("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"changed={'true' if changed else 'false'}\n")
        output.write(f"version={version}\n")
        output.write(f"change_count={change_count}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="", help="First Git revision (exclusive).")
    parser.add_argument("--head", default="HEAD", help="Last Git revision (inclusive).")
    parser.add_argument("--releases", type=Path, default=DEFAULT_RELEASES_PATH)
    parser.add_argument("--date", default="", help="Release date in YYYY-MM-DD format.")
    parser.add_argument(
        "--force-type",
        choices=("auto", "patch", "minor", "major"),
        default="auto",
        help="Override automatic semantic version selection.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    releases_path = args.releases.resolve()
    payload = json.loads(releases_path.read_text(encoding="utf-8"))
    releases = payload.get("releases") or []
    if not releases:
        raise RuntimeError("community/releases.json has no releases")

    head = run_git("rev-parse", args.head)
    base = normalize_base(args.base, head)
    commits = collect_commits(base, head)
    paths = changed_paths(base, head)

    parsed = [parse_commit(commit) for commit in commits]
    kinds = [kind for kind, change, _ in parsed if kind and change]
    changes = [change for _, change, _ in parsed if change]
    breaking = any(is_breaking for _, _, is_breaking in parsed)
    has_conventional_commit = any(
        CONVENTIONAL_COMMIT_RE.match(commit.subject.strip())
        for commit in commits
        if "[skip changelog]" not in commit.subject.casefold()
    )

    # An explicitly skipped Conventional Commit (docs/chore/test/...) must not
    # turn into a release merely because it touched a mapped file. Path-based
    # fallback is reserved for vague, non-conventional commit messages.
    if not changes and not has_conventional_commit:
        changes = fallback_changes(paths)
        if changes:
            kinds = ["fix"]

    changes = deduplicate(changes)
    if not changes:
        print("No user-facing changes detected; releases.json was not modified.")
        write_github_output(False)
        return 0

    current_version = str(releases[0].get("version") or "1.0.0")
    bump = choose_bump(kinds, breaking, args.force_type)
    next_version = bump_version(current_version, bump)
    release_date = args.date or datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()

    if any(release.get("source_sha") == head for release in releases):
        print(f"Commit {head[:8]} is already present in releases.json.")
        write_github_output(False, current_version)
        return 0

    release = {
        "version": next_version,
        "date": release_date,
        "title": release_title(kinds, bump),
        "changes": changes,
        "source_sha": head,
    }
    payload["releases"] = [release, *releases]

    print(json.dumps(release, ensure_ascii=False, indent=2))
    if not args.dry_run:
        releases_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    write_github_output(True, next_version, len(changes))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"auto-changelog error: {exc}", file=sys.stderr)
        raise SystemExit(1)
