"""Discourse triage result: holds fetched data and renders output."""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import aiohttp

from ...config import StarTriageConfig
from ...output import OutputConfig, OutputFormat, TriageResult, hyperlink
from ...savebugs import BugPersistor
from ...source import FetchMode, TaskFilterOptions
from .finder import DiscourseFinder
from .models import DiscoursePost, DiscourseTopic

logger = logging.getLogger(__name__)


class PostStatus(StrEnum):
    UNCHANGED = "unchanged"
    NEW = "new"
    UPDATED = "updated"


@dataclass
class PostWithMetadata:
    post: DiscoursePost
    status: PostStatus
    url: str
    update_date: datetime | None = None
    contains_relevant_posts: bool = False
    replies: list[PostWithMetadata] = field(default_factory=list)

    def add_reply(self, meta: PostWithMetadata) -> None:
        self.replies.append(meta)


def _create_post_meta(post: DiscoursePost, start: datetime, end: datetime, url: str) -> PostWithMetadata:
    created = post.get_creation_time()
    updated = post.get_update_time()

    if updated and created and updated != created and start <= updated < end:
        return PostWithMetadata(post, PostStatus.UPDATED, url, updated)
    if created and start <= created < end:
        return PostWithMetadata(post, PostStatus.NEW, url, created)
    return PostWithMetadata(post, PostStatus.UNCHANGED, url)


def _set_relevant(meta: PostWithMetadata) -> bool:
    is_relevant = any(_set_relevant(r) for r in meta.replies)
    is_relevant = is_relevant or meta.status != PostStatus.UNCHANGED
    meta.contains_relevant_posts = is_relevant
    return is_relevant


@dataclass
class TopicActivity:
    """A topic with new/updated posts in the triage interval, as reply tree."""

    topic: DiscourseTopic
    url: str
    status: PostStatus
    date: datetime | None
    # all user posts in topic order
    posts: list[PostWithMetadata]
    # top-level reply chains containing relevant posts, main post excluded
    threads: list[PostWithMetadata]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.topic.get_id(),
            "title": self.topic.get_name(),
            "url": self.url,
            "status": self.status,
            "date": self.date,
            "tags": self.topic.get_tags(),
            "threads": [_post_to_dict(p) for p in self.threads],
        }


def _post_to_dict(meta: PostWithMetadata) -> dict[str, Any]:
    post = meta.post
    return {
        "id": post.get_id(),
        "number": post.get_post_number(),
        "url": meta.url,
        "author": post.get_author_name() or post.get_author_username(),
        "status": meta.status,
        "date": meta.update_date,
        "content": post.get_data(),
        "replies": [_post_to_dict(r) for r in meta.replies if r.contains_relevant_posts],
    }


def _topic_activity(
    finder: DiscourseFinder, topic: DiscourseTopic, start: datetime, end: datetime, is_triage: bool
) -> TopicActivity | None:
    """Return the topic's activity in [start, end), or None if nothing changed.

    For the team's own triage topics the main post is ignored, only replies count.
    """
    posts = [
        _create_post_meta(p, start, end, finder.get_post_url(topic, i))
        for i, p in enumerate(topic.get_posts())
        if not p.is_small_action()
    ]

    if is_triage:
        for m in posts:
            if m.post.is_main_post_for_topic():
                m.status = PostStatus.UNCHANGED

    if all(m.status == PostStatus.UNCHANGED for m in posts):
        return None

    # Build reply tree
    top_level: list[PostWithMetadata] = []
    for post in posts:
        replied_to = next(
            (m for m in posts if m.post.get_post_number() == post.post.get_reply_to_number()),
            None,
        )
        if replied_to is None or replied_to.post.is_main_post_for_topic():
            top_level.append(post)
        if replied_to is not None:
            replied_to.add_reply(post)

    for post in posts:
        _set_relevant(post)

    main_post = next((m for m in top_level if m.post.is_main_post_for_topic()), None)

    # latest date among relevant posts
    best_date = max((p.update_date for p in posts if p.update_date is not None), default=None)

    return TopicActivity(
        topic=topic,
        url=finder.get_topic_url(topic),
        status=main_post.status if main_post else PostStatus.UNCHANGED,
        date=(main_post.update_date if main_post else None) or best_date,
        posts=posts,
        threads=[m for m in top_level if m is not main_post and m.contains_relevant_posts],
    )


