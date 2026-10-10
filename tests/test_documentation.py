import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def test_active_documentation_local_links_resolve():
    paths = [
        ROOT / "README.md",
        ROOT / "CONTRIBUTING.md",
        *ROOT.glob("docs/**/*.md"),
        *ROOT.glob("configs/*.md"),
        *ROOT.glob("evidence/*.md"),
        *ROOT.glob("benchmarks/*.md"),
    ]
    failures = []
    for path in paths:
        if "reference" in path.relative_to(ROOT).parts:
            continue  # Historical snapshots retain their original references.
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text("utf-8")):
            if target.startswith(("https://", "http://", "#", "mailto:")):
                continue
            target = unquote(target.split("#")[0])
            if not (path.parent / target).exists():
                failures.append(f"{path.relative_to(ROOT)} -> {target}")
    assert not failures, "\n".join(failures)
