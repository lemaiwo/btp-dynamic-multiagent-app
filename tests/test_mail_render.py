"""Tests for the report-mail renderer (``agents/mail_render.py``).

The report agents already write markdown -- the admin UI renders their run
reports with marked.js. Only the mail path threw that away, escaping the
markdown into one unstyled column. This renderer turns the same markdown into
HTML that Outlook will actually lay out.

Outlook on Windows renders with Word's engine: no flexbox, no grid, no
stylesheets, unreliable padding outside table cells. So the contract these
tests pin is narrow on purpose -- nested tables, inline styles, nothing that
needs a real CSS box model.

No network, no mailbox, no tenant required.

Run:  python tests/test_mail_render.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.mail_render import (  # noqa: E402
    markdown_to_html,
    render_report_html,
    split_subject,
    split_verdict,
)

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}   {detail}")


def main() -> None:
    print("\n== split_subject ==")
    # The workflows send 'SAP SIA/100 daily status -- 2026-09-22'. The date is
    # the agent's, copied from the data; splitting it out gives the header band
    # a subline without asking the model for a second argument.
    title, subline = split_subject("SAP SIA/100 daily status -- 2026-09-22")
    check("splits the title off the double dash", title == "SAP SIA/100 daily status", title)
    check("keeps the tail as the subline", subline == "2026-09-22", subline)

    title, subline = split_subject("SAP SIA/100 job runs — 20260922")
    check("splits on an em dash too", title == "SAP SIA/100 job runs", title)
    check("keeps the em dash tail", subline == "20260922", subline)

    title, subline = split_subject("SAP SIA/100 daily status")
    check("a subject with no separator is all title", title == "SAP SIA/100 daily status", title)
    check("and has no subline", subline == "", subline)

    # 'SIA/100 -- status -- 2026-09-22' should keep the first dash as the split:
    # the date is what trails, and the title may well contain a dash itself.
    title, subline = split_subject("A -- B -- C")
    check("splits once, at the first separator", title == "A", title)
    check("the rest stays together", subline == "B -- C", subline)

    check("an empty subject gives empty parts", split_subject("") == ("", ""))

    print("\n== paragraphs and escaping ==")
    html = markdown_to_html("First line.\nSecond line.")
    check("a paragraph is a <p>", html.count("<p") == 1, html)
    check("a single newline is a line break", "<br>" in html, html)

    html = markdown_to_html("One.\n\nTwo.")
    check("a blank line starts a new paragraph", html.count("<p") == 2, html)

    html = markdown_to_html("<script>alert(1)</script>")
    check("escapes the model's markup", "<script>" not in html, html)
    check("keeps it visible as text", "&lt;script&gt;" in html, html)

    html = markdown_to_html("Jobs A & B")
    check("escapes ampersands", "&amp;" in html, html)

    check("an empty body renders nothing", markdown_to_html("") == "")
    check("whitespace only renders nothing", markdown_to_html("   \n\n  ") == "")

    print("\n== headings ==")
    html = markdown_to_html("## Resources")
    check("a heading is not a paragraph", "<p" not in html, html)
    check("a heading carries its text", "Resources" in html, html)
    check("a heading is inline-styled", "style=" in html, html)
    check("no class attributes survive", "class=" not in html, html)

    html = markdown_to_html("# Big\n\n## Medium\n\n### Small")
    check("three heading levels render three blocks", html.count("<div") >= 3, html)
    check(
        "heading levels differ in size",
        len({s for s in html.split("font-size:")[1:]}) > 1,
        html,
    )

    # A '#' that is not a heading marker -- SAP texts carry plenty of them.
    html = markdown_to_html("Job #4711 failed")
    check("a mid-line hash is not a heading", "<p" in html, html)
    check("the hash survives", "#4711" in html, html)

    print("\n== lists ==")
    html = markdown_to_html("- first\n- second\n- third")
    check("a dash list is a <ul>", "<ul" in html, html)
    check("one <li> per item", html.count("<li") == 3, html)
    check("items keep their text", "second" in html, html)
    check("the marker is not repeated as text", "- first" not in html, html)

    html = markdown_to_html("* star\n+ plus")
    check("stars and pluses are list markers too", html.count("<li") == 2, html)

    html = markdown_to_html("1. one\n2. two")
    check("a numbered list is an <ol>", "<ol" in html, html)
    check("numbers are not repeated as text", ">1. one" not in html, html)

    html = markdown_to_html("- outer\n  - inner\n- outer again")
    check("an indented item nests a list", html.count("<ul") == 2, html)
    check("nesting does not lose items", html.count("<li") == 3, html)

    html = markdown_to_html("- lone item\n\nA paragraph.")
    check("a blank line ends the list", html.count("<ul") == 1, html)
    check("and the paragraph follows", "<p" in html, html)

    # A hyphen that starts a line but is not a bullet.
    html = markdown_to_html("-5 degrees")
    check("a dash with no space is not a bullet", "<ul" not in html, html)

    print("\n== tables ==")
    table = (
        "| Server | CPU % | Swap |\n"
        "| --- | ---: | --- |\n"
        "| sapsia01 | 41 | 62% |\n"
        "| sapsia02 | 96 | 18% |"
    )
    html = markdown_to_html(table)
    check("a pipe table is a <table>", "<table" in html, html)
    check("the delimiter row is not a data row", "---" not in html, html)
    check("header cells are <th>", html.count("<th") == 3, html)
    check("two data rows", html.count("<tr") == 3, html)
    check("six data cells", html.count("<td") == 6, html)
    check("cells keep their values", "sapsia02" in html, html)
    check("the table collapses its borders", "border-collapse:collapse" in html, html)
    check("rows alternate shading", html.count("background-color") >= 2, html)

    html = markdown_to_html("| a | b |\n| --- | --- |")
    check("a header-only table still renders", "<th" in html, html)

    # Ragged rows happen when a model writes a table by hand.
    html = markdown_to_html("| a | b |\n| --- | --- |\n| 1 |")
    check("a short row is padded, not dropped", html.count("<td") == 2, html)

    html = markdown_to_html("| a | b |\n| --- | --- |\n| 1 | 2 | 3 |")
    check("a long row keeps every cell", html.count("<td") == 3, html)

    # A line with pipes that is not a table -- no delimiter row under it.
    html = markdown_to_html("use A | B to pipe")
    check("pipes without a delimiter row stay a paragraph", "<table" not in html, html)

    print("\n== inline marks ==")
    html = markdown_to_html("**bold** text")
    check("double stars are bold", "<strong" in html, html)
    check("the stars are consumed", "**" not in html, html)

    html = markdown_to_html("a `code` span")
    check("backticks are code", "<code" in html, html)

    html = markdown_to_html("`a **b** c`")
    check("code spans are not re-marked inside", "<strong" not in html, html)

    # The reason underscores are left alone: SAP identifiers are full of them.
    html = markdown_to_html("DBIF_RSQL_SQL_ERROR and SAP_COM_0345")
    check("underscores are never emphasis", "<em" not in html, html)
    check("the identifier survives intact", "DBIF_RSQL_SQL_ERROR" in html, html)

    html = markdown_to_html("see *Analyzing Database Errors*")
    check("single stars are italic", "<em" in html, html)
    check("the stars are consumed", "*Analyzing" not in html, html)

    # The reason emphasis needs tight boundaries: SQL and arithmetic are full of
    # loose asterisks, and two on one line must not italicise everything between.
    html = markdown_to_html("SELECT * FROM tbtco WHERE x = 5 * 3")
    check("a spaced asterisk is not emphasis", "<em" not in html, html)
    check("the SELECT survives", "SELECT * FROM tbtco" in html, html)

    html = markdown_to_html("a*b*c identifiers")
    check("an asterisk inside a word is not emphasis", "<em" not in html, html)

    html = markdown_to_html("**bold** not italic")
    check("bold is not read as two italics", "<em" not in html, html)

    html = markdown_to_html("[SAP note 12345](https://me.sap.com/notes/12345)")
    check("a markdown link becomes an anchor", "<a href=" in html, html)
    check("the anchor carries the target", "me.sap.com/notes/12345" in html, html)
    check("the label is the text", ">SAP note 12345<" in html, html)

    html = markdown_to_html("[click](javascript:alert(1))")
    check("a javascript: target is refused", "javascript:" not in html, html)
    check("but the label is kept", "click" in html, html)

    html = markdown_to_html('[q](https://x.test/a"onmouseover="alert(1))')
    check("a quote cannot break out of the href", 'href="https://x.test/a"o' not in html, html)

    print("\n== rules and quotes ==")
    html = markdown_to_html("above\n\n---\n\nbelow")
    # The rule itself is drawn with a one-cell layout table -- Word ignores
    # border-top on a bare div. What must not happen is the '---' being read as
    # a table delimiter and swallowing the line above it as a header.
    check("a --- line is a rule, not a data table", "<th" not in html, html)
    check("the rule renders as a border", "border-top" in html, html)

    html = markdown_to_html("> quoted finding")
    check("a blockquote is set off by a left border", "border-left" in html, html)
    check("the marker is not repeated as text", "&gt; quoted" not in html, html)

    print("\n== code fences ==")
    html = markdown_to_html("```\nSELECT * FROM tbtco\n```")
    check("a fence becomes <pre>", "<pre" in html, html)
    check("the code survives", "SELECT * FROM tbtco" in html, html)
    check("the fence markers are consumed", "```" not in html, html)

    html = markdown_to_html("```sql\nSELECT 1\n```")
    check("a language tag is not printed", ">sql" not in html, html)

    html = markdown_to_html("```\n<b>x</b>\n```")
    check("code is escaped too", "<b>" not in html, html)

    html = markdown_to_html("```\nunclosed fence")
    check("an unclosed fence still renders its content", "unclosed fence" in html, html)

    print("\n== the verdict, split off the body ==")
    verdict, rest = split_verdict("Needs attention.\n\n## Totals\n\n- 4 failed")
    check("the opening paragraph is the verdict", verdict == "Needs attention.", verdict)
    check("the rest keeps the sections", rest.startswith("## Totals"), rest)

    verdict, rest = split_verdict("Line one.\nLine two.\n\nBody.")
    check("a two-line verdict stays whole", verdict == "Line one.\nLine two.", verdict)

    verdict, rest = split_verdict("## Totals\n\n- 4 failed")
    check("a body opening on a heading has no verdict", verdict == "", verdict)
    check("and nothing is taken from it", rest.startswith("## Totals"), rest)

    verdict, rest = split_verdict("| a |\n| --- |\n| 1 |")
    check("a body opening on a table has no verdict", verdict == "", verdict)

    verdict, rest = split_verdict("Just the one paragraph.")
    check("a single paragraph is the verdict", verdict == "Just the one paragraph.", verdict)
    check("and leaves an empty body", rest == "", rest)

    check("an empty body splits into nothing", split_verdict("") == ("", ""))

    print("\n== the report shell ==")
    doc = render_report_html(
        "All clear.\n\n## Totals\n\n- 12 jobs",
        title="SAP SIA/100 job runs",
        subline="20260922",
        status="ok",
        footer="Automated report",
    )
    check("the title is in the document", "SAP SIA/100 job runs" in doc, doc[:200])
    check("so is the subline", "20260922" in doc, doc[:400])
    check("the body markdown is rendered", "<li" in doc, doc)
    check("the verdict is carried over", "All clear." in doc, doc)
    check("the verdict appears once", doc.count("All clear.") == 1, doc)
    check("the footer is in the document", "Automated report" in doc, doc)
    check("the sheet is a fixed width", 'width="640"' in doc, doc)

    check("no stylesheet block", "<style" not in doc, doc)
    check("no class attributes", "class=" not in doc, doc)
    check("no remote images", "<img" not in doc, doc)
    check("the header band has a background", "background-color:#1f3348" in doc, doc)

    print("\n== the status accent ==")
    ok = render_report_html("Fine.", title="T", status="ok")
    attention = render_report_html("Not fine.", title="T", status="attention")
    unknown = render_report_html("No verdict given.", title="T", status="")

    check("an ok report is green", "#1e7b4d" in ok, ok)
    check("an attention report is amber", "#b9770e" in attention, attention)
    check("no status is neutral, not green", "#1e7b4d" not in unknown, unknown)
    check("and not amber either", "#b9770e" not in unknown, unknown)
    check("an unknown status word is treated as no status",
          "#1e7b4d" not in render_report_html("x", title="T", status="excellent"))
    check("status is case-insensitive",
          "#b9770e" in render_report_html("x", title="T", status="ATTENTION"))

    print("\n== the shell holds up on odd input ==")
    doc = render_report_html("", title="Still a report", status="ok")
    check("an empty body still renders the title", "Still a report" in doc, doc)
    check("an empty body renders no callout", "#1e7b4d" not in doc, doc)

    doc = render_report_html("Body.", title="<script>x</script>", subline="<b>d</b>")
    check("the title is escaped", "<script>" not in doc, doc)
    check("the subline is escaped", "<b>d</b>" not in doc, doc)

    doc = render_report_html("Body.", title="T", footer="")
    check("no footer text means no footer rule", "border-top:1px solid #e4e9ef" not in doc, doc)

    doc = render_report_html("## Section\n\nText.", title="T", status="attention")
    check("a body with no verdict paragraph shows no callout",
          "#b9770e" not in doc, doc)
    check("but the section survives", "Section" in doc, doc)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    if FAILED:
        sys.exit(1)



# --- pytest-collected: the mail theme ----------------------------------------
# A theme is optional per-server config. Without one, the output must stay
# byte-identical to what the renderer produced before themes existed: every
# report already in someone's inbox is the reference.

import pytest  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "mail_render_default.html"
SAMPLE_BODY = """Needs attention: one job failed.
Second verdict line.

