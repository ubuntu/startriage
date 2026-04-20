"""Discourse triage result: holds fetched data and renders output."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

import aiohttp

from startriage.output import OutputFormat, hyperlink

from . import finder as f
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


@dataclass
class CategoryResult:
    category_name: str
    category: DiscourseCategory
    site: str | None


@dataclass
class DiscourseTriage:
    """Holds all fetched Discourse results for one triage run."""

    results: list[CategoryResult] = field(default_factory=list)
    start: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    site: str | None = None
    had_updates: bool = False
    triage_category_id: int | None = None

    async def print_section(
        self,
        fmt: OutputFormat = OutputFormat.TERMINAL,
        open_in_browser: bool = False,
        shorten_links: bool = True,
        out=None,
    ) -> None:
        """Print the # Forum section to stdout (and optionally to a markdown file)."""
        if out is None:
            out = sys.stdout
        _print = lambda s="": print(s, file=out)  # noqa: E731

        _print("\n# Forum\n")
        site_info = f" on {self.site}" if self.site else ""
        logging.info("Showing forum comments%s", site_info)

        for result in self.results:
            logging.info("Comments belonging to the %s category:", result.category_name)
            _print_category_comments(
                result.category,
                self.start,
                self.end,
                open_in_browser,
                shorten_links,
                result.site,
                fmt,
                out,
                triage_category_id=self.triage_category_id,
            )

    async def write_markdown(self, path: str, open_in_browser: bool = False) -> None:
        """Write markdown-formatted output to a file."""

        buf = io.StringIO()
        await self.print_section(
            fmt=OutputFormat.MARKDOWN, open_in_browser=False, shorten_links=False, out=buf
        )
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(buf.getvalue())


def _print_single_comment(
    post: DiscoursePost,
    status: PostStatus,
    date_updated: datetime | None,
    post_url: str,
    shorten_links: bool,
    fmt: OutputFormat,
    out,
) -> None:
    status_str = {PostStatus.UPDATED: "*", PostStatus.NEW: "+"}.get(status, "")
    date_str = f", {date_updated.strftime('%Y-%m-%d')}" if date_updated else ""

    if fmt == OutputFormat.MARKDOWN:
        link = hyperlink(post_url, str(post.get_id()), fmt)
        author = f.author_str(post)
        print(
            f"{status_str}{link} [{author}{date_str}]",
            file=out,
        )
    else:
        if shorten_links:
            id_str = hyperlink(post_url, str(post.get_id()), fmt)
            url_str = ""
        else:
            id_str = str(post.get_id())
            url_str = f"({post_url})"
        author = f.author_str(post)
        print(f"{status_str}{id_str} [{author}{date_str}] {url_str}", file=out)


def _print_topic_header(
    topic: DiscourseTopic,
    status: PostStatus,
    date_updated: datetime | None,
    author: str | None,
    editor: str | None,
    shorten_links: bool,
    site: str | None,
    fmt: OutputFormat,
    out,
    topic_name_length: int = 50,
) -> None:
    topic_url = f.get_topic_url(topic, site)
    status_str = {PostStatus.UPDATED: "*", PostStatus.NEW: "+"}.get(status, "")
    if not status_str:
        topic_name_length += 1

    name = topic.get_name() or ""
    if len(name) > topic_name_length:
        name = name[: topic_name_length - 1] + "…"
    else:
        name = name.ljust(topic_name_length)

    if fmt == OutputFormat.MARKDOWN:
        link = hyperlink(topic_url, name.strip(), fmt)
    elif shorten_links:
        link = hyperlink(topic_url, name, fmt)
    else:
        link = name

    date_str = f", {date_updated.strftime('%Y-%m-%d')}" if date_updated else ""
    url_str = "" if (fmt == OutputFormat.MARKDOWN or shorten_links) else f"({topic_url})"
    display_author = editor if editor else author
    print(f"{status_str}{link} [{display_author}{date_str}] {url_str}", file=out)


def _print_comment_chain(
    meta: PostWithMetadata, shorten_links: bool, fmt: OutputFormat, out, chain: list[str]
) -> None:
    if not meta.contains_relevant_posts:
        return

    if chain:
        indent = chain[0] + "".join("  " + c for c in chain[1:])
        print(indent, end="─ ", file=out)

    _print_single_comment(meta.post, meta.status, meta.update_date, meta.url, shorten_links, fmt, out)

    relevant_replies = [r for r in meta.replies if r.contains_relevant_posts]
    if relevant_replies:
        if chain and chain[-1] == "├":
            chain[-1] = "│"
        elif chain and chain[-1] == "└":
            chain[-1] = " "
        chain.append("├")
        for reply in relevant_replies[:-1]:
            _print_comment_chain(reply, shorten_links, fmt, out, chain)
        chain[-1] = "└"
        _print_comment_chain(relevant_replies[-1], shorten_links, fmt, out, chain)
        chain.pop()


