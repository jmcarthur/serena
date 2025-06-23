import os

import pytest

from multilspy import SyncLanguageServer
from multilspy.multilspy_config import Language
from multilspy.multilspy_utils import SymbolUtils
from serena.text_utils import LineType


@pytest.mark.haskell
class TestHaskellLanguageServer:
    """Test Haskell Language Server functionality."""

    @pytest.fixture(scope="class")
    def language_server(self):
        """Create a language server for the Haskell test repository."""
        from pathlib import Path

        from test.conftest import create_ls

        repo_path = str(Path(__file__).parent.parent.parent / "resources" / "repos" / "haskell" / "test_repo")
        server = create_ls(Language.HASKELL, repo_path)
        server.start()
        
        # Open the main files to trigger HLS analysis (like an IDE would)
        # This is how LSP is meant to work - files are analyzed when opened
        # IMPORTANT: Keep files open during entire test session for HLS
        main_buffer = server.open_file(os.path.join("app", "Main.hs"))
        lib_buffer = server.open_file(os.path.join("src", "Lib.hs"))
        
        # Enter the contexts to actually open the files
        main_buffer.__enter__()
        lib_buffer.__enter__()
        
        try:
            yield server
        finally:
            # Clean up: exit the file contexts
            lib_buffer.__exit__(None, None, None)
            main_buffer.__exit__(None, None, None)
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

    def test_retrieve_content_around_line(self, language_server: SyncLanguageServer) -> None:
        """Test retrieve_content_around_line functionality with various scenarios."""
        file_path = os.path.join("src", "Lib.hs")

        # Scenario 1: Just a single line (line 14 has someFunc definition - 0-indexed)
        line_14 = language_server.retrieve_content_around_line(file_path, 14)
        assert len(line_14.lines) == 1
        assert "someFunc ::" in line_14.lines[0].line_content
        assert line_14.lines[0].line_number == 14
        assert line_14.lines[0].match_type == LineType.MATCH

        # Scenario 2: Context above and below 
        with_context = language_server.retrieve_content_around_line(file_path, 8, 2, 2)
        assert len(with_context.lines) == 5
        # Check that DemoData type definition is in the matched line
        assert "data DemoData" in with_context.matched_lines[0].line_content
        assert with_context.num_matched_lines == 1
        # Check match types
        assert with_context.lines[0].match_type == LineType.BEFORE_MATCH
        assert with_context.lines[1].match_type == LineType.BEFORE_MATCH
        assert with_context.lines[2].match_type == LineType.MATCH
        assert with_context.lines[3].match_type == LineType.AFTER_MATCH
        assert with_context.lines[4].match_type == LineType.AFTER_MATCH

        # Scenario 3: Edge case - context at start of file
        first_line_with_context = language_server.retrieve_content_around_line(file_path, 0, 3, 3)
        assert first_line_with_context.lines[0].line_number == 0
        # Check match type for the target line
        for line in first_line_with_context.lines:
            if line.line_number == 0:
                assert line.match_type == LineType.MATCH
            elif line.line_number < 0:
                assert line.match_type == LineType.BEFORE_MATCH
            else:
                assert line.match_type == LineType.AFTER_MATCH

    def test_search_files_for_pattern(self, language_server: SyncLanguageServer) -> None:
        """Test search_files_for_pattern with various patterns and glob filters."""
        # Test 1: Search for function type signatures
        func_pattern = r"::\s*[^=]+"
        matches = language_server.search_files_for_pattern(func_pattern)
        assert len(matches) > 0
        # Should find multiple function signatures
        assert len(matches) >= 3

        # Test 2: Search for data type definitions with include glob
        data_pattern = r"data\s+\w+"
        matches = language_server.search_files_for_pattern(data_pattern, paths_include_glob="**/Lib.hs")
        assert len(matches) >= 1  # Should find DemoData in Lib.hs
        assert matches[0].source_file_path is not None
        assert "Lib.hs" in matches[0].source_file_path

        # Test 3: Search for function definitions with exclude glob  
        func_def_pattern = r"\w+\s+::"
        matches = language_server.search_files_for_pattern(func_def_pattern, paths_exclude_glob="**/Main.hs")
        assert len(matches) > 0
        # Should find functions in Lib.hs but not in Main.hs
        assert all(match.source_file_path is not None and "Main.hs" not in match.source_file_path for match in matches)

        # Test 4: Search for specific function 
        main_pattern = r"main\s*::"
        matches = language_server.search_files_for_pattern(main_pattern)
        assert len(matches) == 1  # Should only find main in Main.hs
        assert matches[0].source_file_path is not None
        assert "Main.hs" in matches[0].source_file_path

        # Test 5: Search for imports
        import_pattern = r"import\s+\w+"
        matches = language_server.search_files_for_pattern(import_pattern)
        assert len(matches) >= 1  # Should find imports in Main.hs
        assert any(match.source_file_path is not None and "Main.hs" in match.source_file_path for match in matches)
