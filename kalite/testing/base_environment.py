"""
environment.py defines setup and teardown behaviors for behave tests.
The behavior in this file is appropriate for integration tests, and
could be used to bootstrap other integration tests in our project.
"""
import logging
import os
import tempfile
import shutil
import sauceclient as sc
import socket

from ssl import SSLError
from httplib import CannotSendRequest
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from shutil import WindowsError
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connections
from django.db.transaction import TransactionManagementError
from peewee import Using

from kalite.i18n.base import get_subtitle_file_path, get_subtitle_url
from kalite.testing.base import KALiteTestCase
from kalite.testing.behave_helpers import login_as_admin, login_as_coach, logout, login_as_learner
from kalite.topic_tools.content_models import Item, set_database, create, get, \
    delete_instances

from securesync.models import Zone, Device, DeviceZone
import random

logger = logging.getLogger(__name__)


def before_all(context):
    setup_content_paths(context)


def after_all(context):
    teardown_content_paths(context)


def before_feature(context, feature):
    pass
    # if "uses_content_paths" in context.tags:
    #     setup_content_paths(context)


def after_feature(context, feature):
    pass
    # if "uses_content_paths" in context.tags:
    #     teardown_content_paths(context)


@set_database
def setup_content_paths(context, db):
    """
    Creaters available content items and adds their urls to the context object.

    :param context: A behave context, to which the attributes "available_content_path" and "unavailable_content_path"
        will be added.
    :return: None
    """

    context._exercises = []
    context._subtopics = []
    context._subsubtopics = []
    context.videos = []

    with Using(db, [Item], with_transaction=False):
        # Root node
        context._content_root = Item.create(
            title="Khan Academy",
            description="",
            available=True,
            files_complete=0,
            total_files="1",
            kind="Topic",
            parent=None,
            id="khan",
            slug="khan",
            path="khan/",
            extra_fields="{}",
            youtube_id=None,
            size=0,
            remote_size=315846064333,
            sort_order=0
        )
        for _i in range(4):
            slug = "topic{}".format(_i)
            context._subtopics.append(
                Item.create(
                    title="Subtopic {}".format(_i),
                    description="A subtopic",
                    available=True,
                    files_complete=0,
                    total_files="4",
                    kind="Topic",
                    parent=context._content_root,
                    id=slug,
                    slug=slug,
                    path="khan/{}/".format(slug),
                    extra_fields="{}",
                    size=0,
                    remote_size=1,
                    sort_order=_i,
                )
            )
        
        # Parts of the content recommendation system currently is hard-coded
        # to look for 3rd level recommendations only and so will fail if we
        # don't have this level of lookup
        for subtopic in context._subtopics:
            for _i in range(4):
                slug = "{}-{}".format(subtopic.id, _i)
                context._subsubtopics.append(
                    Item.create(
                        title="{} Subtopic {}".format(subtopic.title, _i),
                        description="A subsubtopic",
                        available=True,
                        files_complete=4,
                        total_files="4",
                        kind="Topic",
                        parent=subtopic,
                        id=slug,
                        slug=slug,
                        path="{}{}/".format(subtopic.path, slug),
                        youtube_id=None,
                        extra_fields="{}",
                        size=0,
                        remote_size=1,
                        sort_order=_i,
                    )
                )

        # We need at least 10 exercises in some of the tests to generate enough
        # data etc.
        # ...and we need at least some exercises in each sub-subtopic
        for parent in context._subsubtopics:
            for _i in range(4):
                slug = "{}-exercise-{}".format(parent.id, _i)
                context._exercises.append(
                    Item.create(
                        title="Exercise {} in {}".format(_i, parent.title),
                        parent=parent,
                        description="Solve this",
                        available=True,
                        kind="Exercise",
                        id=slug,
                        slug=slug,
                        path="{}{}/".format(parent.path, slug),
                        extra_fields="{}",
                        sort_order=_i
                    )
                )
        # Add some videos, too, even though files don't exist
        for parent in context._subsubtopics:
            for _i in range(4):
                slug = "{}-video-{}".format(parent.pk, _i)
                context.videos.append(
                    Item.create(
                        title="Video {} in {}".format(_i, parent.title),
                        parent=random.choice(context._subsubtopics),
                        description="Watch this",
                        available=True,
                        kind="Video",
                        id=slug,
                        slug=slug,
                        path="{}{}/".format(parent.path, slug),
                        extra_fields={
                            "subtitle_urls": [],
                            "content_urls": {"stream": "/foo", "stream_type": "video/mp4"},
                        },
                        sort_order=_i
                    )
                )
    context.available_content_path = random.choice(context._exercises).path

    # These paths are "magic" -- the success or failure of actually visiting the content items in the browser
    # depends on these specific values.
    context.unavailable_content_path = "khan/foo/bar/unavail"

    context.searchable_term = "Subtopic"

    with Using(db, [Item], with_transaction=False):
        context._unavailable_item = Item.create(
            title="Unavailable item",
            description="baz",
            available=False,
            kind="Video",
            id="unavail123",
            slug="unavail",
            path=context.unavailable_content_path,
            parent=random.choice(context._subsubtopics).pk,
        )


