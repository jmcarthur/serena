import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from overrides import override

from multilspy.language_server import LanguageServer
from multilspy.lsp_protocol_handler.lsp_types import InitializeParams
from multilspy.lsp_protocol_handler.server import ProcessLaunchInfo
from multilspy.multilspy_config import MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger

from .haskell_utils import check_hls_dependency, get_haskell_initialize_params, get_hls_version, is_haskell_ignored_dirname


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
            ProcessLaunchInfo(cmd="haskell-language-server-wrapper --lsp", cwd=repository_root_path),
            "haskell",
        )
        self.request_id = 0
        self.server_ready = asyncio.Event()

        # Log version information if available
        hls_version = get_hls_version()
        if hls_version:
            logger.log(f"Found HLS: {hls_version}", logging.INFO)

    def _get_initialize_params(self, repository_absolute_path: str) -> InitializeParams:
        """
        Returns the initialize params for the Haskell Language Server.
        """
        params_file_path = os.path.join(os.path.dirname(__file__), "initialize_params.json")
        return get_haskell_initialize_params(repository_absolute_path, params_file_path)

    @asynccontextmanager
    async def start_server(self) -> AsyncIterator["HaskellLanguageServer"]:
        """Start haskell-language-server process"""

        async def register_capability_handler(params):
            return

        async def window_log_message(msg):
            self.logger.log(f"LSP: window/logMessage: {msg}", logging.INFO)

        async def progress_handler(params):
            """Handle $/progress notifications for debugging purposes"""
            value = params.get("value", {})
            kind = value.get("kind", "")
            title = value.get("title", "")
            self.logger.log(f"HLS progress: {kind} - {title}", logging.DEBUG)

            # Set the event when HLS signals it has completed an initialization stage
            if kind == "end":
                self.logger.log(f"HLS progress ended for '{title}', signaling ready.", logging.INFO)
                self.server_ready.set()

        async def do_nothing(params):
            return

        async def check_experimental_status(params):
            """
            Also listen for experimental/serverStatus as a backup signal
            """
            if params.get("quiescent"):
                self.logger.log("Received experimental/serverStatus with quiescent=true", logging.INFO)
                self.server_ready.set()

        self.server.on_request("client/registerCapability", register_capability_handler)
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("$/progress", progress_handler)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)
        self.server.on_notification("experimental/serverStatus", check_experimental_status)

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

            # Wait for HLS to signal readiness through progress notifications or experimental/serverStatus
            # HLS can take several minutes for large projects due to cradle loading and dependency compilation
            self.logger.log("Waiting for HLS to signal readiness (this may take several minutes for large projects)...", logging.INFO)
            try:
                # Use a 60-second timeout to allow HLS sufficient time to start
                # For very large projects, HLS will continue loading in the background
                await asyncio.wait_for(self.server_ready.wait(), timeout=60.0)
                self.logger.log("HLS signaled ready via experimental/serverStatus", logging.INFO)
            except TimeoutError:
                self.logger.log(
                    "HLS hasn't signaled readiness after 60s. Proceeding, but HLS may still be loading. "
                    "Operations may be slow or incomplete until HLS finishes initialization.",
                    logging.WARNING,
                )
                # Don't set server_ready here - let HLS signal when it's actually ready
                # This allows operations to proceed but warns they might fail

            try:
                yield self
            finally:
                pass
