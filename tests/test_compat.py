"""Unit tests for version detection and the per-version behavior profiles."""

import pytest

from forgejo_projects_mcp.compat import (
    CSRF_ORIGIN,
    CSRF_TOKEN,
    DEFAULT_PROFILE,
    QUIRKS,
    Profile,
    Quirk,
    Version,
    detect_csrf_token,
    detect_version,
    profile_for,
)

# A realistic slice of the head of any authenticated Forgejo page.
PAGE_HEAD = """
<script>
    window.config = {
        appUrl: 'http:\\/\\/localhost:3316\\/',
        assetVersionEncoded: encodeURIComponent('16.0.3~gitea-1.22.0'),
        csrfToken: 'FGk8RRgcoIC1ZRZ1QNFvabc',
    };
</script>
"""

PAGE_FOOTER_ONLY = """
<footer><div class="left-links">Version:
    <a href="/admin/config">8.0.3</a>
</div></footer>
"""


# --------------------------------------------------------------------- Version
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("16.0.3", (16, 0, 3)),
        ("16.0.3+gitea-1.22.0", (16, 0, 3)),  # REST API shape
        ("16.0.3~gitea-1.22.0", (16, 0, 3)),  # HTML asset-version shape
        ("7.0.16+gitea-1.21.11", (7, 0, 16)),
        ("1.21.11-2", (1, 21, 11)),  # legacy pre-7 numbering
        ("16.0", (16, 0, 0)),
    ],
)
def test_version_parse_accepts_every_published_shape(raw, expected):
    version = Version.parse(raw)
    assert (version.major, version.minor, version.patch) == expected
    assert version.raw == raw


@pytest.mark.parametrize("raw", [None, "", "unknown", "gitea"])
def test_version_parse_rejects_non_versions(raw):
    assert Version.parse(raw) is None


def test_versions_compare_numerically_and_ignore_build_metadata():
    assert Version.parse("9.0.3") < Version.parse("10.0.0")
    assert Version.parse("13.0.5") < Version.parse("14.0.0")
    assert Version.parse("16.0.3~gitea-1.22.0") == Version.parse("16.0.3+gitea-1.22.0")


def test_version_short_drops_build_metadata():
    assert Version.parse("16.0.3+gitea-1.22.0").short == "16.0.3"


# -------------------------------------------------------------------- detection
def test_detect_version_from_window_config():
    assert detect_version(PAGE_HEAD) == Version(16, 0, 3)


def test_detect_version_from_the_asset_url_fallback():
    html = '<script src="/assets/js/index.js?v=12.0.4~gitea-1.22.0"></script>'
    assert detect_version(html) == Version(12, 0, 4)


def test_detect_version_from_the_footer_fallback():
    assert detect_version(PAGE_FOOTER_ONLY) == Version(8, 0, 3)


def test_detect_version_returns_none_without_a_marker():
    assert detect_version("") is None
    assert detect_version("<html><body>nothing here</body></html>") is None


def test_detect_csrf_token_from_window_config_and_form_field():
    assert detect_csrf_token(PAGE_HEAD) == "FGk8RRgcoIC1ZRZ1QNFvabc"
    assert detect_csrf_token('<input name="_csrf" value="abc123">') == "abc123"
    assert detect_csrf_token("<p>no token</p>") is None


# --------------------------------------------------------------------- profiles
def test_unknown_version_uses_the_newest_verified_behavior():
    profile = profile_for(None)
    assert profile.csrf_mode == CSRF_ORIGIN
    assert profile.quirks == ()
    assert profile.describe()["verified"] is False


@pytest.mark.parametrize("version", ["14.0.0", "15.0.7", "16.0.3"])
def test_origin_csrf_from_forgejo_14(version):
    assert profile_for(Version.parse(version)).csrf_mode == CSRF_ORIGIN


@pytest.mark.parametrize("version", ["7.0.16", "9.0.3", "13.0.5"])
def test_csrf_token_required_below_forgejo_14(version):
    profile = profile_for(Version.parse(version))
    assert profile.csrf_mode == CSRF_TOKEN
    assert "csrf-token-required" in profile.quirks


def test_board_title_quirk_narrows_candidates_below_forgejo_10():
    """Below 10.0 the page <title> names the repository, not the board."""
    old = profile_for(Version(9, 0, 3))
    new = profile_for(Version(10, 0, 0))

    assert "board-title-missing-from-page-title" in old.quirks
    assert "board-title-missing-from-page-title" not in new.quirks
    # Only the project headings survive below 10.0; the <title> fallbacks would
    # report the repository there.
    assert all("<h2" in p for p in old.patterns["board_title"])
    assert any("<title>" in p for p in new.patterns["board_title"])