@dataclass
class CategoryResult:
    category_name: str
    topics: list[TopicActivity]


@dataclass
class DiscourseTriage(TriageResult):
    """Holds all fetched Discourse results for one triage run."""

    results: list[CategoryResult]
    site: str

    @property
    def had_updates(self) -> bool:
        return any(r.topics for r in self.results)

    async def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "categories": [
                {"name": r.category_name, "topics": [t.to_dict() for t in r.topics]} for r in self.results
            ],
        }

    async def print_section(
        self,
        cfg: OutputConfig,
    ) -> None:
        """Print the # Forum section to stdout (and optionally to a markdown file)."""

        topic_count = sum(len(r.topics) for r in self.results)
        logging.info("Showing forum comments on %s", self.site)

        match cfg.fmt:
            case OutputFormat.MARKDOWN:
                print("## Discourse", file=cfg.out)
            case OutputFormat.TERMINAL:
                print(f"## Discourse ({topic_count} topic{'s' if topic_count != 1 else ''})", file=cfg.out)
            case _:
                raise NotImplementedError

        if topic_count == 0:
            match cfg.fmt:
                case OutputFormat.MARKDOWN:
                    print("no activity", file=cfg.out)
                case OutputFormat.TERMINAL:
                    ...
                case _:
                    raise NotImplementedError
            return

        for result in self.results:
            logger.info("Comments belonging to the %s category:", result.category_name)
            await self._print_category_comments(result.topics, cfg)

    async def record(self, persistor: BugPersistor) -> None:
        pass  # no bugs to record, just forum comments

    @staticmethod
    def _content_preview(post: DiscoursePost, max_len: int = 50) -> str:
        """Return the first *max_len* characters of the post body, cleaned up."""
        raw = (post.get_data() or "").strip()
        # Collapse whitespace / newlines so the preview fits on one line
        preview = " ".join(raw.split())
        if len(preview) > max_len:
            preview = preview[: max_len - 1] + "…"
        return preview or "(no content)"

    def _print_single_comment(
        self,
        post: DiscoursePost,
        status: PostStatus,
        date_updated: datetime | None,
        post_url: str,
        cfg: OutputConfig,
    ) -> None:
        status_str = {PostStatus.UPDATED: "*", PostStatus.NEW: "+"}.get(status, "")
        date_str = f" {date_updated.strftime('%Y-%m-%d')}" if date_updated else ""
        preview = self._content_preview(post)

        match cfg.fmt:
            case OutputFormat.MARKDOWN:
                link = hyperlink(post_url, str(post.get_id()), cfg.fmt)
                print(f"{status_str}{link} [{date_str.strip()}] {preview}", file=cfg.out)
            case OutputFormat.TERMINAL:
                post_txt = f"{post.get_id()} {preview}"
                if cfg.terminal_links:
                    post_ref = hyperlink(post_url, post_txt, cfg.fmt)
                    url_str = ""
                else:
                    post_ref = post_txt
                    url_str = f" ({post_url})"
                print(f"{status_str}{post_ref} [{date_str.strip()}]{url_str}", file=cfg.out)
            case _:
                raise NotImplementedError

    def _print_topic_header(
        self,
        activity: TopicActivity,
        cfg: OutputConfig,
        topic_name_length: int = 50,
    ) -> None:
        topic_url = activity.url
        date_updated = activity.date
        status_str = {PostStatus.UPDATED: "*", PostStatus.NEW: "+"}.get(activity.status, "")
        if not status_str:
            topic_name_length += 1

        name = activity.topic.get_name() or ""
        if len(name) > topic_name_length:
            name = name[: topic_name_length - 1] + "…"
        else:
            name = name.ljust(topic_name_length)

        match cfg.fmt:
            case OutputFormat.MARKDOWN:
                link = hyperlink(topic_url, name.strip(), cfg.fmt)
                date_str = f" {date_updated.strftime('%Y-%m-%d')}" if date_updated else ""
                print(f"#### {status_str}{link}{date_str}", file=cfg.out)

            case OutputFormat.TERMINAL:
                if cfg.terminal_links:
                    link = hyperlink(topic_url, name, cfg.fmt)
                else:
                    link = name

                date_str = f" {date_updated.strftime('%Y-%m-%d')}" if date_updated else ""
                url_str = "" if cfg.terminal_links else f" ({topic_url})"
                print(f"{status_str}{link} [{date_str.strip()}]{url_str}", file=cfg.out)

            case _:
                raise NotImplementedError

    def _print_comment_chain(self, meta: PostWithMetadata, cfg: OutputConfig, chain: list[str]) -> None:
        if not meta.contains_relevant_posts:
            return

        if chain:
            indent = chain[0] + "".join("  " + c for c in chain[1:])
            if cfg.fmt == OutputFormat.MARKDOWN:
                # Discourse strips leading regular spaces; replace with non-breaking spaces
                indent = indent.replace(" ", "\u00a0")
                print(indent, end="─\u00a0", file=cfg.out)
            else:
                print(indent, end="─ ", file=cfg.out)

        self._print_single_comment(meta.post, meta.status, meta.update_date, meta.url, cfg)

        relevant_replies = [r for r in meta.replies if r.contains_relevant_posts]
        if relevant_replies:
            if chain and chain[-1] == "├":
                chain[-1] = "│"
            elif chain and chain[-1] == "└":
                chain[-1] = " "
            chain.append("├")
            for reply in relevant_replies[:-1]:
                self._print_comment_chain(reply, cfg, chain)
            chain[-1] = "└"
            self._print_comment_chain(relevant_replies[-1], cfg, chain)
            chain.pop()

    async def _print_category_comments(self, topics: list[TopicActivity], cfg: OutputConfig) -> None:
        for activity in topics:
            self._print_topic_header(activity, cfg)

            for post in activity.threads[:-1]:
                self._print_comment_chain(post, cfg, ["├"])
            if activity.threads:
                self._print_comment_chain(activity.threads[-1], cfg, ["└"])

            print(file=cfg.out)  # blank line after each topic (spacing in terminal / notes in markdown)

        if cfg.open_in_browser:
            # only open the latest updated post in each topic
            for activity in topics:
                for post in reversed(activity.posts):
                    if post.status == PostStatus.UNCHANGED:
                        continue

                    webbrowser.open_new_tab(post.url)
                    await asyncio.sleep(0.2)
                    break