## Resources

| Server | CPU | Note |
| --- | ---: | :---: |
| s01 | 96 | `hot` |
| s02 | 12 | [link](https://example.com/x) |

### Detail

- one **bold**
  - nested *em*
1. first

> quoted

```
code <x>
```

---

Plain paragraph with `code`."""


def _sample(theme=None) -> str:
    kwargs = {"theme": theme} if theme is not None else {}
    return render_report_html(SAMPLE_BODY, title="Daily check <x>", subline="2026-09-29",
                              status="attention", footer="Footer text & more", **kwargs)


def test_default_output_is_byte_identical_to_before_themes():
    assert _sample() == FIXTURE.read_text(encoding="utf-8")


def test_an_empty_theme_is_the_default():
    from agents.mail_render import MailTheme

    assert _sample(MailTheme()) == FIXTURE.read_text(encoding="utf-8")
    assert MailTheme.from_config(None) == MailTheme()
    assert MailTheme.from_config({}) == MailTheme()


def test_themed_colours_reach_band_link_heading_tables_and_shell():
    from agents.mail_render import MailTheme

    theme = MailTheme.from_config({
        "band": "#102030", "band_text": "#fafafa", "band_sub": "#a0b0c0",
        "accent": "#e0a010", "link": "#123abc", "heading": "#334455",
        "head_cell": "#eeeeee", "zebra": "#f0f0f0", "shell": "#dddddd",
    })
    doc = _sample(theme)
    assert "background-color:#102030" in doc
    assert "color:#fafafa" in doc
    assert "color:#a0b0c0" in doc
    assert "#e0a010" in doc, "the accent rule appears"
    assert "color:#123abc" in doc, "links use the themed colour"
    assert "color:#334455" in doc, "headings use the themed colour"
    assert "background-color:#eeeeee" in doc
    assert "background-color:#f0f0f0" in doc
    assert "background-color:#dddddd" in doc
    for old in ("#1f3348", "#1d6fa5", "#eef2f6", "#eef1f5"):
        assert old not in doc, f"default colour {old} leaked into a themed mail"


def test_status_tints_stay_semantic():
    from agents.mail_render import MailTheme

    theme = MailTheme.from_config({"band": "#102030", "accent": "#e0a010"})
    assert "#b9770e" in _sample(theme)
    ok = render_report_html("Fine.", title="T", status="ok", theme=theme)
    assert "#1e7b4d" in ok


def test_accent_is_absent_by_default():
    from agents.mail_render import MailTheme

    doc = _sample(MailTheme(band="#102030"))
    assert "height:3px" not in doc


def test_themed_font_is_used():
    from agents.mail_render import MailTheme

    doc = _sample(MailTheme.from_config({"font": "Georgia, 'Times New Roman', serif"}))
    assert "font-family:Georgia, 'Times New Roman', serif" in doc
    assert "Segoe UI" not in doc


def test_logo_is_an_outlook_safe_img_with_escaping():
    from agents.mail_render import MailTheme

    theme = MailTheme.from_config({"logo_url": "https://example.com/logo.png?a=1&b=2",
                                   "org_name": "Example & Co"})
    doc = render_report_html("Body.", title="T", theme=theme)
    assert '<img src="https://example.com/logo.png?a=1&amp;b=2"' in doc
    assert 'alt="Example &amp; Co"' in doc
    assert 'height="28"' in doc
    assert 'border="0"' in doc
    assert "width:auto" in doc


def test_org_name_shows_in_the_band_without_a_logo_and_is_escaped():
    from agents.mail_render import MailTheme

    doc = render_report_html("Body.", title="T",
                             theme=MailTheme.from_config({"org_name": "<b>Example</b>"}))
    assert "&lt;b&gt;Example&lt;/b&gt;" in doc
    assert "<b>Example</b>" not in doc
    assert "<img" not in doc


def test_theme_footer_overrides_the_default_footer_and_is_escaped():
    from agents.mail_render import MailTheme

    doc = render_report_html("Body.", title="T", footer="Default footer",
                             theme=MailTheme.from_config({"footer": "Sent by <ops>"}))
    assert "Sent by &lt;ops&gt;" in doc
    assert "Default footer" not in doc


@pytest.mark.parametrize("cfg,message", [
    ({"band": "red"}, "band"),
    ({"link": "#12345"}, "link"),
    ({"accent": "#12345g"}, "accent"),
    ({"zebra": "#ffffff\n"}, "zebra"),
    ({"band": 123}, "band"),
    ({"logo_url": "http://example.com/logo.png"}, "https"),
    ({"logo_url": "https://example.com/a b.png"}, "logo_url"),
    ({"logo_url": 'https://example.com/"onerror=x'}, "logo_url"),
    ({"logo_url": "https://example.com/<x>"}, "logo_url"),
    ({"logo_url": "https://example.com/javascript:alert(1)"}, "logo_url"),
    ({"font": "Arial; background:url(x)"}, "font"),
    ({"org_name": "x" * 81}, "org_name"),
    ({"footer": "x" * 301}, "footer"),
    ({"banner": "#ffffff"}, "unknown"),
    ("not a dict", "object"),
])
def test_from_config_rejects_bad_values(cfg, message):
    from agents.mail_render import MailTheme

    with pytest.raises(ValueError, match=message):
        MailTheme.from_config(cfg)


def test_to_config_round_trips_only_what_was_set():
    from agents.mail_render import MailTheme

    cfg = {"band": "#102030", "logo_url": "https://example.com/l.png", "org_name": "Example"}
    theme = MailTheme.from_config(cfg)
    assert theme.to_config() == cfg
    assert MailTheme.from_config(theme.to_config()) == theme
    assert MailTheme().to_config() == {}


if __name__ == "__main__":
    main()
