import json
import logging
import os
import pathlib
import threading

from overrides import override

from multilspy.language_servers.haskell.haskell_utils import (
    check_hls_dependency,
    get_hls_version,
    is_haskell_ignored_dirname,
)
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
        self.server_ready = threading.Event()
        
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

    def _start_server(self):
        """Start haskell-language-server process"""
        def register_capability_handler(params):
            return

        def window_log_message(msg):
            self.logger.log(f"LSP: window/logMessage: {msg}", logging.INFO)

        def progress_handler(params):
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
                self.logger.log(f"HLS progress ended: {title or 'unknown'}", logging.DEBUG)
                # Consider the server ready when we get an end progress notification
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

        # Wait for HLS to be ready with proper timeout
        self.logger.log("Waiting for HLS to complete initial initialization...", logging.INFO)
        if self.server_ready.wait(timeout=60.0):
            self.logger.log("HLS server is ready", logging.INFO)
        else:
            self.logger.log("Timeout waiting for HLS to become ready, proceeding anyway", logging.WARNING)
            # Set ready anyway after timeout
            self.server_ready.set()