def test_describe_reports_the_resolved_behavior():
    described = profile_for(Version.parse("13.0.5")).describe()
    assert described == {
        "version": "13.0.5",
        "version_short": "13.0.5",
        "csrf_mode": CSRF_TOKEN,
        "quirks": ["csrf-token-required"],
        "verified": True,
    }


def test_a_version_outside_the_verified_window_is_flagged():
    assert profile_for(Version(99, 0, 0)).describe()["verified"] is False
    assert profile_for(Version(1, 19, 0)).describe()["verified"] is False
    # Forgejo numbering jumps from the 1.x line straight to 7.0.
    assert profile_for(Version(1, 20, 6)).describe()["verified"] is True
    assert profile_for(Version(7, 0, 16)).describe()["verified"] is True


def test_legacy_board_vocabulary_quirk_below_forgejo_1_21():
    """Before 1.21, project columns were called boards in the markup."""
    legacy = profile_for(Version(1, 20, 6))
    modern = profile_for(Version(1, 21, 11))

    assert "legacy-board-vocabulary" in legacy.quirks
    assert "legacy-board-vocabulary" not in modern.quirks
    assert all("board-column" in p for p in legacy.patterns["board_column_open"])
    assert all("project-column" in p for p in modern.patterns["board_column_open"])


def test_every_quirk_is_documented_and_uniquely_named():
    ids = [quirk.id for quirk in QUIRKS]
    assert len(ids) == len(set(ids))
    assert all(len(quirk.description) > 40 for quirk in QUIRKS)


# ----------------------------------------------------------------------- routes
def test_routes_render_with_owner_repo_and_ids():
    profile = DEFAULT_PROFILE
    assert profile.route("projects", owner="o", repo="r") == "/o/r/projects"
    assert (
        profile.route("column_move", owner="o", repo="r", project_id=3, column_id=7)
        == "/o/r/projects/3/7/move"
    )
    assert profile.route("milestone_delete", owner="o", repo="r") == "/o/r/milestones/delete"


def test_an_unknown_route_is_a_programming_error():
    with pytest.raises(KeyError):
        DEFAULT_PROFILE.route("no_such_route", owner="o", repo="r")


def test_a_quirk_can_replace_a_single_route_without_losing_the_rest():
    quirk = Quirk(
        id="test-only",
        description="x" * 50,
        overrides={"routes": {"projects": "/{owner}/{repo}/boards"}},
    )
    patched = quirk.apply(DEFAULT_PROFILE)

    assert patched.route("projects", owner="o", repo="r") == "/o/r/boards"
    assert patched.route("issues", owner="o", repo="r") == "/o/r/issues"


# --------------------------------------------------------------------- patterns
def test_the_first_matching_candidate_wins():
    profile = Profile(patterns={"thing": (r"first-(\d+)", r"second-(\d+)")})

    assert profile.search("thing", "second-2").group(1) == "2"
    assert profile.search("thing", "first-1 second-2").group(1) == "1"
    assert profile.search("thing", "nothing") is None


def test_finditer_never_mixes_two_candidates():
    """Candidates are alternatives; combining them would double-count."""
    profile = Profile(patterns={"thing": (r"a-(\d+)", r"b-(\d+)")})

    matches = list(profile.finditer("thing", "a-1 a-2 b-3"))

    assert [m.group(1) for m in matches] == ["1", "2"]


def test_split_falls_through_to_a_candidate_that_divides_the_text():
    profile = Profile(patterns={"sep": (r"(<hr/>)", r"(<br/>)")})

    assert profile.split("sep", "x<br/>y") == ["x", "<br/>", "y"]
    assert profile.split("sep", "no separators") == ["no separators"]


def test_patterns_can_be_parameterized_with_escaped_values():
    profile = Profile(patterns={"raw": (r'id="{id}-raw">(.*?)</div>',)})

    found = profile.search("raw", '<div id="issue-5-raw">body</div>', id="issue-5")

    assert found.group(1) == "body"


def test_with_csrf_mode_leaves_the_rest_of_the_profile_intact():
    profile = profile_for(Version(16, 0, 3))
    adapted = profile.with_csrf_mode(CSRF_TOKEN)

    assert adapted.csrf_mode == CSRF_TOKEN
    assert adapted.routes == profile.routes
    assert adapted.version == profile.version


def test_card_types_map_to_the_values_the_form_posts():
    """The dropdown posts 0 for "Text only" and 1 for "Images and text".

    Sending 1 for text and 2 for images is what an earlier mapping did: 1
    silently produced an images board, and 2 was out of range and stored as
    text. Both are live-verified in ``test_live_client``.
    """
    profile = profile_for(Version(16, 0, 3))

    assert profile.card_types == {"text": "0", "images_and_text": "1"}