@set_database
def teardown_content_paths(context, db):
    """
    The opposite of ``setup_content_urls``. Removes content items created there.

    :param context: A behave context, which keeps a reference to the Items so we can clean them up.
    :return: None.
    """
    with Using(db, [Item], with_transaction=False):
        context._unavailable_item.delete_instance()
        context._content_root.delete_instance()
        for item in (context._exercises +
                     context.videos +
                     context._subsubtopics +
                     context._subtopics):
            item.delete_instance()


def setup_sauce_browser(context):
    """
    Use saucelabs remote webdriver. Has side effects on the passed in behave context.

    :param context: the behave context
    :return: none, but has side effects. Adds properties "sauce" and "browser" to context.
    """
    # based on http://saucelabs.com/examples/example.py
    username = os.environ.get('SAUCE_USERNAME')
    access_key = os.environ.get('SAUCE_ACCESS_KEY')
    circle_build = os.environ.get('CIRCLE_BUILD_NUM')
    circle_node = os.environ.get('CIRCLE_NODE_INDEX')
    
    tunnel_id = "{build}-{node}".format(build=circle_build, node=circle_node)
    context.sauce = sc.SauceClient(username, access_key)
    sauce_url = "http://{username}:{access_key}@ondemand.saucelabs.com:80/wd/hub".format(username=username,
                                                                                         access_key=access_key)

    profile = webdriver.FirefoxProfile()
    if "download_csv" in context.tags:
        # Let csv files be downloaded automatically. Can be accessed using context.download_dir
        context.download_dir = tempfile.mkdtemp()
        profile.set_preference("browser.download.folderList", 2)
        profile.set_preference("browser.download.manager.showWhenStarting", False)
        profile.set_preference("browser.download.dir", context.download_dir)
        profile.set_preference("browser.helperApps.neverAsk.saveToDisk", "text/csv")
        context.browser = webdriver.Firefox(firefox_profile=profile)  # Use local browser for this particular test
    else:
        desired_capabilities = DesiredCapabilities.FIREFOX.copy()
        desired_capabilities["tunnelIdentifier"] = tunnel_id
        try:
            context.browser = webdriver.Remote(desired_capabilities=desired_capabilities,
                                               browser_profile=profile,
                                               command_executor=sauce_url)
        except (WebDriverException, socket.timeout):  # socket.timeout thrown occasionally, Selenium doesn't handle it
            print("Couldn't establish a connection to saucelabs. Using a local Firefox WebDriver instance.")
            del context.sauce
            context.browser = webdriver.Firefox(firefox_profile=profile)


def setup_local_browser(context):
    """
    Use local webdriver. Has side effects on the passed in behave context.

    :param context: the behave context
    :return: none, but has side effects. Adds property "browser" to context.
    """

    profile = webdriver.FirefoxProfile()
    if "download_csv" in context.tags:
        # Let csv files be downloaded automatically. Can be accessed using context.download_dir
        context.download_dir = tempfile.mkdtemp()
        profile.set_preference("browser.download.folderList", 2)
        profile.set_preference("browser.download.manager.showWhenStarting", False)
        profile.set_preference("browser.download.dir", context.download_dir)
        profile.set_preference("browser.helperApps.neverAsk.saveToDisk", "text/csv")

    context.browser = webdriver.Firefox(firefox_profile=profile)


