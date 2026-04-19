"""Tests for discourse models and finder helpers"""

from __future__ import annotations

import datetime
import json

import pytest

from startriage.sources.discourse.models import DiscourseCategory, DiscoursePost, DiscourseTopic

EXAMPLE_USER_STRING = (
    '{"id":4592175,"name":"User Name","username":"username1",'
    '"avatar_template":"/user_avatar/discourse.ubuntu.com/username1/{size}/103124_2.png",'
    '"created_at":"2022-05-16T13:59:43.661Z","cooked":"\u003cp\u003e\u003ca Test \u003e","post_number":2,'
    '"post_type":1,"updated_at":"2022-05-19T15:32:33.361Z","reply_count":1,"reply_to_post_number":1,'
    '"quote_count":0,"incoming_link_count":3,"reads":33,"readers_count":32,"score":26.6,"yours":false,'
    '"topic_id":11522,"topic_slug":"test-slug","display_username":"","primary_group_name":null,'
    '"primary_group_flair_url":null,"primary_group_flair_bg_color":null,"primary_group_flair_color":null,'
    '"version":1,"can_edit":true,"can_delete":false,"can_recover":false,"can_wiki":true,"read":true,'
    '"user_title":null,"bookmarked":false,"actions_summary":[{"id":2,"can_act":true}],'
    '"moderator":false,"admin":false,"staff":false,"user_id":11234234231,"hidden":false,"trust_level":0,'
    '"deleted_at":null,"user_deleted":false,"edit_reason":null,"can_view_edit_history":true,"wiki":false,'
    '"notice":{"type":"new_user"},"can_accept_answer":true,"can_unaccept_answer":false,"accepted_answer":false}'
)

EXAMPLE_TOPIC_STRING = (
    '{"id":11522,"title":"Virtualization - libvirt","fancy_title":"Virtualization - libvirt",'
    '"slug":"virtualization-libvirt","posts_count":10,"reply_count":5,"highest_post_number":10,"image_url":null,'
    '"created_at":"2019-06-24T11:20:59.936Z","last_posted_at":"2022-06-13T17:56:31.210Z","bumped":true,'
    '"bumped_at":"2022-06-13T17:56:31.210Z","archetype":"regular","unseen":false,"last_read_post_number":2,'
    '"unread":0,"new_posts":0,"pinned":false,"unpinned":null,"visible":true,"closed":false,"archived":false,'
    '"notification_level":1,"bookmarked":false,"liked":false,"tags":[],"views":10466,"like_count":1,'
    '"has_summary":false,"last_poster_username":"chxsec","category_id":26,"pinned_globally":false,'
    '"featured_link":null,"has_accepted_answer":false,"posters":[]}'
)

EXAMPLE_TOPIC_STRING_WITH_TAGS = (
    '{"id":10648,"title":"Charmed MongoDB K8S - Reference: Requirements",'
    '"fancy_title":"Charmed MongoDB K8S - Requirements","slug":"charmed-mongodb-k8s-reference-requirements",'
    '"posts_count":1,"reply_count":0,"highest_post_number":1,"image_url":null,"created_at":"2023-05-17T08:06:53.046Z",'
    '"last_posted_at":"2023-05-17T08:06:53.178Z","bumped":true,"bumped_at":"2023-05-25T10:13:07.753Z",'
    '"archetype":"regular","unseen":false,"pinned":false,"unpinned":null,"visible":true,"closed":false,'
    '"archived":false,"bookmarked":null,"liked":null,"tags":["k8s","mongodb","doc","charmed-mongodb"],"views":56,'
    '"like_count":0,"has_summary":false,"last_poster_username":"dratushnyy","category_id":41,"pinned_globally":false,'
    '"featured_link":null,"has_accepted_answer":false,"posters":[]}'
)

