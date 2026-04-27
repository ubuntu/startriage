"""Discourse data models: post, topic, category."""

from __future__ import annotations

import re
from datetime import datetime


class DiscoursePost:
    """A single Discourse post extracted from the API JSON."""

    def __init__(self, post_json: dict) -> None:
        self._id = post_json.get("id")
        self._author_username = post_json.get("username")
        self._author_name = post_json.get("name")
        self._post_number = post_json.get("post_number")
        self._post_type: int = post_json.get("post_type", 1)
        self._data = post_json.get("raw")
        self._cooked = post_json.get("cooked")
        self._num_replies = post_json.get("reply_count")
        self._reply_to_number = post_json.get("reply_to_post_number")
        self._created_at = self._parse_dt(post_json.get("created_at"))
        self._updated_at = self._parse_dt(post_json.get("updated_at"))

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (OSError, ValueError):
            return None

    def __str__(self) -> str:
        return "Invalid Post" if self._id is None else f"Post #{self._id}"

    def get_id(self) -> int | None:
        return self._id

    def get_author_username(self) -> str | None:
        return self._author_username

    def get_author_name(self) -> str | None:
        return self._author_name

    def get_creation_time(self) -> datetime | None:
        return self._created_at

    def get_update_time(self) -> datetime | None:
        return self._updated_at

    def get_post_number(self) -> int | None:
        return self._post_number

    def get_data(self) -> str | None:
        if self._data is not None:
            return self._data
        if self._cooked:
            # Strip HTML tags and collapse whitespace for a plain-text preview
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self._cooked)).strip() or None
        return None

    def get_num_replies(self) -> int | None:
        return self._num_replies

    def get_reply_to_number(self) -> int | None:
        return self._reply_to_number

    def is_main_post_for_topic(self) -> bool:
        return self._post_number == 1

    def is_small_action(self) -> bool:
        """Return True for automated system posts (post_type=3) with no user content."""
        return self._post_type == 3


class DiscourseTopic:
    """A Discourse topic (thread) with its posts."""

    def __init__(self, topic_json: dict) -> None:
        self._id = topic_json.get("id")
        self._name = topic_json.get("title")
        self._slug = topic_json.get("slug")
        self._category_id: int | None = topic_json.get("category_id")
        self._pinned = topic_json.get("pinned", False)
        self._tags: list[str] = [str(t) for t in topic_json.get("tags", [])]
        self._posts: list[DiscoursePost] = []
        self._latest_update_time: datetime | None = None

        try:
            if topic_json.get("bumped") and "bumped_at" in topic_json:
                self._latest_update_time = datetime.fromisoformat(
                    topic_json["bumped_at"].replace("Z", "+00:00")
                )
            if "last_posted_at" in topic_json:
                last = datetime.fromisoformat(topic_json["last_posted_at"].replace("Z", "+00:00"))
                if self._latest_update_time is None or last > self._latest_update_time:
                    self._latest_update_time = last
        except (OSError, ValueError):
            pass

    def __str__(self) -> str:
        if self._id is None or self._name is None:
            return "Invalid Topic"
        return f"Topic: {self._name}"

    def get_id(self) -> int | None:
        return self._id

    def get_category_id(self) -> int | None:
        return self._category_id

    def get_name(self) -> str | None:
        return self._name

    def get_slug(self) -> str | None:
        return self._slug

    def get_pinned(self) -> bool:
        return self._pinned

    def get_latest_update_time(self) -> datetime | None:
        return self._latest_update_time

    def get_tags(self) -> list[str]:
        return self._tags

    def has_tag(self, tag_name: str) -> bool:
        return tag_name in self._tags

    def add_post(self, post: DiscoursePost) -> None:
        if not isinstance(post, DiscoursePost):
            raise TypeError(f"Expected DiscoursePost, got {type(post)}")
        self._posts.append(post)

    def get_posts(self) -> list[DiscoursePost]:
        return self._posts


class DiscourseCategory:
    """A Discourse category with its topics and subcategories."""

    def __init__(self, category_json: dict) -> None:
        self._id = category_json.get("id")
        self._name = category_json.get("name")
        self._slug = category_json.get("slug")
        self._description = category_json.get("description_text")
        self._topics: list[DiscourseTopic] = []
        self._subcategories: list[DiscourseCategory] = []

        for sub in category_json.get("subcategory_list", []):
            self._subcategories.append(DiscourseCategory(sub))

    def __str__(self) -> str:
        if self._id is None or self._name is None:
            return "Invalid Category"
        return f"Category: {self._name}"

    def get_id(self) -> int | None:
        return self._id

    def get_name(self) -> str | None:
        return self._name

    def get_slug(self) -> str | None:
        return self._slug

    def get_description(self) -> str | None:
        return self._description

    def add_topic(self, topic: DiscourseTopic) -> None:
        if not isinstance(topic, DiscourseTopic):
            raise TypeError(f"Expected DiscourseTopic, got {type(topic)}")
        self._topics.append(topic)

    def get_topics(self) -> list[DiscourseTopic]:
        return self._topics

    def add_subcategory(self, subcategory: DiscourseCategory) -> None:
        if not isinstance(subcategory, DiscourseCategory):
            raise TypeError(f"Expected DiscourseCategory, got {type(subcategory)}")
        self._subcategories.append(subcategory)

    def get_subcategories(self) -> list[DiscourseCategory]:
        return self._subcategories

    def get_subcategory_by_id(self, subcategory_id: int) -> DiscourseCategory | None:
        for sub in self._subcategories:
            if sub.get_id() == subcategory_id:
                return sub
        return None

    def get_subcategory_by_name(self, name: str) -> DiscourseCategory | None:
        name_lower = name.lower()
        for sub in self._subcategories:
            if (n := sub.get_name()) and n.lower() == name_lower:
                return sub
            if (s := sub.get_slug()) and s.lower() == name_lower:
                return sub
        return None
