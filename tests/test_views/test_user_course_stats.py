"""
Tests for the per-learner course stats (the Discussions "Learners" tab) on the MySQL backend.

Covers two defects of the stock code:

* looking up the stats of specific usernames loaded every forum user on the site;
* deleting a thread (or a response) left the stats of the *other* users whose comments
  disappeared with it counting content that no longer exists.
"""

from typing import Any, Generator

import pytest

from forum.api.users import get_user_course_stats
from forum.backends.mysql.api import MySQLBackend
from forum.backends.mysql.models import CourseStat
from test_utils.client import APIClient

pytestmark = pytest.mark.django_db

COURSE_ID = "course-v1:Org+Course+Run"
OTHER_COURSE_ID = "course-v1:Org+Other+Run"


@pytest.fixture(autouse=True)
def use_mysql_backend(monkeypatch: pytest.MonkeyPatch) -> Generator[Any, Any, Any]:
    """Run every test in this module against the MySQL backend."""
    monkeypatch.setattr("forum.backend.is_mysql_backend_enabled", lambda course_id: True)
    yield MySQLBackend


def make_users() -> tuple[str, str, str]:
    """Create the thread author (1), a responder (2) and a replier (3)."""
    for user_id, username in (("1", "author"), ("2", "responder"), ("3", "replier")):
        MySQLBackend.find_or_create_user(user_id, username=username)
    return "1", "2", "3"


def make_thread(author_id: str) -> str:
    """Create a thread."""
    return MySQLBackend.create_thread(
        {
            "title": "Thread",
            "body": "Thread",
            "course_id": COURSE_ID,
            "commentable_id": "topic",
            "author_id": author_id,
            "author_username": "author",
            "abuse_flaggers": [],
            "historical_abuse_flaggers": [],
            "thread_type": "discussion",
        }
    )


def make_comment(
    thread_id: str, author_id: str, parent_id: Any = None, **extra: Any
) -> str:
    """Create a response (no parent) or a reply (with a parent) and count it in the stats."""
    data = {
        "body": "Comment",
        "course_id": COURSE_ID,
        "author_id": author_id,
        "comment_thread_id": thread_id,
        "author_username": "someone",
        "parent_id": parent_id,
        "depth": 1 if parent_id else 0,
        **extra,
    }
    return MySQLBackend.create_comment(data)


def stat(user_id: str, field: str) -> int:
    """Read one stored counter of a user for the test course."""
    return int(getattr(CourseStat.objects.get(user_id=user_id, course_id=COURSE_ID), field))


def test_deleting_a_thread_refreshes_the_stats_of_everyone_who_commented(
    api_client: APIClient,
) -> None:
    """The responder's and replier's counters must not keep counting deleted comments."""
    author_id, responder_id, replier_id = make_users()
    thread_id = make_thread(author_id)
    response_id = make_comment(thread_id, responder_id)
    make_comment(thread_id, replier_id, parent_id=response_id)
    assert stat(responder_id, "responses") == 1
    assert stat(replier_id, "replies") == 1

    response = api_client.delete_json(f"/api/v2/threads/{thread_id}")

    assert response.status_code == 200
    assert stat(responder_id, "responses") == 0
    assert stat(replier_id, "replies") == 0


def test_deleting_a_response_refreshes_the_stats_of_its_repliers(
    api_client: APIClient,
) -> None:
    """Replies are deleted with their response, so their authors' counters must drop too."""
    author_id, responder_id, replier_id = make_users()
    thread_id = make_thread(author_id)
    response_id = make_comment(thread_id, responder_id)
    make_comment(thread_id, replier_id, parent_id=response_id)
    assert stat(replier_id, "replies") == 1

    response = api_client.delete_json(f"/api/v2/comments/{response_id}")

    assert response.status_code == 200
    assert stat(responder_id, "responses") == 0
    assert stat(replier_id, "replies") == 0


def test_deleting_a_thread_keeps_stats_of_content_that_still_exists(
    api_client: APIClient,
) -> None:
    """Only the deleted thread's comments stop counting; the same users' other comments stay."""
    author_id, responder_id, _ = make_users()
    doomed_thread_id = make_thread(author_id)
    surviving_thread_id = make_thread(author_id)
    make_comment(doomed_thread_id, responder_id)
    make_comment(surviving_thread_id, responder_id)
    assert stat(responder_id, "responses") == 2

    assert api_client.delete_json(f"/api/v2/threads/{doomed_thread_id}").status_code == 200

    assert stat(responder_id, "responses") == 1


def test_anonymous_comments_are_not_in_the_author_lists() -> None:
    """Anonymous content never counts towards a user's stats, so it needs no refresh."""
    author_id, responder_id, replier_id = make_users()
    thread_id = make_thread(author_id)
    response_id = make_comment(thread_id, responder_id)
    make_comment(thread_id, replier_id, parent_id=response_id, anonymous=True)

    assert MySQLBackend.get_comment_author_ids_of_a_thread(thread_id) == [responder_id]
    assert MySQLBackend.get_descendant_comment_author_ids(response_id) == []


def test_comment_author_lists_are_distinct_and_include_replies_at_any_depth() -> None:
    """Every non-anonymous author is listed once, whatever the nesting."""
    author_id, responder_id, replier_id = make_users()
    thread_id = make_thread(author_id)
    response_id = make_comment(thread_id, responder_id)
    make_comment(thread_id, responder_id)
    reply_id = make_comment(thread_id, replier_id, parent_id=response_id)
    make_comment(thread_id, author_id, parent_id=reply_id)

    assert MySQLBackend.get_comment_author_ids_of_a_thread(thread_id) == [
        author_id,
        responder_id,
        replier_id,
    ]
    assert MySQLBackend.get_descendant_comment_author_ids(response_id) == [
        author_id,
        replier_id,
    ]
    assert MySQLBackend.get_descendant_comment_author_ids(reply_id) == [author_id]


def test_stats_for_usernames_are_looked_up_without_loading_every_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The username search must not go through get_users(), which builds a dict per user."""
    make_users()
    for user_id, threads in (("1", 5), ("2", 3), ("3", 0)):
        MySQLBackend.update_stats_for_course(user_id, COURSE_ID, threads=threads)
    MySQLBackend.update_stats_for_course("2", OTHER_COURSE_ID, threads=9)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("get_users() must not be used for a username search")

    monkeypatch.setattr(MySQLBackend, "get_users", fail)

    result = get_user_course_stats(COURSE_ID, usernames="responder,author")

    assert [row["username"] for row in result["user_stats"]] == ["responder", "author"]
    assert result["count"] == 2
    assert result["num_pages"] == 1


def test_stats_for_usernames_fall_back_to_scanning_when_a_backend_cannot_look_them_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backends without the targeted lookup keep working through the generic path."""
    make_users()
    MySQLBackend.update_stats_for_course("2", COURSE_ID, threads=3)

    def unsupported(*args: Any, **kwargs: Any) -> None:
        raise NotImplementedError

    monkeypatch.setattr(MySQLBackend, "get_user_stats_for_usernames", unsupported)

    result = get_user_course_stats(COURSE_ID, usernames="responder")

    assert [row["username"] for row in result["user_stats"]] == ["responder"]
