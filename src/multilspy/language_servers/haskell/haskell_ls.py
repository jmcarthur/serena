import asyncio
import json
import logging
import os
import pathlib
import re
import shutil
from contextlib import asynccontextmanager
from typing import AsyncIterator

from overrides import override

from multilspy import multilspy_types
from multilspy.lsp_protocol_handler import lsp_types
from multilspy.multilspy_exceptions import MultilspyException
from multilspy.multilspy_logger import MultilspyLogger
from multilspy.language_server import LanguageServer
from multilspy.lsp_protocol_handler.server import Error, ProcessLaunchInfo
from multilspy.lsp_protocol_handler.lsp_types import InitializeParams
from multilspy.multilspy_config import MultilspyConfig


class HaskellLanguageServer(LanguageServer):
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
            import subprocess
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
            import subprocess
            result = subprocess.run(['haskell-language-server-wrapper', '--version'], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
        except FileNotFoundError:
            return None
        return None

    @classmethod
    def setup_runtime_dependencies(cls):
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
        self.setup_runtime_dependencies()
        
        super().__init__(
            config,
            logger,
            repository_root_path,
            ProcessLaunchInfo(
                cmd="haskell-language-server-wrapper",
                args=["--lsp"],
                cwd=repository_root_path
            ),
            "haskell",
        )
        self.server_ready = asyncio.Event()
        self.request_id = 0
        self._cradle_load_task = None

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

    async def _monitor_stderr_for_cradle(self):
        """
        Monitor stderr for cradle loading status.
        This is a fallback mechanism if progress notifications don't work.
        """
        try:
            # Patterns that indicate successful cradle loading
            success_patterns = [
                re.compile(r"Cradle.*loaded", re.IGNORECASE),
                re.compile(r"Cradle.*ready", re.IGNORECASE),
                re.compile(r"Cradle.*succeeded", re.IGNORECASE),
                re.compile(r"Loading.*complete", re.IGNORECASE),
            ]
            
            # Patterns that indicate failure
            failure_patterns = [
                re.compile(r"No cradle", re.IGNORECASE),
                re.compile(r"Cradle failed", re.IGNORECASE),
                re.compile(r"Error loading", re.IGNORECASE),
                re.compile(r"Failed to load", re.IGNORECASE),
            ]
            
            # Read stderr line by line
            async for line in self.server.stderr:
                line_str = line.decode('utf-8') if isinstance(line, bytes) else line
                self.logger.log(f"HLS stderr: {line_str.strip()}", logging.DEBUG)
                
                # Check for success patterns
                for pattern in success_patterns:
                    if pattern.search(line_str):
                        self.logger.log("HLS cradle loaded successfully", logging.INFO)
                        self.server_ready.set()
                        return
                
                # Check for failure patterns
                for pattern in failure_patterns:
                    if pattern.search(line_str):
                        self.logger.log(f"HLS cradle loading failed: {line_str.strip()}", logging.ERROR)
                        raise RuntimeError(f"HLS cradle loading failed: {line_str.strip()}")
                        
        except asyncio.CancelledError:
            self.logger.log("Stderr monitoring cancelled", logging.DEBUG)
            raise
        except Exception as e:
            self.logger.log(f"Error monitoring stderr: {e}", logging.ERROR)
            raise

    @asynccontextmanager
    async def start_server(self) -> AsyncIterator["HaskellLanguageServer"]:
        """Start haskell-language-server process"""
        
        async def register_capability_handler(params):
            return

        async def window_log_message(msg):
            self.logger.log(f"LSP: window/logMessage: {msg}", logging.INFO)

        async def progress_handler(params):
            """Handle $/progress notifications to detect when HLS is ready"""
            self.logger.log(f"LSP: $/progress: {params}", logging.INFO)
            
            # Track progress tokens to understand what HLS is doing
            token = params.get("token", "")
            value = params.get("value", {})
            kind = value.get("kind", "")
            title = value.get("title", "")
            message = value.get("message", "")
            
            self.logger.log(f"HLS Progress: token={token}, kind={kind}, title={title}, message={message}", logging.INFO)
            
            # Look for completion signals
            # HLS sends progress notifications during startup
            if kind == "end":
                # Any "end" progress means HLS has finished some initialization stage
                if not self.server_ready.is_set():
                    self.logger.log(f"HLS progress ended: {title or 'unknown'}", logging.INFO)
                    self.server_ready.set()

        async def do_nothing(params):
            return

        self.server.on_request("client/registerCapability", register_capability_handler)
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("$/progress", progress_handler)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)

        async with super().start_server():
            self.logger.log("Starting haskell-language-server process", logging.INFO)
            await self.server.start()
            initialize_params = self._get_initialize_params(self.repository_root_path)

            self.logger.log(
                "Sending initialize request from LSP client to LSP server and awaiting response",
                logging.INFO,
            )
            init_response = await self.server.send.initialize(initialize_params)
            
            # Verify basic server capabilities
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

            # Start monitoring stderr for cradle loading (fallback mechanism)
            self._cradle_load_task = asyncio.create_task(self._monitor_stderr_for_cradle())
            
            # Wait for server to signal some progress completion
            self.logger.log("Waiting for HLS to initialize...", logging.INFO)
            try:
                await asyncio.wait_for(self.server_ready.wait(), timeout=60.0)
                # HLS signals progress completion early, but needs more time for cross-module analysis
                # Add a delay to ensure it's fully ready
                self.logger.log("HLS signaled progress completion, waiting for full initialization...", logging.INFO)
                await asyncio.sleep(10.0)
                self.logger.log("HLS should be ready now", logging.INFO)
            except asyncio.TimeoutError:
                self.logger.log("HLS did not signal any progress within 60 seconds, proceeding anyway", logging.WARNING)
            finally:
                # Cancel the stderr monitoring task
                if self._cradle_load_task and not self._cradle_load_task.done():
                    self._cradle_load_task.cancel()
                    try:
                        await self._cradle_load_task
                    except asyncio.CancelledError:
                        pass

            yield self