async def find(
    config: StarTriageConfig,
    filter: TaskFilterOptions,
    mode: FetchMode,
) -> DiscourseTriage:
    """Fetch all Discourse data for the given categories and date range."""

    team_config = config.get_team(filter.team)

    async with aiohttp.ClientSession() as session:
        finder = DiscourseFinder()

        # Resolve triage category names → IDs
        resolved_triage_ids: set[int] = set()
        for cat_name in team_config.discourse_triage_categories:
            cat = await finder.get_category_by_name(session, cat_name.strip())
            cat_id = cat.get_id() if cat is not None else None
            if cat_id is not None:
                resolved_triage_ids.add(cat_id)
            else:
                logger.warning("Unable to find triage category: %s", cat_name)

        results: list[CategoryResult] = []
        for category_name in [c.strip() for c in team_config.discourse_categories]:
            category = await finder.get_category_by_name(session, category_name)
            if category is None:
                logger.warning("Unable to find category: %s", category_name)
                continue

            await finder.add_topics_to_category(
                session, category, ignore_before=filter.start, ignore_after=filter.end
            )

            logger.info("Fetching Discourse comments…")
            topics = category.get_topics()
            await asyncio.gather(*[finder.add_posts_to_topic(session, t) for t in topics])

            activities = [
                _topic_activity(
                    finder, t, filter.start, filter.end, is_triage=t.get_category_id() in resolved_triage_ids
                )
                for t in topics
            ]
            results.append(CategoryResult(category_name, [a for a in activities if a is not None]))

    return DiscourseTriage(results=results, site=finder.site)
