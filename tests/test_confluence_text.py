"""Confluence storage-format XHTML to the plain text an agent reads.

Headings, lists, tables, macros, task lists and references become text an
agent can quote; scripts, styles, comments, images, emoticons, control and
format characters become nothing; a document over the byte cap is refused
before anything is parsed.

Run:  python -m pytest tests/test_confluence_text.py
"""

from __future__ import annotations

import pytest

from agents import confluence_text
from agents.confluence_text import MAX_HTML_BYTES, TooLarge, to_text


def test_headings_paragraphs_and_entities():
    doc = "<h1>Title</h1><p>First &amp; second&nbsp;line</p><h2>Sub</h2><p>Body</p>"
    assert to_text(doc) == "# Title\n\nFirst & second line\n\n## Sub\n\nBody"


def test_lists_bulleted_numbered_and_nested():
    doc = "<ul><li>a</li><li>b<ol><li>one</li><li>two</li></ol></li><li>c</li></ul>"
    assert to_text(doc) == "- a\n- b\n  1. one\n  2. two\n- c"


def test_list_items_wrapping_paragraphs_stay_one_item_per_line():
    doc = "<ul><li><p>x</p></li><li><p>y</p><p>more</p></li></ul><p>after</p>"
    assert to_text(doc) == "- x\n- y\nmore\n\nafter"


def test_tables_become_pipe_rows_with_a_separator_after_the_first_row():
    doc = ("<table><tbody><tr><th>Object</th><th>Type</th></tr>"
           "<tr><td><p>Z_FM</p><p>second</p></td><td>FUNC</td></tr></tbody></table>")
    assert to_text(doc) == "| Object | Type |\n| --- | --- |\n| Z_FM second | FUNC |"


def test_macros_keep_their_name_title_and_body_text():
    doc = ('<ac:structured-macro ac:name="info" ac:schema-version="1">'
           '<ac:parameter ac:name="title">Note</ac:parameter>'
           '<ac:parameter ac:name="icon">true</ac:parameter>'
           '<ac:rich-text-body><p>Read this</p></ac:rich-text-body></ac:structured-macro>')
    assert to_text(doc) == "[macro: info] Note\nRead this"


def test_code_macro_keeps_its_lines():
    doc = ('<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">abap'
           '</ac:parameter><ac:plain-text-body><![CDATA[DATA lv_x TYPE i.\n  lv_x = 1 < 2.]]>'
           '</ac:plain-text-body></ac:structured-macro>')
    assert to_text(doc) == "[macro: code]\nDATA lv_x TYPE i.\n  lv_x = 1 < 2."


def test_task_lists_become_checkboxes():
    doc = ("<ac:task-list>"
           "<ac:task><ac:task-status>complete</ac:task-status>"
           "<ac:task-body>Done thing</ac:task-body></ac:task>"
           "<ac:task><ac:task-status>incomplete</ac:task-status>"
           "<ac:task-body>Open thing</ac:task-body></ac:task>"
           "</ac:task-list>")
    assert to_text(doc) == "- [x] Done thing\n- [ ] Open thing"


def test_links_keep_text_images_and_emoticons_vanish_references_are_bracketed():
    doc = ('<p>See <a href="https://x.example/secret?token=1">the page</a> '
           '<ac:image><ri:attachment ri:filename="diagram.png"/></ac:image>'
           '<ac:emoticon ac:name="tick"/> and '
           '<ac:link><ri:page ri:content-title="TSP - Overview"/></ac:link></p>')
    out = to_text(doc)
    assert out == "See the page and [TSP - Overview]"
    assert "token" not in out and "diagram" not in out


def test_script_style_comments_and_control_characters_are_dropped():
    doc = ("<p>a<!-- hidden -->b</p><script>alert(1)</script><style>p{}</style>"
           "<p>c​d\x07e</p>")
    assert to_text(doc) == "ab\n\ncde"


def test_blank_runs_collapse_and_layout_wrappers_add_nothing():
    doc = ("<ac:layout><ac:layout-section><ac:layout-cell><p>a</p><p></p><p> </p>"
           "<div><br/><br/></div><p>b</p></ac:layout-cell></ac:layout-section></ac:layout>")
    assert to_text(doc) == "a\n\nb"


def test_malformed_markup_still_yields_its_text():
    assert to_text("<p>unclosed <b>bold") == "unclosed bold"
    assert to_text("") == ""


def test_over_the_byte_cap_is_refused_before_parsing(monkeypatch):
    monkeypatch.setattr(confluence_text, "MAX_HTML_BYTES", 64)
    with pytest.raises(TooLarge):
        to_text("<p>" + "x" * 100 + "</p>")
    assert MAX_HTML_BYTES == 4 * 1024 * 1024
