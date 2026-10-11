import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def active_documentation_paths():
    return [
        path
        for path in [
            ROOT / "README.md",
            ROOT / "CONTRIBUTING.md",
            *ROOT.glob("docs/**/*.md"),
            *ROOT.glob("configs/*.md"),
            *ROOT.glob("evidence/*.md"),
            *ROOT.glob("benchmarks/*.md"),
        ]
        if "reference" not in path.relative_to(ROOT).parts
    ]


def test_active_documentation_local_links_resolve():
    failures = []
    for path in active_documentation_paths():
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text("utf-8")):
            if target.startswith(("https://", "http://", "#", "mailto:")):
                continue
            # Active docs must also work outside the author's workstation.
            if re.match(r"^(?:[A-Za-z]:|[/\\])", target):
                failures.append(f"{path.relative_to(ROOT)} -> absolute link: {target}")
                continue
            target = unquote(target.split("#")[0])
            if not (path.parent / target).exists():
                failures.append(f"{path.relative_to(ROOT)} -> {target}")
    assert not failures, "\n".join(failures)


def test_active_documentation_code_fences_balance():
    failures = []
    for path in active_documentation_paths():
        opened = None
        for line in path.read_text("utf-8").splitlines():
            fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if not fence:
                continue
            marker, suffix = fence.groups()
            if opened is None:
                opened = marker
            elif (
                marker[0] == opened[0]
                and len(marker) >= len(opened)
                and not suffix.strip()
            ):
                opened = None
        if opened:
            failures.append(str(path.relative_to(ROOT)))
    assert not failures, "Unclosed code fences: " + ", ".join(failures)
