"""Stage the bundled audited public demo without inference or private inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path, PurePosixPath

ASSETS = ("demo.html", "demo.css", "demo.js", "review.html", "review.css", "review.js", "packet-format-v3.svg")
SOURCE_DIR = Path(__file__).resolve().parents[1] / "docs" / "long-run-explanation"
DOCUMENTS = ("peer-reporting-spec.md", "peer-reporting-source-review.md", "peer-reporting-resource-proposal.json",
             "peer-reporting-project-writeup.md", "peer-reporting-mechanical-results-audit-sol-v1.json",
             "public-results-projection.md", "collection-admission-amendment.md")


def projection_files(source: Path) -> list[str]:
    manifest_path = source / "projection-manifest.json"
    if not manifest_path.is_file():
        raise ValueError("bundled audited public results are absent; serve the published docs tree instead")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("kind") != "public_peer_results_projection_manifest_v1"
            or manifest.get("reviewer_packets_included") is not False
            or manifest.get("controller_bindings_included") is not False):
        raise ValueError("expected the audited public projection manifest")
    names = []
    for entry in manifest["files"]:
        relative = entry["path"]
        path = PurePosixPath(relative)
        if (not relative or "\\" in relative or ":" in relative or path.is_absolute() or ".." in path.parts
                or path.parts[0] not in {"results-v1", "collection-v1"}):
            raise ValueError("unsafe public projection path")
        candidate = (source / relative).resolve()
        if not candidate.is_relative_to(source.resolve()) or not candidate.is_file():
            raise ValueError("public projection file is absent or outside the source")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("bundled public projection hash mismatch")
        names.append(relative)
    if len(names) != len(set(names)) or len(names) != manifest["file_count"]:
        raise ValueError("public projection file list repeats or differs from its count")
    if not {"results-v1/summary.json", "collection-v1/index.json"} <= set(names):
        raise ValueError("public demo requires bundled factual results and evidence index")
    return names


def stage(output: Path) -> Path:
    source = SOURCE_DIR.resolve()
    output = output.resolve()
    if output.exists():
        raise ValueError("demo output must be a new directory")
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("demo output must be outside the authored UI source")
    names = projection_files(source)
    docs = source.parent
    for name in ASSETS:
        if not (source / name).is_file():
            raise ValueError("public demo authored asset is absent")
    for name in DOCUMENTS:
        if not (docs / name).is_file():
            raise ValueError("public demo documentation is absent")
    output.mkdir(parents=True)
    for relative in (*ASSETS, *names, "projection-manifest.json"):
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    for name in DOCUMENTS:
        shutil.copyfile(docs / name, output / name)
    rendered = "project.html"
    if (docs / rendered).is_file():
        shutil.copyfile(docs / rendered, output / rendered)
    execution = "peer-reporting-final-execution-audit-sol-v1.json"
    if (docs / execution).is_file():
        shutil.copyfile(docs / execution, output / execution)
    else:
        writeup = output / "peer-reporting-project-writeup.md"
        text = writeup.read_text(encoding="utf-8")
        text = re.sub(r"\[([^\]]+)\]\(" + re.escape(execution) + r"\)", r"\1 (execution audit not bundled)", text)
        writeup.write_text(text, encoding="utf-8")
    for name in ("demo.html", "peer-reporting-project-writeup.md"):
        page = output / name
        text = page.read_text(encoding="utf-8")
        for document in (*DOCUMENTS, execution):
            text = text.replace("../" + document, document)
        text = text.replace("../project.html", "project.html" if (docs / rendered).is_file() else "peer-reporting-project-writeup.md")
        page.write_text(text, encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".local/demo-v3"))
    args = parser.parse_args()
    try:
        print(stage(args.output))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
