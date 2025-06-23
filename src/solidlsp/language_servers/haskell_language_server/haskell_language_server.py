import json
import logging
import os
import pathlib
import shutil
import subprocess
import threading

from overrides import override

from multilspy.lsp_protocol_handler.lsp_types import InitializeParams
from multilspy.lsp_protocol_handler.server import ProcessLaunchInfo
from multilspy.multilspy_config import MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger
from solidlsp.ls import SolidLanguageServer


class HaskellLanguageServer(SolidLanguageServer):
    """
    Provides Haskell specific instantiation of the LanguageServer class using haskell-language-server.
    """
    
    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        # For Haskell projects, we should ignore:
        # - .stack-work: Stack build artifacts
        # - dist-newstyle: Cabal build artifacts
        # - .hie-bios: HLS cache/session files
        # - .cabal-sandbox: Old-style Cabal sandbox
        haskell_ignore_dirs = [".stack-work", "dist-newstyle", ".hie-bios", ".cabal-sandbox"]
        return super().is_ignored_dirname(dirname) or dirname in haskell_ignore_dirs

    @staticmethod
    def _get_ghc_version():
        """Get the installed GHC version or None if not found."""
        try:
            result = subprocess.run(['ghc', '--version'], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
        except FileNotFoundError:
            return None
        return None

    @staticmethod
    def _get_hls_version():
        """Get the installed haskell-language-server version or None if not found."""
        try:
            result = subprocess.run(['haskell-language-server-wrapper', '--version'], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
        except FileNotFoundError:
            return None
        return None

    @classmethod
    def setup_runtime_dependency(cls):
        """
        Check if required Haskell runtime dependencies are available.
        Raises RuntimeError with helpful message if dependencies are missing.
        """
        # Check for haskell-language-server-wrapper
        if not shutil.which("haskell-language-server-wrapper"):
            ghc_version = cls._get_ghc_version()
            if ghc_version:
                raise RuntimeError(
                    f"Found {ghc_version} but haskell-language-server-wrapper is not installed.\n"
                    "Please install haskell-language-server using one of these methods:\n"
                    "  - ghcup: ghcup install hls\n"
                    "  - Stack: stack install haskell-language-server\n"
                    "  - Download from: https://github.com/haskell/haskell-language-server/releases\n\n"
                    "After installation, make sure haskell-language-server-wrapper is in your PATH."
                )
            else:
                raise RuntimeError(
                    "haskell-language-server-wrapper not found in PATH.\n"
                    "Please install haskell-language-server using one of these methods:\n\n"
                    "Option 1 - ghcup (recommended):\n"
                    "  curl --proto '=https' --tlsv1.2 -sSf https://get-ghcup.haskell.org | sh\n"
                    "  ghcup install ghc\n"
                    "  ghcup install hls\n\n"
                    "Option 2 - Download from GitHub releases:\n"
                    "  https://github.com/haskell/haskell-language-server/releases\n\n"
                    "Make sure haskell-language-server-wrapper is in your PATH after installation."
                )
        
        return True

    def __init__(self, config: MultilspyConfig, logger: MultilspyLogger, repository_root_path: str):
        self.setup_runtime_dependency()
        
        super().__init__(
            config,
            logger,
            repository_root_path,
            ProcessLaunchInfo(cmd="haskell-language-server-wrapper --lsp", cwd=repository_root_path),
            "haskell",
        )
        self.server_ready = threading.Event()
        self.request_id = 0

    def _get_initialize_params(self, repository_absolute_path: str) -> InitializeParams:
        """
        Returns the initialize params for the Haskell Language Server.
        """
        with open(os.path.join(os.path.dirname(__file__), "initialize_params.json"), "r", encoding="utf-8") as f:
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

    def _start_server(self):
        """Start haskell-language-server process"""
        def register_capability_handler(params):
            return

        def window_log_message(msg):
            self.logger.log(f"LSP: window/logMessage: {msg}", logging.INFO)

        def progress_handler(params):
            """Handle $/progress notifications to detect when HLS is ready"""
            self.logger.log(f"LSP: $/progress: {params}", logging.DEBUG)
            
            # Check if this is the end of initial indexing
            if "value" in params and "kind" in params["value"]:
                if params["value"]["kind"] == "end":
                    # HLS might be ready now
                    self.server_ready.set()

        def do_nothing(params):
            return

        self.server.on_request("client/registerCapability", register_capability_handler)
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("$/progress", progress_handler)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)

        self.logger.log("Starting haskell-language-server process", logging.INFO)
        self.server.start()
        initialize_params = self._get_initialize_params(self.repository_root_path)

        self.logger.log(
            "Sending initialize request from LSP client to LSP server and awaiting response",
            logging.INFO,
        )
        init_response = self.server.send.initialize(initialize_params)

        # Verify server capabilities
        assert "textDocumentSync" in init_response["capabilities"]
        assert "definitionProvider" in init_response["capabilities"]

        self.server.notify.initialized({})
        
        # Send workspace/didChangeConfiguration to disable unnecessary plugins
        self.logger.log("Configuring HLS plugins", logging.INFO)
        config_params = {
            "settings": {
                "haskell": {
                    "plugin": {
                        "hlint": {"globalOn": False},
                        "eval": {"globalOn": False},
                        "stan": {"globalOn": False},
                    }
                }
            }
        }
        self.server.notify.workspace_did_change_configuration(config_params)
        
        self.completions_available.set()

        # For simple test repo, HLS should be ready quickly
        # In production, we might need more sophisticated readiness detection
        self.logger.log("Waiting for HLS to be ready...", logging.INFO)
        if not self.server_ready.wait(timeout=60.0):
            self.logger.log("HLS did not signal readiness within 60 seconds, proceeding anyway", logging.WARNING)
        else:
            self.logger.log("HLS is ready", logging.INFO)