async def _get_editor(
    session: aiohttp.ClientSession | None, post: DiscoursePost, site: str | None
) -> str | None:
    if session is None:
        return None
    return await f.get_editor_name(session, post, site)


def _print_category_comments(
    category: DiscourseCategory,
    start: datetime,
    end: datetime,
    open_in_browser: bool,
    shorten_links: bool,
    site: str | None,
    fmt: OutputFormat,
    out,
    triage_category_id: int | None = None,
) -> None:
    initial_open = True
    for topic in category.get_topics():
        posts = topic.get_posts()
        meta_list = [
            _create_post_meta(p, start, end, f.get_post_url(topic, i, site)) for i, p in enumerate(posts)
        ]

        # Topics in the triage category: ignore main-post updates, show replies only.
        is_triage = triage_category_id is not None and topic.get_category_id() == triage_category_id
        if is_triage:
            for m in meta_list:
                if m.post.is_main_post_for_topic():
                    m.status = PostStatus.UNCHANGED

        topic_relevant = any(m.status != PostStatus.UNCHANGED for m in meta_list)
        if not topic_relevant:
            continue

        # Build reply tree
        final_list: list[PostWithMetadata] = []
        for item in meta_list:
            replied_to = next(
                (m for m in meta_list if m.post.get_post_number() == item.post.get_reply_to_number()),
                None,
            )
            if replied_to is None or replied_to.post.is_main_post_for_topic():
                final_list.append(item)
            if replied_to is not None:
                replied_to.add_reply(item)

            if item.status != PostStatus.UNCHANGED and open_in_browser:
                if initial_open:
                    initial_open = False
                    webbrowser.open(item.url)
                    time.sleep(5)
                else:
                    webbrowser.open_new_tab(item.url)
                    time.sleep(1.2)

        for item in meta_list:
            _set_relevant(item)

        # Find main post
        main_meta = next((m for m in final_list if m.post.is_main_post_for_topic()), None)

        # In markdown mode, the tree goes inside a fenced code block to preserve indentation
        if fmt == OutputFormat.MARKDOWN:
            tree_buf = io.StringIO()
            tree_out = tree_buf
        else:
            tree_out = out

        if main_meta:
            author = f.author_str(main_meta.post)
            editor = None  # editor lookup requires async; done in find()
            _print_topic_header(
                topic,
                main_meta.status,
                main_meta.update_date,
                author,
                editor,
                shorten_links,
                site,
                fmt,
                tree_out,
            )
            final_list = [m for m in final_list if m != main_meta and m.contains_relevant_posts]
        else:
            _print_topic_header(
                topic, PostStatus.UNCHANGED, None, None, None, shorten_links, site, fmt, tree_out
            )

        for item in final_list[:-1]:
            _print_comment_chain(item, shorten_links, fmt, tree_out, ["├"])
        if final_list:
            _print_comment_chain(final_list[-1], shorten_links, fmt, tree_out, ["└"])

        if fmt == OutputFormat.MARKDOWN:
            tree_text = tree_buf.getvalue().rstrip("\n")
            if tree_text:
                print(f"```\n{tree_text}\n```", file=out)
            print(file=out)  # action stub blank line


async def find(
    session: aiohttp.ClientSession,
    category_names: str,
    start: datetime,
    end: datetime,
    site: str | None = None,
    tag: str | None = None,
    triage_topic_id: int | None = None,
    triage_category_id: int | None = None,
) -> DiscourseTriage:
    """Fetch all Discourse data for the given categories and date range.

    Prints output incrementally as each category is processed.
    """
    triage = DiscourseTriage(start=start, end=end, site=site, triage_category_id=triage_category_id)

    for category_name in [c.strip() for c in category_names.split(",")]:
        category = await f.get_category_by_name(session, category_name, site)
        if category is None:
            logging.warning("Unable to find category: %s", category_name)
            continue

        await f.add_topics_to_category(session, category, start, site)

        # Fetch all topic posts concurrently
        topics = [t for t in category.get_topics() if tag is None or t.has_tag(tag)]
        await asyncio.gather(*[f.add_posts_to_topic(session, t, site) for t in topics])

        triage.results.append(CategoryResult(category_name, category, site))

    return triage