EXAMPLE_CATEGORY_STRING = (
    '{"id":17,"name":"Server","color":"0E76BD","text_color":"FFFFFF","slug":"server","topic_count":156,'
    '"post_count":1068,"position":23,"description":"Discuss Ubuntu Server.","description_text":"A place '
    'to discuss Ubuntu Server.","description_excerpt":"A place to discuss Ubuntu Server.",'
    '"topic_url":"/t/about-the-server-category/738","read_restricted":false,"permission":1,"notification_level":1,'
    '"topic_template":"","has_children":true,"sort_order":"","sort_ascending":null,"show_subcategory_list":false,'
    '"num_featured_topics":3,"default_view":"latest","subcategory_list_style":"rows_with_featured_topics",'
    '"default_top_period":"all","default_list_filter":"all","minimum_required_tags":0,'
    '"navigate_to_first_post_after_read":false,"topics_day":0,"topics_week":0,"topics_month":1,"topics_year":42,'
    '"topics_all_time":318,"subcategory_ids":[26,54],"uploaded_logo":null,"uploaded_background":null}'
)

EXAMPLE_SUBCATEGORY_SET_STRING = (
    '{"id":6,"name":"General Discussions","color":"92278F","text_color":"FFFFFF","slug":"general",'
    '"topic_count":4253,"post_count":11038,"position":1,"description":"Got something to say?",'
    '"description_text":"Got something to say about Kubernetes?",'
    '"topic_url":"/t/about-the-general-discussions-category/18","read_restricted":false,"permission":null,'
    '"notification_level":1,"has_children":true,"sort_order":"","sort_ascending":null,"show_subcategory_list":false,'
    '"num_featured_topics":3,"default_view":"latest","subcategory_list_style":"rows_with_featured_topics",'
    '"default_top_period":"quarterly","default_list_filter":"all","minimum_required_tags":0,'
    '"navigate_to_first_post_after_read":false,"topics_day":3,"topics_week":14,"topics_month":67,"topics_year":965,'
    '"topics_all_time":4884,"subcategory_ids":[22,26],"uploaded_logo":null,"uploaded_logo_dark":null,'
    '"uploaded_background":null,"subcategory_list":['
    '{"id":22,"name":"Windows","color":"0078d4","text_color":"FFFFFF","slug":"windows","topic_count":77,'
    '"post_count":197,"position":19,"description":"Welcome to the Windows containers in Kubernetes.",'
    '"description_text":"Welcome to the Windows containers in Kubernetes discussion.",'
    '"description_excerpt":"Welcome to the Windows containers in Kubernetes discussion.",'
    '"topic_url":"/t/about-the-windows-category/5633","read_restricted":false,"permission":null,"parent_category_id":6,'
    '"notification_level":1,"topic_template":"","has_children":false,"sort_order":"","sort_ascending":null,'
    '"show_subcategory_list":false,"num_featured_topics":3,"default_view":"",'
    '"subcategory_list_style":"rows_with_featured_topics","default_top_period":"all","default_list_filter":"all",'
    '"minimum_required_tags":0,"navigate_to_first_post_after_read":false,"topics_day":0,"topics_week":0,'
    '"topics_month":1,"topics_year":16,"topics_all_time":77,"subcategory_ids":[],"uploaded_logo":null,'
    '"uploaded_logo_dark":null,"uploaded_background":null},'
    '{"id":26,"name":"microk8s","color":"E95420","text_color":"FFFFFF","slug":"microk8s","topic_count":554,'
    '"post_count":1947,"position":23,'
    '"description":"<strong>MicroK8s</strong> is a low-ops, minimal production Kubernetes.",'
    '"description_text":"MicroK8s is a low-ops, minimal production Kubernetes.",'
    '"description_excerpt":"MicroK8s is a low-ops, minimal production Kubernetes.",'
    '"topic_url":"/t/microk8s-documentation-home/11243","read_restricted":false,"permission":null,'
    '"parent_category_id":6,"notification_level":1,"topic_template":"",'
    '"has_children":false,"sort_order":"","sort_ascending":null,"show_subcategory_list":false,"num_featured_topics":3,'
    '"default_view":"latest","subcategory_list_style":"rows_with_featured_topics","default_top_period":"yearly",'
    '"default_list_filter":"all","minimum_required_tags":0,"navigate_to_first_post_after_read":false,"topics_day":0,'
    '"topics_week":4,"topics_month":8,"topics_year":150,"topics_all_time":554,"subcategory_ids":[],'
    '"uploaded_logo":null,"uploaded_logo_dark":null,"uploaded_background":null}]}'
)


