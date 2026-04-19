"""Async Discourse API fetcher."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import aiohttp

from .models import DiscourseCategory, DiscoursePost, DiscourseTopic

DEFAULT_SITE = "https://discourse.ubuntu.com"

_POST_URL = "{site}/posts/{id}.json"
_POST_LATEST_EDIT_URL = "{site}/posts/{id}/revisions/latest.json"
_CATEGORY_JSON_URL = "{site}/c/{id}/show.json"
_CATEGORY_TOPIC_LIST_URL = "{site}/c/{id}.json?state=muted"
_CATEGORY_LIST_URL = "{site}/categories.json?include_subcategories=true"
_TOPIC_URL = "{site}/t/{id}.json"
_TOPIC_BATCH_URL = "{site}/t/{id}/posts.json"
_USER_URL = "{site}/u/{id}.json"


def _site(site: str | None) -> str:
    return site or DEFAULT_SITE


async def _get_json(session: aiohttp.ClientSession, url: str) -> dict | None:
    """GET url, return parsed JSON or None on error."""
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
            logging.debug("HTTP %s fetching %s", resp.status, url)
            return None
    except (aiohttp.ClientError, json.JSONDecodeError) as exc:
        logging.debug("Error fetching %s: %s", url, exc)
        return None


def _extract_posts(json_output: dict) -> list[DiscoursePost]:
    posts = []
    stream = json_output.get("post_stream", {})
    for p in stream.get("posts", []):
        post = DiscoursePost(p)
        if post.get_id() is not None:
            posts.append(post)
    return posts


async def get_post_by_id(
    session: aiohttp.ClientSession, post_id: int, site: str | None = None
) -> DiscoursePost | None:
    data = await _get_json(session, _POST_URL.format(site=_site(site), id=post_id))
    return DiscoursePost(data) if data else None


async def get_batch_of_posts(
    session: aiohttp.ClientSession, topic_id: int, post_ids: list[int], site: str | None = None
) -> list[DiscoursePost]:
    if not post_ids:
        return []
    url = _TOPIC_BATCH_URL.format(site=_site(site), id=topic_id)
    url += "?" + "&".join(f"post_ids[]={pid}" for pid in post_ids)
    data = await _get_json(session, url)
    return _extract_posts(data) if data else []


async def add_posts_to_topic(
    session: aiohttp.ClientSession, topic: DiscourseTopic, site: str | None = None
) -> None:
    """Fetch and attach all posts for a topic."""
    url = _TOPIC_URL.format(site=_site(site), id=topic.get_id())
    data = await _get_json(session, url)
    if not data:
        return

    for post in _extract_posts(data):
        topic.add_post(post)

    # Find missing posts from the stream index
    known_ids = {str(p.get_id()) for p in topic.get_posts()}
    stream_ids = data.get("post_stream", {}).get("stream", [])
    missing = [pid for pid in stream_ids if str(pid) not in known_ids]

    if missing:
        topic_id = topic.get_id()
        assert topic_id is not None
        chunk_size = int(data.get("chunk_size", 1))
        tasks = []
        for i in range(0, len(missing), chunk_size):
            batch = missing[i : i + chunk_size]
            tasks.append(get_batch_of_posts(session, topic_id, batch, site))
        results = await asyncio.gather(*tasks)
        for batch_posts in results:
            for post in batch_posts:
                topic.add_post(post)


async def _get_category_by_id(
    session: aiohttp.ClientSession, cat_id: int, site: str | None = None
) -> DiscourseCategory | None:
    data = await _get_json(session, _CATEGORY_JSON_URL.format(site=_site(site), id=cat_id))
    if data and "category" in data:
        return DiscourseCategory(data["category"])
    return None


async def get_category_by_name(
    session: aiohttp.ClientSession, category_name: str, site: str | None = None
) -> DiscourseCategory | None:
    """Find a category (or subcategory via 'parent/child' notation) by name/slug."""
    nav = category_name.split("/")
    data = await _get_json(session, _CATEGORY_LIST_URL.format(site=_site(site)))
    if not data:
        return None

    cats = data.get("category_list", {}).get("categories", [])
    found: DiscourseCategory | None = None

    for cat_json in cats:
        if (
            cat_json.get("name", "").lower() == nav[0].lower()
            or cat_json.get("slug", "").lower() == nav[0].lower()
        ):
            found = DiscourseCategory(cat_json)
            # Some sites omit subcategory_list but provide subcategory_ids
            if "subcategory_list" not in cat_json and "subcategory_ids" in cat_json:
                sub_tasks = [_get_category_by_id(session, sid, site) for sid in cat_json["subcategory_ids"]]
                subs = await asyncio.gather(*sub_tasks)
                for sub in subs:
                    if sub:
                        found.add_subcategory(sub)
            break

    for level in nav[1:]:
        if found:
            found = found.get_subcategory_by_name(level)
        else:
            break

    return found


async def add_topics_to_category(
    session: aiohttp.ClientSession,
    category: DiscourseCategory,
    ignore_before: datetime | None = None,
    site: str | None = None,
) -> None:
    """Recursively fetch all topics for a category, stopping at old topics."""
    url = _CATEGORY_TOPIC_LIST_URL.format(site=_site(site), id=category.get_id())
    await _add_topics_from_url(session, category, url, ignore_before, site)


async def _add_topics_from_url(
    session: aiohttp.ClientSession,
    category: DiscourseCategory,
    url: str,
    ignore_before: datetime | None,
    site: str | None,
) -> None:
    data = await _get_json(session, url)
    if not data:
        return

    topic_list = data.get("topic_list", {})
    for t in topic_list.get("topics", []):
        topic = DiscourseTopic(t)
        update_time = topic.get_latest_update_time()
        if ignore_before is None or update_time is None or update_time >= ignore_before:
            category.add_topic(topic)
        elif not topic.get_pinned():
            return  # topics are date-ordered; stop early

    if "more_topics_url" in topic_list:
        raw_next = topic_list["more_topics_url"]
        next_url = f"{_site(site)}{'.json?'.join(raw_next.split('?'))}"
        await _add_topics_from_url(session, category, next_url, ignore_before, site)


async def get_editor_name(
    session: aiohttp.ClientSession, post: DiscoursePost, site: str | None = None
) -> str | None:
    """Fetch the display name of the most recent editor of a main post."""
    author = post.get_author_username() if not post.get_author_name() else post.get_author_name()
    rev_url = _POST_LATEST_EDIT_URL.format(site=_site(site), id=post.get_id())
    data = await _get_json(session, rev_url)
    if not data or "username" not in data:
        return author

    username = data["username"]
    user_url = _USER_URL.format(site=_site(site), id=username)
    user_data = await _get_json(session, user_url)
    if user_data and "user" in user_data and user_data["user"].get("name"):
        return user_data["user"]["name"]
    return username


def get_topic_url(topic: DiscourseTopic, site: str | None = None) -> str:
    return f"{_site(site)}/t/{topic.get_id()}"


def get_post_url(topic: DiscourseTopic, post_index: int, site: str | None = None) -> str:
    url = get_topic_url(topic, site)
    posts = topic.get_posts()
    if 0 <= post_index < len(posts):
        url += f"/{posts[post_index].get_post_number()}"
    return url


def get_post_url_by_id(post: DiscoursePost, site: str | None = None) -> str:
    return f"{_site(site)}/p/{post.get_id()}"


def author_str(post: DiscoursePost) -> str | None:
    name = post.get_author_name()
    return post.get_author_username() if not name else name
