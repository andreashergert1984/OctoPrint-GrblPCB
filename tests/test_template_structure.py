"""Guard: plugin templates must have balanced tags.

OctoPrint wraps every template in Knockout comments; one stray closing tag
makes the browser move the closing comment out of its container and breaks
view model binding for the whole page.
"""
import pathlib
import re

import pytest

TEMPLATES = sorted((pathlib.Path(__file__).parent.parent / "octoprint_grblpcb" / "templates").glob("*.jinja2"))
VOID = {"input", "br", "hr", "img", "meta", "link", "col", "source", "area", "base", "wbr"}
TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*?(/?)>")


def _unbalanced(text):
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    stack, problems = [], []
    for m in TAG.finditer(text):
        closing, tag, self_closing = m.group(1), m.group(2).lower(), m.group(3)
        line = text.count("\n", 0, m.start()) + 1
        if tag in VOID or self_closing:
            continue
        if not closing:
            stack.append((tag, line))
        elif not stack:
            problems.append(f"stray </{tag}> at line {line}")
        elif stack[-1][0] != tag:
            problems.append(f"</{tag}> at line {line} closes <{stack[-1][0]}> from line {stack[-1][1]}")
            stack.pop()
        else:
            stack.pop()
    problems += [f"<{tag}> from line {line} never closed" for tag, line in stack]
    return problems


def _knockout_balanced(text):
    opened = len(re.findall(r"<!--\s*ko\s", text))
    closed = len(re.findall(r"<!--\s*/ko\s*-->", text))
    return opened == closed


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_template_tags_are_balanced(template):
    assert _unbalanced(template.read_text()) == []


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_template_knockout_comments_are_balanced(template):
    assert _knockout_balanced(template.read_text())


# Quoted literals in text bindings that are not words to translate
_UNITS = {"mm", "µm", "RPM", "mm/min", "mm/m"}
_TEXT_BINDING = re.compile(r"text:\s*((?:[^,'\"]|'[^']*'|\"[^\"]*\")+)")


def _untranslatable_text_literals(text):
    found = []
    for binding in re.findall(r'data-bind="([^"]*)"', text):
        for expression in _TEXT_BINDING.findall(binding):
            for literal in re.findall(r"'([^']*)'", expression):
                if re.search(r"[A-Za-z]{2,}", literal) and literal.strip() not in _UNITS:
                    found.append(literal)
    return found


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_text_bindings_have_no_fixed_words(template):
    # Words in text bindings bypass gettext; build them in the view model
    assert _untranslatable_text_literals(template.read_text()) == []


# Fixed words between tags bypass gettext as well. Allowed: units and
# machine/G-code tokens that are the same in every language.
_ALLOWED_TOKENS = {"mm", "RPM", "mm/min", "mm/m", "mm/s", "2D", "3D", "X", "Y", "Z", "F", "S",
                   "X+", "X-", "Y+", "Y-", "Z+", "Z-", "0X", "0Y", "0Z", "RST", "B", "H", "°", "µm"}


def _fixed_text_nodes(text):
    text = re.sub(r"\{%\s*trans\b.*?\{%\s*endtrans\s*%\}", " ", text, flags=re.S)
    text = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", " ", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    # Replace whole tags (attributes may contain '>') so only text nodes remain
    text = re.sub(r"<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", "<>", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    found = []
    for node in re.findall(r">([^<>]+)<", text):
        for token in node.split():
            token = token.strip("().,:;×")
            if re.search(r"[A-Za-z]{2,}", token) and token not in _ALLOWED_TOKENS:
                found.append(token)
    return found


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_fixed_words_between_tags(template):
    assert _fixed_text_nodes(template.read_text()) == []