@pytest.mark.parametrize(
    "post_id,name,username,data,post_number,created,updated,rep_cnt,rep_to,post_string",
    [
        (
            4592175,
            "User Name",
            "username1",
            None,
            2,
            datetime.datetime(2022, 5, 16, 13, 59, 43, 661000, tzinfo=datetime.timezone.utc),
            datetime.datetime(2022, 5, 19, 15, 32, 33, 361000, tzinfo=datetime.timezone.utc),
            1,
            1,
            EXAMPLE_USER_STRING,
        ),
        (None, None, None, None, None, None, None, None, None, "{}"),
        (
            "",
            "",
            "",
            "",
            None,
            None,
            None,
            None,
            None,
            '{"id":"","name":"","username":"","raw":"","updated_at":"","created_at":"","reply_to_post_number":null}',
        ),
    ],
)
def test_create_post_from_json(
    post_id, name, username, data, post_number, created, updated, rep_cnt, rep_to, post_string
):
    post = DiscoursePost(json.loads(post_string))
    assert post.get_id() == post_id
    assert post.get_author_name() == name
    assert post.get_author_username() == username
    assert post.get_data() == data
    assert post.get_creation_time() == created
    assert post.get_update_time() == updated
    assert post.get_post_number() == post_number
    assert post.get_num_replies() == rep_cnt
    assert post.get_reply_to_number() == rep_to
    if post_id is None:
        assert str(post) == "Invalid Post"
    else:
        assert str(post.get_id()) in str(post)


@pytest.mark.parametrize(
    "topic_id,name,slug,update_time,tags,topic_string",
    [
        (
            11522,
            "Virtualization - libvirt",
            "virtualization-libvirt",
            datetime.datetime(2022, 6, 13, 17, 56, 31, 210000, tzinfo=datetime.timezone.utc),
            [],
            EXAMPLE_TOPIC_STRING,
        ),
        (
            10648,
            "Charmed MongoDB K8S - Reference: Requirements",
            "charmed-mongodb-k8s-reference-requirements",
            datetime.datetime(2023, 5, 25, 10, 13, 7, 753000, tzinfo=datetime.timezone.utc),
            ["k8s", "mongodb", "doc", "charmed-mongodb"],
            EXAMPLE_TOPIC_STRING_WITH_TAGS,
        ),
        (None, None, None, None, [], "{}"),
        ("", "", "", None, [], '{"id":"","title":"","slug":"","last_posted_at":"","tags":[]}'),
    ],
)
def test_create_topic_from_json(topic_id, name, slug, update_time, tags, topic_string):
    topic = DiscourseTopic(json.loads(topic_string))
    assert topic.get_id() == topic_id
    assert topic.get_name() == name
    assert topic.get_slug() == slug
    assert topic.get_latest_update_time() == update_time
    for tag in topic.get_tags():
        assert tag in tags
    if topic_id is None or name is None:
        assert str(topic) == "Invalid Topic"
    else:
        topic_name = topic.get_name()
        assert topic_name is not None
        assert topic_name in str(topic)


def test_add_posts_to_topic():
    topic = DiscourseTopic(json.loads('{"id":"12345","title":"Test Topic","slug":"test-topic"}'))
    assert len(topic.get_posts()) == 0
    post_1 = DiscoursePost(
        json.loads('{"id":1,"name":"User","username":"","raw":"","updated_at":"","created_at":""}')
    )
    topic.add_post(post_1)
    assert len(topic.get_posts()) == 1
    post_2 = DiscoursePost(
        json.loads('{"id":2,"name":"User","username":"","raw":"","updated_at":"","created_at":""}')
    )
    topic.add_post(post_2)
    assert len(topic.get_posts()) == 2
    with pytest.raises(TypeError):
        topic.add_post(None)  # ty:ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        topic.add_post(123)  # ty:ignore[invalid-argument-type]
    assert len(topic.get_posts()) == 2


