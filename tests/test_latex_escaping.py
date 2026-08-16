"""LaTeX escaping regression tests.

``_escape`` did not handle the backslash, so any README containing one
produced a ``.tex`` file that would not compile: the replacements below all
introduce backslashes, and escaping ``\\`` afterwards (or not at all) leaves
stray control sequences behind.
"""

from __future__ import annotations

import pytest

from opendocs.generators.latex_generator import LatexGenerator

escape = LatexGenerator._escape


class TestBackslash:
    def test_lone_backslash_becomes_textbackslash(self):
        assert escape("\\") == r"\textbackslash{}"

    def test_windows_path(self):
        assert escape(r"C:\path_to\file") == r"C:\textbackslash{}path\_to\textbackslash{}file"

    def test_latex_command_is_neutralised(self):
        """A command in the source must not survive as a live command."""
        result = escape(r"\textbf{hi}")
        assert result == r"\textbackslash{}textbf\{hi\}"

    def test_backslash_before_special_char_does_not_double_escape(self):
        """Regression: this used to yield '\\\\&', where '\\\\' is a line break."""
        result = escape(r"50% \& rising")
        assert r"\\" not in result
        assert result == r"50\% \textbackslash{}\& rising"


class TestSpecialCharacters:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("&", r"\&"),
            ("%", r"\%"),
            ("$", r"\$"),
            ("#", r"\#"),
            ("_", r"\_"),
            ("{", r"\{"),
            ("}", r"\}"),
            ("~", r"\textasciitilde{}"),
            ("^", r"\textasciicircum{}"),
        ],
    )
    def test_each_special_char(self, raw, expected):
        assert escape(raw) == expected

    def test_plain_text_is_untouched(self):
        assert escape("plain text, with punctuation!") == "plain text, with punctuation!"

    def test_combined(self):
        assert escape("a_b & c%d") == r"a\_b \& c\%d"


class TestNoUnescapedControlSequences:
    """Whatever the input, the output must not contain a stray command."""

    @pytest.mark.parametrize(
        "raw",
        [
            r"C:\Users\test",
            r"\alpha \beta",
            r"100% \& more_stuff",
            r"\\",
            r"~/.config/opendocs",
            r"a^b_c{d}e",
            "\\textbackslash",
        ],
    )
    def test_only_known_commands_survive(self, raw):
        allowed = {
            r"\&",
            r"\%",
            r"\$",
            r"\#",
            r"\_",
            r"\{",
            r"\}",
            r"\textasciitilde",
            r"\textasciicircum",
            r"\textbackslash",
        }
        result = escape(raw)
        # Walk every backslash and confirm it starts one of the allowed escapes.
        i = 0
        while i < len(result):
            if result[i] == "\\":
                assert any(result.startswith(cmd, i) for cmd in allowed), (
                    f"unescaped control sequence at {i} in {result!r} (input {raw!r})"
                )
                i += max(len(c) for c in allowed if result.startswith(c, i))
            else:
                i += 1