# FYI: context.tags contains feature tags + scenario tags.
def before_scenario(context, scenario):
    database_setup(context)

    if "uses_video_with_subtitles" in context.tags:
        _make_video(context)

    if "registered_device" in context.tags:
        do_fake_registration()

    if os.environ.get("TRAVIS", False):  # Indicates we're running on remote build server
        setup_sauce_browser(context)
    else:
        setup_local_browser(context)

    context.logged_in = False
    # A superuser now needs to exist or UI is blocked by a modal.
    # https://github.com/learningequality/ka-lite/pull/3668
    if not User.objects.filter(is_superuser=True).exists():
        assert User.objects.create_superuser(
            username='superusername',
            password='superpassword',
            email='super@email.com'
        ), "Didn't create an admin user"
    if "as_admin" in context.tags:
        context.logged_in = True
        login_as_admin(context)
    elif "as_coach" in context.tags:
        context.logged_in = True
        login_as_coach(context)
    elif "as_learner" in context.tags:
        context.logged_in = True
        login_as_learner(context)


def after_scenario(context, scenario):
    if "uses_video_with_subtitles" in context.tags:
        _teardown_video(context)

    if context.logged_in:
        logout(context)

    if "download_csv" in context.tags:
        shutil.rmtree(context.download_dir)

    try:
        if hasattr(context, "sauce"):
            print("Link to your job: https://saucelabs.com/jobs/%s" % context.browser.session_id)
            if context.scenario.status == "failed":
                context.sauce.jobs.update_job(context.browser.session_id, passed=False)
            else:
                context.sauce.jobs.update_job(context.browser.session_id, passed=True)
    except SSLError as e:
        print("SSL error: Couldn't log the job... Error message:\n" + e.message)
    except Exception as e:
        if "404" in e.message:
            print("Couldn't log the job... Error message:\n" + e.message)
        else:
            raise
    finally:
        try:
            # Don't shut down the browser until all AJAX requests have completed.
            while context.browser.execute_script("return (window.jQuery || { active : 0 }).active"):
                pass
            context.browser.quit()
        except CannotSendRequest:
            pass

    database_teardown(context)


def database_setup(context):
    """
    Behave features are analogous to test suites, and behave scenarios are analogous to TestCases, but due to
    implementation details each _feature_ is wrapped in a TestCase. This and database_teardown should simulate the
    setup/teardown done by TestCases in order to achieve consistent isolation.
    """
    KALiteTestCase.setUpDatabase()


def database_teardown(context):
    """
    Behave features are analogous to test suites, and behave scenarios are analogous to TestCases, but due to
    implementation details each _feature_ is wrapped in a TestCase. This and database_setup should simulate the
    setup/teardown done by TestCases in order to achieve consistent isolation.
    """
    for alias in connections:
        try:
            call_command("flush", database=alias, interactive=False)
        except TransactionManagementError as e:
            print("Couldn't flush the database, got a TransactionManagementError: " + e.message)


def do_fake_registration():
    """
    Register the device, in case some feature being tested depends on it. Will be undone by the database teardown.
    """
    # Create a Zone and DeviceZone to fool the Device into thinking it's registered
    zone = Zone(name="The Danger Zone", description="Welcome to it.")
    zone.save()
    device = Device.get_own_device()
    device_zone = DeviceZone(device=device, zone=zone)
    device_zone.save()


def _make_video(context):
    root = get({"parent": None})
    lang_code = "en"
    youtube_id = "my_cool_id"
    item_dict = {
        "title": "Subtitled Video",
        "description": "A video with subtitles",
        "available": True,
        "kind": "Video",
        "id": "video_with_subtitles",
        "slug": "video_with_subtitles",
        "path": "khan/video_with_subtitles",
        "extra_fields": {
            "subtitle_urls": [{"url": get_subtitle_url(youtube_id=youtube_id, code=lang_code),
                               "code": lang_code,
                               "name": "English"}],
            "content_urls": {"stream": "/foo", "stream_type": "video/mp4"},
        },
        "parent": root,
    }
    # `create` will quietly do nothing if the item already exists. Possible from pathological test runs.
    # So delete any identical Items first.
    delete_instances(ids=[item_dict["id"]])
    context.video = create(item_dict)

    subtitle_path = get_subtitle_file_path(lang_code=lang_code, youtube_id=youtube_id)
    subtitle_dir = os.path.dirname(subtitle_path)
    if not os.path.exists(subtitle_dir):
        os.makedirs(subtitle_dir)
    with open(subtitle_path, "w") as f:
        f.write("foo")
    context._subtitle_file_path = subtitle_path


def _teardown_video(context):
    delete_instances([context.video.id])
    try:
        os.remove(context._subtitle_file_path)
    except WindowsError as e:
        print("Couldn't remove temporary subtitle file {}. Exception:\n\t{}".format(
            context._subtitle_file_path,
            str(e))
        )