@pytest.mark.parametrize(
    "category_id,name,description,category_string",
    [
        (17, "Server", "A place to discuss Ubuntu Server.", EXAMPLE_CATEGORY_STRING),
        (None, None, None, "{}"),
        ("", "", "", '{"id":"","name":"","description_text":""}'),
    ],
)
def test_create_category_from_json(category_id, name, description, category_string):
    cat = DiscourseCategory(json.loads(category_string))
    assert cat.get_id() == category_id
    assert cat.get_name() == name
    assert cat.get_description() == description
    if name is None or category_id is None:
        assert str(cat) == "Invalid Category"
    else:
        assert name in str(cat)


@pytest.mark.parametrize(
    "subcategory_id,name,description,category_string",
    [
        (
            22,
            "Windows",
            "Welcome to the Windows containers in Kubernetes discussion.",
            EXAMPLE_SUBCATEGORY_SET_STRING,
        ),
        (None, None, None, '{"id":1,"subcategory_list":[{}]}'),
        (
            "",
            "",
            "",
            '{"id":1,"name":"Test","description_text":"","subcategory_list":[{"id":"","name":"","description_text":""}]}',
        ),
    ],
)
def test_create_subcategory_from_json(subcategory_id, name, description, category_string):
    cat = DiscourseCategory(json.loads(category_string))
    sub = cat.get_subcategories()[0]
    assert sub.get_id() == subcategory_id
    assert sub.get_name() == name
    assert sub.get_description() == description


@pytest.mark.parametrize(
    "subcategory_id,name,description",
    [
        (22, "Windows", "Welcome to the Windows containers in Kubernetes discussion."),
        (26, "microk8s", "MicroK8s is a low-ops, minimal production Kubernetes."),
        (28, None, None),
    ],
)
def test_get_subcategory_by_id(subcategory_id, name, description):
    cat = DiscourseCategory(json.loads(EXAMPLE_SUBCATEGORY_SET_STRING))
    sub = cat.get_subcategory_by_id(subcategory_id)
    if name is None:
        assert sub is None
    else:
        assert sub is not None and sub.get_name() == name
        assert sub.get_description() == description


@pytest.mark.parametrize(
    "subcategory_id,name,description",
    [
        (22, "Windows", "Welcome to the Windows containers in Kubernetes discussion."),
        (26, "microk8s", "MicroK8s is a low-ops, minimal production Kubernetes."),
        (None, "test fail", None),
    ],
)
def test_get_subcategory_by_name(subcategory_id, name, description):
    cat = DiscourseCategory(json.loads(EXAMPLE_SUBCATEGORY_SET_STRING))
    sub = cat.get_subcategory_by_name(name)
    if subcategory_id is None:
        assert sub is None
    else:
        assert sub is not None and sub.get_id() == subcategory_id
        assert sub.get_description() == description


def test_add_topics_to_category():
    cat = DiscourseCategory(json.loads('{"id":"45678","name":"Test","description_text":"A test category."}'))
    assert len(cat.get_topics()) == 0
    topic_1 = DiscourseTopic(json.loads('{"id":"10","title":"Test Topic 1","slug":"test-topic-1"}'))
    cat.add_topic(topic_1)
    assert len(cat.get_topics()) == 1
    topic_2 = DiscourseTopic(json.loads('{"id":"11","title":"Test Topic 2","slug":"test-topic-2"}'))
    cat.add_topic(topic_2)
    assert len(cat.get_topics()) == 2
    with pytest.raises(TypeError):
        cat.add_topic(None)  # ty:ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        cat.add_topic(123)  # ty:ignore[invalid-argument-type]
    assert len(cat.get_topics()) == 2
