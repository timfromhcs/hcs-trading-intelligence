"""Run the quality gates in order and write a machine-readable JSON report.

Each gate is retried at most three times (AGENTS.md 14.2); the report records the exact
command, exit codes, attempts, pytest counts and the git SHA. Nothing is skipped or
downgraded: a gate that never exits 0 fails the run.
"""

import argparse
import json
import re
import subprocess  # noqa: S404
import sys
from datetime import UTC, datetime
from pathlib import Path

MAX_ATTEMPTS = 3
_COUNTS = re.compile(r"(\d+) (passed|failed|skipped|error|errors|xfailed|xpassed)")
_SECRET = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bghp_[0-9A-Za-z]{36}\b"
)

DEFAULT_GATES: tuple[tuple[str, list[str]], ...] = (
    ("ruff-check", ["uv", "run", "--frozen", "ruff", "check", "."]),
    ("ruff-format", ["uv", "run", "--frozen", "ruff", "format", "--check", "."]),
    ("mypy", ["uv", "run", "--frozen", "mypy"]),
    (
        "pytest",
        [
            "uv",
            "run",
            "--frozen",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--cov-report=term",
            "--cov-report=xml:coverage.xml",
        ],
    ),
    (
        "coverage-floors",
        ["uv", "run", "--frozen", "python", "scripts/check_coverage_floors.py"],
    ),
    (
        "eval-model",
        ["uv", "run", "--frozen", "python", "-m", "hcs_trader", "eval-model"],
    ),
    ("secret-scan", ["internal:secret-scan"]),
)


def parse_pytest_counts(output: str) -> dict[str, int]:
    """Extract counts from pytest's final summary line."""
    lines = [line for line in output.splitlines() if _COUNTS.search(line)]
    counts: dict[str, int] = {}
    if lines:
        for number, label in _COUNTS.findall(lines[-1]):
            counts[label] = int(number)
    return counts


def git_sha(root: Path) -> str | None:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    sha = result.stdout.strip()
    return (
        sha if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None
    )


def secret_scan(root: Path) -> tuple[int, str]:
    """Scan git-tracked files for private keys and well-known token shapes."""
    listed = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "ls-files", "-z"],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        return 2, "git ls-files failed"
    hits = []
    for name in listed.stdout.decode("utf-8", "replace").split("\0"):
        path = root / name
        if name and path.is_file() and path.stat().st_size < 5_000_000:  # noqa: PLR2004
            if _SECRET.search(path.read_bytes()):
                hits.append(name)
    return (1, "secret-like content in: " + ", ".join(hits)) if hits else (0, "clean")


def _execute(command: list[str], root: Path) -> tuple[int, str]:
    if command == ["internal:secret-scan"]:
        return secret_scan(root)
    try:
        result = subprocess.run(  # noqa: S603
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return 127, str(error)
    return result.returncode, (result.stdout + result.stderr)[-4000:]


def run_gates(
    gates: tuple[tuple[str, list[str]], ...],
    root: Path,
    *,
    stop_on_failure: bool = True,
) -> dict[str, object]:
    results: list[dict[str, object]] = []
    for name, command in gates:
        attempts: list[int] = []
        output = ""
        for _ in range(MAX_ATTEMPTS):
            code, output = _execute(command, root)
            attempts.append(code)
            if code == 0:
                break
        entry: dict[str, object] = {
            "name": name,
            "command": command,
            "exit_code": attempts[-1],
            "attempts": len(attempts),
            "attempt_exit_codes": attempts,
            "output_tail": output[-1000:],
        }
        if name == "pytest":
            entry["pytest_counts"] = parse_pytest_counts(output)
        results.append(entry)
        if attempts[-1] != 0 and stop_on_failure:
            break
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha(root),
        "all_passed": len(results) == len(gates)
        and all(r["exit_code"] == 0 for r in results),
        "gates": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("gates-report.json"))
    parser.add_argument("--keep-going", action="store_true")
    args = parser.parse_args()
    report = run_gates(DEFAULT_GATES, args.root, stop_on_failure=not args.keep_going)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {args.output}; all_passed={report['all_passed']}")
    sys.exit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
