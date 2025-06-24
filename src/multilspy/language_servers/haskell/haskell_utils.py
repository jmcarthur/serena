"""
Shared utilities for Haskell language server implementations.
Contains common functions and constants used by both multilspy and solidlsp.
"""

import json
import os
import pathlib
import shutil
import subprocess

# Directories to ignore when searching for Haskell files
HASKELL_IGNORE_DIRS = [".stack-work", "dist-newstyle", ".hie-bios", ".cabal-sandbox"]


def get_ghc_version() -> str | None:
    """Get the installed GHC version or None if not found."""
    try:
        result = subprocess.run(["ghc", "--version"], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    return None


def get_hls_version() -> str | None:
    """Get the installed HLS version or None if not found."""
    try:
        result = subprocess.run(["haskell-language-server-wrapper", "--version"], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    return None


def check_hls_dependency() -> None:
    """Check for haskell-language-server-wrapper and raise RuntimeError if not found."""
    if not shutil.which("haskell-language-server-wrapper"):
        ghc_version = get_ghc_version()
        if ghc_version:
            raise RuntimeError(
                f"Found {ghc_version} but haskell-language-server-wrapper is not in PATH.\n"
                f"Please install HLS using one of these methods:\n"
                f"  1. ghcup: ghcup install hls\n"
                f"  2. Stack: stack install haskell-language-server\n"
                f"  3. From releases: https://github.com/haskell/haskell-language-server/releases\n"
                f"Make sure haskell-language-server-wrapper is in your PATH after installation."
            )
        raise RuntimeError(
            "haskell-language-server-wrapper not found in PATH.\n"
            "Please install Haskell and HLS first:\n"
            "  1. Install GHC/Stack/Cabal\n"
            "  2. Install HLS using ghcup: ghcup install hls\n"
            "  3. Ensure haskell-language-server-wrapper is in your PATH"
        )


def is_haskell_ignored_dirname(dirname: str) -> bool:
    """Check if a directory should be ignored for Haskell projects."""
    return dirname in HASKELL_IGNORE_DIRS


def get_haskell_initialize_params(repository_absolute_path: str, params_file_path: str) -> dict:
    """
    Returns the initialize params for the Haskell Language Server.

    Args:
        repository_absolute_path: Absolute path to the repository root
        params_file_path: Path to the initialize_params.json file

    Returns:
        Dictionary with initialization parameters

    """
    with open(params_file_path, encoding="utf-8") as f:
        d = json.load(f)

    del d["_description"]

    d["processId"] = os.getpid()
    assert d["rootPath"] == "$rootPath"
    d["rootPath"] = repository_absolute_path

    assert d["rootUri"] == "$rootUri"
    d["rootUri"] = pathlib.Path(repository_absolute_path).as_uri()

    assert d["workspaceFolders"][0]["uri"] == "$uri"
    d["workspaceFolders"][0]["uri"] = pathlib.Path(repository_absolute_path).as_uri()

    assert d["workspaceFolders"][0]["name"] == "$name"
    d["workspaceFolders"][0]["name"] = os.path.basename(repository_absolute_path)

    return d