def test_every_version_agrees_on_the_card_type_values():
    """No release in the supported range posts different values."""
    versions = [Version(1, 20, 6), Version(1, 21, 11), Version(7, 0, 16),
                Version(10, 0, 3), Version(13, 0, 5), Version(16, 0, 3)]

    assert {
        tuple(sorted(profile_for(v).card_types.items())) for v in versions
    } == {(("images_and_text", "1"), ("text", "0"))}


def test_redirect_writes_names_only_the_routes_that_redirect_on_success():
    """Board mutations answer 200 on success, so a 200 there is not a refusal."""
    profile = profile_for(Version(16, 0, 3))

    assert profile.redirect_writes == {
        "project_new", "project_edit", "milestone_new", "milestone_edit"
    }
    for ajax_route in (
        "column_new", "column", "column_default", "column_move",
        "issue_projects", "milestone_delete", "project_delete",
        "milestone_close", "project_close",
    ):
        assert ajax_route not in profile.redirect_writes


def test_no_version_changes_which_writes_redirect():
    """Live-probed on 1.20 through 16: the same four routes, everywhere."""
    versions = [Version(1, 20, 6), Version(1, 21, 11), Version(9, 0, 3),
                Version(14, 0, 5), Version(16, 0, 3)]

    assert {frozenset(profile_for(v).redirect_writes) for v in versions} == {
        frozenset({"project_new", "project_edit", "milestone_new", "milestone_edit"})
    }


def test_the_form_error_pattern_matches_the_flash_forgejo_renders():
    """The real shapes: a <p> inside the container, with 13+ adding an attribute."""
    profile = profile_for(Version(16, 0, 3))
    older = ('<div class="ui negative message flash-message flash-error">\n\t\t'
             "<p>Due date format must be &#39;yyyy-mm-dd&#39;.</p>\n\t</div>")
    newer = ('<div id="flash-message" class="ui negative message flash-message '
             'flash-error" hx-swap-oob="true">\n\t\t'
             "<p>form.Title cannot be empty.</p>\n\t</div>")

    assert profile.search("form_error", older).group(1) == (
        "Due date format must be &#39;yyyy-mm-dd&#39;."
    )
    assert profile.search("form_error", newer).group(1) == (
        "form.Title cannot be empty."
    )


def test_an_empty_flash_container_is_not_read_as_a_reason():
    """A rejection with no readable reason must not report a blank one."""
    profile = profile_for(Version(16, 0, 3))
    empty = '<div class="ui negative message flash-message flash-error">\n\t</div>'

    assert profile.search("form_error", empty) is None


def test_the_form_error_pattern_falls_back_to_bare_text():
    """A future template that drops the paragraph is still readable."""
    profile = profile_for(Version(16, 0, 3))
    bare = '<div class="flash-error">Something went wrong.</div>'

    assert profile.search("form_error", bare).group(1) == "Something went wrong."


def test_the_edit_form_patterns_read_both_textarea_shapes():
    """Forgejo 10 moved name="content" behind the Markdown editor's attributes."""
    profile = profile_for(Version(16, 0, 3))
    plain = '<textarea name="content">kept</textarea>'
    editor = ('<textarea class="markdown-text-editor js-quick-submit" '
              'name="content" aria-label="Description">kept</textarea>')

    for html in (plain, editor):
        assert profile.search("milestone_edit_description", html).group(1) == "kept"
        assert profile.search("project_edit_description", html).group(1) == "kept"


def test_the_milestone_form_patterns_read_the_title_and_deadline():
    profile = profile_for(Version(16, 0, 3))
    html = ('<input name="title" placeholder="Title" value="Sprint 1" required>'
            '<input type="date" id="deadline" name="deadline" value="2029-12-31">')

    assert profile.search("milestone_edit_title", html).group(1) == "Sprint 1"
    assert profile.search("milestone_edit_deadline", html).group(1) == "2029-12-31"


@pytest.mark.parametrize("color", ["#e01e5a", "#E01E5A", "#000000"])
def test_the_column_color_pattern_accepts_the_shapes_forgejo_takes(color):
    """Six hex digits, either case. Live-verified against a real instance."""
    profile = profile_for(Version(16, 0, 3))

    assert profile.compiled("column_color")[0].fullmatch(color)


@pytest.mark.parametrize(
    "color", ["red", "e01e5a", "#12345", "#gggggg", "#", "#fffx", "#fff"]
)
def test_the_column_color_pattern_rejects_what_forgejo_answers_500_for(color):
    """`#fff` belongs here: the CSS shorthand produces that same HTTP 500."""
    profile = profile_for(Version(16, 0, 3))

    assert not profile.compiled("column_color")[0].fullmatch(color)

