import subprocess


def get_git_commit(path: str) -> str:
    """Gets the current git commit hash for the repository.

    Args:
        path: Path within the git repository.

    Returns:
        The current commit hash, or empty string if not a git repo or error.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
