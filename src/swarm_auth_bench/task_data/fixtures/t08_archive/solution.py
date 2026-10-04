from pathlib import Path


def safe_member_path(root, member):
    """Resolve an archive member under the extraction root."""
    return Path(root) / member
