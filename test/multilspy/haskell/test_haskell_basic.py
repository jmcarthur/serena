import os

import pytest

from multilspy import SyncLanguageServer
from multilspy.multilspy_config import Language
from multilspy.multilspy_utils import SymbolUtils


@pytest.mark.haskell
class TestHaskellLanguageServer:
    """Test Haskell Language Server functionality."""

    @pytest.fixture(scope="class")
    def language_server(self):
        """Create a language server for the Haskell test repository."""
        from test.conftest import create_ls
        from pathlib import Path
        import time

        repo_path = str(Path(__file__).parent.parent.parent / "resources" / "repos" / "haskell" / "test_repo")
        server = create_ls(Language.HASKELL, repo_path)
        server.start()
        
        # Open the main files to trigger HLS analysis (like an IDE would)
        # This is how LSP is meant to work - files are analyzed when opened
        with server.open_file(os.path.join("app", "Main.hs")):
            with server.open_file(os.path.join("src", "Lib.hs")):
                # Give HLS a moment to process the opened files
                time.sleep(5)
        
        try:
            yield server
        finally:
            server.stop()

    def test_find_symbol(self, language_server: SyncLanguageServer) -> None:
        """Test that we can find symbols in the symbol tree."""
        symbols = language_server.request_full_symbol_tree()
        assert SymbolUtils.symbol_tree_contains_name(symbols, "someFunc"), "someFunc not found in symbol tree"
        assert SymbolUtils.symbol_tree_contains_name(symbols, "helperFunc"), "helperFunc not found in symbol tree"
        assert SymbolUtils.symbol_tree_contains_name(symbols, "DemoData"), "DemoData not found in symbol tree"
        assert SymbolUtils.symbol_tree_contains_name(symbols, "processData"), "processData not found in symbol tree"
        assert SymbolUtils.symbol_tree_contains_name(symbols, "main"), "main function not found in symbol tree"

    def test_go_to_definition(self, language_server: SyncLanguageServer) -> None:
        """Test go-to-definition functionality."""
        file_path = os.path.join("app", "Main.hs")

        # Test go-to-definition for someFunc (imported from Lib)
        # Line 8 in Main.hs: someFunc
        definition = language_server.request_definition(file_path, 7, 4)  # 0-indexed, so line 8 is index 7

        assert definition is not None and len(definition) > 0, "Could not find definition for someFunc"
        assert any("Lib.hs" in d.get("relativePath", "") for d in definition), "Definition should be in Lib.hs"

    def test_find_references(self, language_server: SyncLanguageServer) -> None:
        """Test finding references to a symbol."""
        file_path = os.path.join("src", "Lib.hs")
        symbols = language_server.request_document_symbols(file_path)

        # Find the helperFunc symbol
        helper_symbol = None
        for sym in symbols[0]:
            if sym.get("name") == "helperFunc":
                helper_symbol = sym
                break

        assert helper_symbol is not None, "Could not find 'helperFunc' symbol in Lib.hs"

        # Get references to helperFunc
        sel_start = helper_symbol["selectionRange"]["start"]
        refs = language_server.request_references(file_path, sel_start["line"], sel_start["character"])

        # Should find references in both Lib.hs and Main.hs
        assert len(refs) >= 2, "Should find at least 2 references to helperFunc"
        assert any("Lib.hs" in ref.get("relativePath", "") for ref in refs), "Should find reference in Lib.hs"
        assert any("Main.hs" in ref.get("relativePath", "") for ref in refs), "Should find reference in Main.hs"

    def test_hover(self, language_server: SyncLanguageServer) -> None:
        """Test hover functionality for type information."""
        file_path = os.path.join("src", "Lib.hs")

        # Test hover on DemoData type (line 8)
        hover_info = language_server.request_hover(file_path, 8, 5)
        assert hover_info is not None, "Hover should return information for DemoData"
        hover_text = str(hover_info)
        assert "DemoData" in hover_text, "Hover should contain type name"
