import asyncio
import json
import logging
import os
import pathlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from overrides import override

from multilspy.language_server import LanguageServer
from multilspy.lsp_protocol_handler.lsp_types import InitializeParams
from multilspy.lsp_protocol_handler.server import ProcessLaunchInfo
from multilspy.multilspy_config import MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger

from .haskell_utils import check_hls_dependency, get_hls_version, is_haskell_ignored_dirname


class HaskellLanguageServer(LanguageServer):
    """
    Provides Haskell specific instantiation of the LanguageServer class using haskell-language-server.
    """
    
    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        return super().is_ignored_dirname(dirname) or is_haskell_ignored_dirname(dirname)


    @classmethod
    def setup_runtime_dependencies(cls):
        """
        Check if required Haskell runtime dependencies are available.
        Raises RuntimeError with helpful message if dependencies are missing.
        """
        check_hls_dependency()
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
        self.request_id = 0
        self._cradle_load_task = None
        
        # Log version information if available
        hls_version = get_hls_version()
        if hls_version:
            logger.log(f"Found HLS: {hls_version}", logging.INFO)

    def _get_initialize_params(self, repository_absolute_path: str) -> InitializeParams:
        """
        Returns the initialize params for the Haskell Language Server.
        """
        with open(os.path.join(os.path.dirname(__file__), "initialize_params.json"), encoding="utf-8") as f:
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
                        # Just monitoring for debug purposes
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
            """Handle $/progress notifications for debugging purposes"""
            # Just log progress for visibility
            value = params.get("value", {})
            kind = value.get("kind", "")
            title = value.get("title", "")
            self.logger.log(f"HLS progress: {kind} - {title}", logging.DEBUG)

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
            
            # Wait for HLS to signal readiness through progress notifications or experimental/serverStatus
            # HLS can take several minutes for large projects due to cradle loading and dependency compilation
            self.logger.log("Waiting for HLS to signal readiness (this may take several minutes for large projects)...", logging.INFO)
            try:
                # Use a 30-second timeout as a balance between responsiveness and allowing HLS time to start
                # For very large projects, HLS will continue loading in the background
                await asyncio.wait_for(self.server_ready.wait(), timeout=30.0)
                self.logger.log("HLS signaled ready via experimental/serverStatus", logging.INFO)
            except TimeoutError:
                self.logger.log(
                    "HLS hasn't signaled readiness after 30s. Proceeding, but HLS may still be loading. "
                    "Operations may be slow or incomplete until HLS finishes initialization.",
                    logging.WARNING
                )
                # Don't set server_ready here - let HLS signal when it's actually ready
                # This allows operations to proceed but warns they might fail
                self.completions_available.set()  # Basic completion might work

            try:
                yield self
            finally:
                # Cancel the stderr monitoring task
                if self._cradle_load_task and not self._cradle_load_task.done():
                    self._cradle_load_task.cancel()
                    try:
                        await self._cradle_load_task
                    except asyncio.CancelledError:
                        pass
