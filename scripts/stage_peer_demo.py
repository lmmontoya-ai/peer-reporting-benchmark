"""Stage a local demo without inference, credentials, or run artifacts."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ASSETS = ("demo.html", "demo.css", "demo.js", "review.html", "review.css", "review.js", "packet-format-v3.svg")


def stage(output: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "docs" / "long-run-explanation"
    output = output.resolve()
    if output.exists():
        raise ValueError("demo output must be a new directory")
    if output.is_relative_to(source.resolve()) or source.resolve().is_relative_to(output):
        raise ValueError("demo output must be outside the authored UI source")
    output.mkdir(parents=True)
    for name in ASSETS:
        shutil.copyfile(source / name, output / name)
    # Documentation stays available when this staging directory is served alone.
    docs = source.parent
    for name in ("peer-reporting-spec.md", "peer-reporting-source-review.md",
                 "peer-reporting-resource-proposal.json"):
        shutil.copyfile(docs / name, output / name)
    page = output / "demo.html"
    text = page.read_text(encoding="utf-8")
    for name in ("peer-reporting-spec.md", "peer-reporting-source-review.md",
                 "peer-reporting-resource-proposal.json"):
        text = text.replace("../" + name, name)
    page.write_text(text, encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".local/demo-v2"))
    args = parser.parse_args()
    try:
        print(stage(args.output))
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
