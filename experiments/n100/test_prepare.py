"""Bounded offline packaging checks. Pass a local public Git repository."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main(repository):
    here = Path(__file__).resolve().parent
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("n100_prepare", here / "prepare.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repository = Path(repository).resolve()
    before = subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"])
    with tempfile.TemporaryDirectory(prefix="n100-overlay-") as temp:
        root = Path(temp)
        destination = root / "prepared"
        module.prepare(repository, destination)
        audit = json.loads((destination / "offline-check/verification.json").read_text(encoding="utf-8"))
        assert audit["valid"] and audit["counts"] == {"collection": 54, "smoke": 9}
        for bad in (destination, repository / "forbidden-new-tree"):
            try:
                module.prepare(repository, bad)
            except ValueError:
                pass
            else:
                raise AssertionError("unsafe destination accepted")
        copied = root / "tampered-overlay"
        shutil.copytree(here, copied)
        target = copied / "overlay/src/swarm_auth_bench/peer_reporting/fixtures.py"
        target.write_bytes(target.read_bytes() + b"# tampered\n")
        result = subprocess.run([sys.executable, str(copied / "prepare.py"), "--repository", str(repository), "--destination", str(root / "rejected")], capture_output=True, text=True)
        assert result.returncode != 0 and "overlay hash mismatch" in result.stderr
        assert not (root / "rejected").exists()
    assert subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"]) == before
    print("offline packaging checks passed: reproduction, destinations, tamper, unchanged checkout")


if __name__ == "__main__":
    main(sys.argv[1])
