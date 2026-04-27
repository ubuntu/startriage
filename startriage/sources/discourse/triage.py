"""Discourse triage result: holds fetched data and renders output."""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import aiohttp

from ...config import StarTriageConfig
from ...enums import FetchMode
from ...output import OutputConfig, OutputFormat, TriageResult, hyperlink
from ...savebugs import BugPersistor
from ...source import TaskFilterOptions
from .finder import DiscourseFinder
from .models import DiscourseCategory, DiscoursePost, DiscourseTopic


class PostStatus(Enum):
    UNCHANGED = 0
    NEW = 1
    UPDATED = 2


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


def _topic_is_relevant(
    topic: DiscourseTopic, start: datetime, end: datetime, triage_category_ids: set[int]
) -> bool:
    """Return True if *topic* has at least one new/updated post in [start, end)."""
    posts = topic.get_posts()
    if not posts:
        return False
    meta_list = [_create_post_meta(p, start, end, "") for p in posts]
    # ignore the team's triage posts, but consider replies to them.
    cat_id = topic.get_category_id()
    is_triage = cat_id is not None and cat_id in triage_category_ids
    if is_triage:
        for m in meta_list:
            if m.post.is_main_post_for_topic():
                m.status = PostStatus.UNCHANGED
    return any(m.status != PostStatus.UNCHANGED for m in meta_list)


@dataclass
class CategoryResult:
    category_name: str
    category: DiscourseCategory
    site: str | None


@dataclass
class DiscourseTriage(TriageResult):
    """Holds all fetched Discourse results for one triage run."""

    finder: DiscourseFinder  # api access
    filter: TaskFilterOptions
    results: list[CategoryResult]
    site: str | None = None
    had_updates: bool = False
    triage_category_ids: set[int] = field(default_factory=set)

    def _count_relevant_topics(self) -> int:
        """Count topics across all categories that have new/updated posts."""
        return sum(
            1
            for result in self.results
            for topic in result.category.get_topics()
            if _topic_is_relevant(topic, self.filter.start, self.filter.end, self.triage_category_ids)
        )

    async def print_section(
        self,
        cfg: OutputConfig,
    ) -> None:
        """Print the # Forum section to stdout (and optionally to a markdown file)."""

        topic_count = self._count_relevant_topics()
        site_info = f" on {self.site}" if self.site else ""
        logging.info("Showing forum comments%s", site_info)

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
            logging.info("Comments belonging to the %s category:", result.category_name)
            await self._print_category_comments(
                result.category,
                self.filter.start,
                self.filter.end,
                cfg,
                triage_category_ids=self.triage_category_ids,
            )

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
        topic: DiscourseTopic,
        status: PostStatus,
        date_updated: datetime | None,
        cfg: OutputConfig,
        topic_name_length: int = 50,
    ) -> None:
        topic_url = self.finder.get_topic_url(topic)
        status_str = {PostStatus.UPDATED: "*", PostStatus.NEW: "+"}.get(status, "")
        if not status_str:
            topic_name_length += 1

        name = topic.get_name() or ""
        if len(name) > topic_name_length:
            name = name[: topic_name_length - 1] + "…"
        else:
            name = name.ljust(topic_name_length)

        match cfg.fmt:
            case OutputFormat.MARKDOWN:
                link = hyperlink(topic_url, name.strip(), cfg.fmt)
                date_str = f" {date_updated.strftime('%Y-%m-%d')}" if date_updated else ""
                print(f"### {status_str}{link}{date_str}", file=cfg.out)

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

    async def _get_editor(self, session: aiohttp.ClientSession | None, post: DiscoursePost) -> str | None:
        if session is None:
            return None
        return await self.finder.get_editor_name(session, post)

    async def _print_category_comments(
        self,
        category: DiscourseCategory,
        start: datetime,
        end: datetime,
        cfg: OutputConfig,
        triage_category_ids: set[int] | None = None,
    ) -> None:

        relevant_topics = []
        for topic in category.get_topics():
            posts_raw = topic.get_posts()
            posts = [
                _create_post_meta(p, start, end, self.finder.get_post_url(topic, i))
                for i, p in enumerate(posts_raw)
                if not p.is_small_action()
            ]

            # Topics in the triage category: ignore main-post updates, show replies only.
            cat_id = topic.get_category_id()
            is_triage = (
                triage_category_ids is not None and cat_id is not None and cat_id in triage_category_ids
            )
            if is_triage:
                for m in posts:
                    if m.post.is_main_post_for_topic():
                        m.status = PostStatus.UNCHANGED

            topic_relevant = any(m.status != PostStatus.UNCHANGED for m in posts)
            if not topic_relevant:
                continue
            relevant_topics.append(posts)

            # Build reply tree
            final_list: list[PostWithMetadata] = []
            for post in posts:
                replied_to = next(
                    (m for m in posts if m.post.get_post_number() == post.post.get_reply_to_number()),
                    None,
                )
                if replied_to is None or replied_to.post.is_main_post_for_topic():
                    final_list.append(post)
                if replied_to is not None:
                    replied_to.add_reply(post)

            for post in posts:
                _set_relevant(post)

            # Find main post
            main_post = next((m for m in final_list if m.post.is_main_post_for_topic()), None)

            # Best date for the header: latest update_date among relevant posts
            # (falls back to None if no relevant post has a date)
            best_date = max(
                (p.update_date for p in posts if p.update_date is not None),
                default=None,
            )

            if main_post:
                self._print_topic_header(
                    topic,
                    main_post.status,
                    main_post.update_date or best_date,
                    cfg,
                )
                final_list = [m for m in final_list if m != main_post and m.contains_relevant_posts]
            else:
                self._print_topic_header(topic, PostStatus.UNCHANGED, best_date, cfg)

            for post in final_list[:-1]:
                self._print_comment_chain(post, cfg, ["├"])
            if final_list:
                self._print_comment_chain(final_list[-1], cfg, ["└"])

            print(file=cfg.out)  # blank line after each topic (spacing in terminal / notes in markdown)

        if cfg.open_in_browser:
            # only open the latest updated post in each topic
            for posts in relevant_topics:
                for post in reversed(posts):
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

    site: str | None = None
    tag: str | None = None

    async with aiohttp.ClientSession() as session:
        finder = DiscourseFinder(site)

        # Resolve triage category names → IDs
        resolved_triage_ids: set[int] = set()
        for cat_name in team_config.discourse_triage_categories:
            cat = await finder.get_category_by_name(session, cat_name.strip())
            cat_id = cat.get_id() if cat is not None else None
            if cat_id is not None:
                resolved_triage_ids.add(cat_id)
            else:
                logging.warning("Unable to find triage category: %s", cat_name)

        results: list[CategoryResult] = []
        for category_name in [c.strip() for c in team_config.discourse_categories]:
            category = await finder.get_category_by_name(session, category_name)
            if category is None:
                logging.warning("Unable to find category: %s", category_name)
                continue

            await finder.add_topics_to_category(
                session, category, ignore_before=filter.start, ignore_after=filter.end, site=site
            )

            # Fetch all topic posts concurrently
            topics = [t for t in category.get_topics() if tag is None or t.has_tag(tag)]
            await asyncio.gather(*[finder.add_posts_to_topic(session, t) for t in topics])

            results.append(CategoryResult(category_name, category, site))

        return DiscourseTriage(
            finder=finder,
            filter=filter,
            site=site,
            triage_category_ids=resolved_triage_ids,
            results=results,
        )
