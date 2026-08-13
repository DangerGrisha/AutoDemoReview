"""Tests for rendering a coaching report (demoreview/coachreport.py + render.py).

Two things are being protected here.

DEEP LINKS. Making findings cite rounds is only worth it if the round becomes a
click into the 3D replay at the second the mistake happened. The link shape is
a contract between three files -- coachreport builds it, render.py's whitelist
allows it, web/app.py serves it -- so a change in any one of them must fail
loudly rather than degrade into literal text on the page.

INJECTION. The report is rendered as markdown into the match page, and with
`COACH_BACKEND=api` its text comes from a language model. A model that emits a
`javascript:` link, or a player who names themselves `<script>`, must not reach
the DOM. render.py escapes first and then re-linkifies exactly one URL shape;
these tests pin that behaviour down from the outside.
"""

from demoreview import coachreport, render

REPORT = {
    "headline": "Ты умираешь рано и в одной и той же точке.",
    "strengths": [{"point": "18 киллов за матч",
                   "evidence": "второй результат в лобби по киллам (медиана 14)."}],
    "problems": [{
        "title": "Ты умираешь раньше всех в лобби",
        "severity": "high",
        "cause": "Берёшь первый контакт до того, как вышла утилита.",
        "evidence": "Средняя секунда смерти 19.0 с против 40.4 с по лобби.",
        "rounds": [4, 6, 21],
        "fix": "Первые 20 секунд играй вторым номером.",
    }],
    "drill": {"name": "Дисциплина тайминга", "how": "10 раундов без первого контакта.",
              "target": "Средняя секунда смерти выше 35 с."},
}

SEEK = {"4": 18.5, "6": 22.0}          # round -> the second this player died


# --------------------------------------------------------------------------- #
# markdown
# --------------------------------------------------------------------------- #

def test_rounds_become_deep_links_at_the_moment_of_death():
    md = coachreport.render_markdown(REPORT, match_id=27, seek=SEEK)

    assert "[4](/matches/27/3d?round=4&t=18.5)" in md
    assert "[6](/matches/27/3d?round=6&t=22.0)" in md
    # No death time recorded for round 21 -> link to the round, not to second 0.
    assert "[21](/matches/27/3d?round=21)" in md


def test_rounds_stay_plain_text_without_a_match():
    """The CLI report has nothing to link to; it must not emit dead links."""
    md = coachreport.render_markdown(REPORT)

    assert "/matches/" not in md
    assert "4, 6, 21" in md


def test_markdown_carries_every_part_of_the_report():
    md = coachreport.render_markdown(REPORT, match_id=27, seek=SEEK)

    assert REPORT["headline"] in md
    for field in ("title", "cause", "evidence", "fix"):
        assert REPORT["problems"][0][field] in md
    assert REPORT["strengths"][0]["point"] in md
    assert REPORT["drill"]["name"] in md and REPORT["drill"]["target"] in md
    assert coachreport.SEVERITY_LABEL[coachreport.LANGUAGE]["high"] in md
    # Every section heading for the configured language is present, since this
    # fixture exercises every section.
    lower = md.lower()
    for heading in coachreport.HEADINGS[coachreport.LANGUAGE]:
        assert heading.lower() in lower, heading


def test_an_empty_report_renders_without_blowing_up():
    md = coachreport.render_markdown({"headline": "", "strengths": [],
                                      "problems": [], "drill": {}})

    assert isinstance(md, str)


# --------------------------------------------------------------------------- #
# HTML: the round link is the ONLY link shape allowed through
# --------------------------------------------------------------------------- #

def test_the_round_link_survives_into_html():
    html = render.markdown_to_html(
        coachreport.render_markdown(REPORT, match_id=27, seek=SEEK))

    # '&' is escaped before linkification, so the href carries '&amp;'.
    assert '<a class="coach-round" href="/matches/27/3d?round=4&amp;t=18.5">4</a>' in html


def test_foreign_links_are_never_linkified():
    """Whatever URL the text contains, only our own round link becomes an <a>."""
    hostile = (
        "[click](javascript:alert(1))\n\n"
        "[click](https://evil.example/steal)\n\n"
        "[click](//evil.example)\n\n"
        "[click](/matches/27/3d?round=4&t=1.0)\n"      # the one legitimate shape
    )
    html = render.markdown_to_html(hostile)

    assert html.count("<a ") == 1, html
    assert "javascript:" not in html.replace("javascript:alert(1))", "")
    assert "evil.example" in html          # still visible, just as inert text
    assert "<a class=\"coach-round\" href=\"/matches/27/3d?round=4&amp;t=1.0\">" in html


def test_markup_in_the_report_text_is_escaped_not_executed():
    html = render.markdown_to_html(
        "# <script>alert(1)</script>\n\nname: <img src=x onerror=alert(1)>\n")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img" not in html


def test_a_hostile_player_name_inside_a_finding_is_escaped():
    report = dict(REPORT)
    report["problems"] = [dict(REPORT["problems"][0],
                               evidence='Тебя убил <script>alert(1)</script>')]
    html = render.markdown_to_html(coachreport.render_markdown(report, match_id=27))

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_bold_and_code_still_work():
    html = render.markdown_to_html("**жирный** и `код`")

    assert "<strong>жирный</strong>" in html
    assert "<code>код</code>" in html


# --------------------------------------------------------------------------- #
# the card the match page embeds
# --------------------------------------------------------------------------- #

def test_the_card_opens_itself_when_there_is_an_action_but_no_report():
    """A collapsed empty card would hide the only button that fills it."""
    with_action = render.render_coaching(None, action_html="<button>go</button>")
    with_report = render.render_coaching("**готово**")

    assert "<button>go</button>" in with_action
    assert 'aria-expanded="true"' in with_action        # expanded, not collapsed
    assert 'aria-expanded="false"' in with_report       # there is something to unfold
    assert "<strong>готово</strong>" in with_report
