# ![dsctriage](img/dsctriage-small.png) Discourse Triage

[![CI](https://github.com/lvoytek/discourse-triage/actions/workflows/main.yml/badge.svg)](https://github.com/lvoytek/discourse-triage/actions/workflows/main.yml)
[![dsctriage](https://snapcraft.io/dsctriage/badge.svg)](https://snapcraft.io/dsctriage)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)


Output comments from a [Discourse](https://www.discourse.org/) server for triage. This script is used by the Ubuntu
Server team to keep up with suggested fixes and issues in the [Ubuntu Server Guide](https://ubuntu.com/server/docs), 
using [Ubuntu's Discourse site](https://discourse.ubuntu.com). It can, however, also be used to look into Discourse 
posts on any Discourse server, or other sections of Ubuntu's documentation.

The easiest way to install and keep dsctriage up to date is through snap:

```bash
sudo snap install dsctriage
```

Alternatively you can download this repository and run directly with python:

```bash
python3 -m dsctriage
```

## Usage
To run Discourse Triage with the default settings, open a terminal and enter:

    dsctriage

By default, the script will find new posts in the Ubuntu Server discussion and guide categories, created or updated yesterday or over the weekend. They will then be displayed in the terminal in a tree format. Clicking on the post ID will open a given comment in a web browser. The following arguments can be used to change functionality.

### Dates
Dates must follow the format: `%Y-%m-%d` (e.g. 2019-12-17, 2020-05-26)

#### Single Date Argument
If one date is given then posts from only that day will be found. 
For example, the following finds all posts in the `Server` category from April 27th, 2022:

    dsctriage 2022-04-27

#### Two Date Arguments
If two dates are given then all the posts created and updated on those days and between (fully inclusive) will be found. 
For example, the following, finds all posts last modified on the 10th, 11th, and 12th of September 2022:

    dsctriage 2022-09-10 2022-09-12

#### Day Name
The triage day name can also be provided to automatically extract the desired date range. For example, the following command will show all relevant comments from last Monday, which represents Tuesday triage:

    dsctriage tuesday

Running triage for Monday will show comments from last Friday and the weekend:

    dsctriage mon

### Server
To use a different Discourse server/website, use the `-s` or `--site` option, along with the desired base URL. For example,
to get yesterday's posts in the `plugin` category of [Discourse's meta site](https://meta.discourse.org/), run:

    dsctriage -s https://meta.discourse.org -c plugin

or:

    dsctriage --site https://meta.discourse.org -c plugin

### Categories
If you want to find comments in a different category or set of categories (see the [Ubuntu category list](https://discourse.ubuntu.com/categories)),
then you can specify them with the `-c` or `--category` option. Discourse Triage will attempt to match each listed item
with an  existing category name or slug, case-insensitive. For example, to get comments from yesterday or over the
weekend in the `LXD` and `Multipass` categories, run either:

    dsctriage -c LXD,Multipass

or:

    dsctriage --category LXD,Multipass

The output will also show updates within the given category's subcategories. If you would like updates from only one
specific subcategory, then you can use `/` as if it were a subdirectory. For example, to check updates to `microk8s` in
the [Kubernetes Discourse](https://discuss.kubernetes.io), which is a subcategory of [General Discussions](https://discuss.kubernetes.io/c/general-discussions/6),
run the following:

    dsctriage -c 'general discussions/microk8s' -s https://discuss.kubernetes.io

Updates in the muted topics of a category will be included in the final output.

### Tag
To focus specifically on topics with a given tag in Discourse, specify the `-t` or `--tag` option. Discourse Triage will
then look through the tags of each updated topic in the category and only show those containing the one provided. For
example, the following command will show updates to topics specific to kubeflow in the `charm` category of CharmHub's
Discourse site:

    dsctriage -s https://discourse.charmhub.io/ -c charm -t kubeflow

### Print full urls
By default, post IDs can be clicked to open in a browser. However, if your terminal does not support the hyperlink
format, or you just want the urls in plaintext you can use the `--fullurls` argument. This will print the url to the
post at the end of each line. Run the following command to get the posts from yesterday or over the weekend with their urls:

    dsctriage --fullurls

### Open in web browser
If you want all posts found to be shown in a web browser, then specify the `-o` or `--open` argument. Once the posts are
found, they will then be opened in your default browser in their own tabs. Specify the argument with either:
    
    dsctriage -o

or:
    
    dsctriage --open

### Add to backlog
To add a specific post or comment to the backlog, a formatted line of text can be printed with the `-b` or `--backlog`
argument. This text can then be copied to the backlog to be managed later. The following commands will show the post
with the ID 14159:

    dsctriage -b 14159

or:

    dsctriage --backlog 14159

### Set default category and server
To update the Discourse server and category used by default, add the `--set-defaults` argument during a dsctriage run
against them. Future runs will no longer need them to be specified each time. For example, the following will run
dsctriage and set the default category to `announcements` in the Discourse meta site: 

    dsctriage -s https://meta.discourse.org -c announcements --set-defaults

## Configuration
Alongside the `--set-defaults` argument shown above, Discourse Triage can be configured the `dsctriage.conf` file. This
can be edited in `~/snap/dsctriage/current/.config/` when using the snap, or `~/.config` when running with `python3 -m`.

### Usage
The `dsctriage.conf` file works as a standard config file, where options are set in the `[dsctriage]` section using a
`=`. Here is an example of a valid `dsctriage.conf`:

    [dsctriage]
    category = doc
    site = https://forum.snapcraft.io
    progress_bar = True
    shorten_links = True

### Options
The following options can be modified in the config file:

* `category`
    - The Discourse category to look at, initially defaults to `Server`
* `site`
    - The Discourse site URL to look at, initially defaults to `https://discourse.ubuntu.com`
* `progress_bar`
    - Whether to show the progress bar when running dsctriage, defaults to `True`
* `shorten_links`
    - Whether to show links as hyperlinks in the post number, or print them fully. Defaults to `True`, making them
    hyperlinks.


# Ubuntu Server Triage

[![Continuous Integration](https://github.com/canonical/ubuntu-server-triage/actions/workflows/ci.yaml/badge.svg)](https://github.com/canonical/ubuntu-server-triage/actions/workflows/ci.yaml)
[![ustriage](https://snapcraft.io/ustriage/badge.svg)](https://snapcraft.io/ustriage)

Output Ubuntu Server Launchpad bugs that for triage. The script is used by members of the Ubuntu Server team to determine what Launchpad bugs to review on a particular day or range of days. Giving us programmatic access to a set of bugs to look at. The older method was to look at [this page](https://bugs.launchpad.net/ubuntu/?field.searchtext=&orderby=-date_last_updated&search=Search&field.status%3Alist=NEW&field.status%3Alist=CONFIRMED&field.status%3Alist=TRIAGED&field.status%3Alist=INPROGRESS&field.status%3Alist=FIXCOMMITTED&field.status%3Alist=INCOMPLETE_WITH_RESPONSE&field.status%3Alist=INCOMPLETE_WITHOUT_RESPONSE&assignee_option=any&field.assignee=&field.bug_reporter=&field.bug_commenter=&field.subscriber=&field.structural_subscriber=ubuntu-server&field.component-empty-marker=1&field.tag=&field.tags_combinator=ANY&field.status_upstream-empty-marker=1&field.has_cve.used=&field.omit_dupes.used=&field.omit_dupes=on&field.affects_me.used=&field.has_no_package.used=&field.has_patch.used=&field.has_branches.used=&field.has_branches=on&field.has_no_branches.used=&field.has_no_branches=on&field.has_blueprints.used=&field.has_blueprints=on&field.has_no_blueprints.used=&field.has_no_blueprints=on) and manually find all the bugs corresponding to a particular bug.

The easiest way to obtain the script and keep it updated is to use the snap:

```bash
sudo snap install ustriage
```

If using the snap is not possible, you can instead obtain it from git and run it by:

```bash
# Running with no arguments will get previous day's bugs
python -m ustriage
```

## Dates

Dates must follow the format: `%Y-%m-%d` (e.g. 2016-11-30, 1999-05-22)

### Single Date Argument

If only one date is given then all the bugs on that one day will be found. For example, the following finds all bugs last modified on only the 10th of September:

```bash
ustriage 2016-09-10
```

### Two Date Arguments

If two dates are given then all the bugs found on those days and between (fully inclusive) will be found. For example, the following, finds all bugs last modified on the 10th, 11th, and 12th of September:

```bash
ustriage 2016-09-10 2016-09-12
```

## Arguments

### Follow Bug Links

By default the script outputs links of the form "LP: #XXXXXX". Ubuntu's
default browser, gnome-terminal, makes these appear as hyperlinks
automatically, saving space and leaving more for the bug titles. If
instead you'd like full URLs, use `--fullurls`.

### Open Bugs in Browser

Quite commonly the triager wants to open all bugs in the browser, to read, review and manage them. Via ``open`` argument that can be done automatically.

```bash
ustriage --open 2016-09-10 2016-09-12
```

### Launchpad Name and Subscription Type

By default this searches for the structural subscription of the ubuntu-server Team.
But depending on the use case one might overwrite the team name with `--lpname` (which can be any launchpad user, doesn't have to be a Team).
Additionally, especially when setting a personal name it is common that the filter should be switched to check for bug subscription instead of a structural subscription which can be done via `--bugsubscriber`.

```bash
#  show all bugs user paelzer is subscribed to (without date modification filter)
ustriage --lpname paelzer --bugsubscriber

# show all bugs user paelzer is subscribed to that were modified last month
ustriage --lpname paelzer --bugsubscriber 2016-08-20 2016-09-20
```

## Bug expiration

To have some kind of tracking of the bugs subscribed by ubuntu-server as well as those tagged server-todo we have to make sure that we identify those that are dormant for too long.
Therefore by default bug expiration info is now added to the output by default.

Since these lists can be rather huge they are not opened in a browser by default.
But if wanted a user can set the option --open-expire

```bash
ustriage 2016-09-10 2016-09-12 --open-expire
```

If instead a user is not interested at all in the expiration he can disable the report by --no-expiration

```bash
ustriage 2016-09-10 2016-09-12 --no-expiration
```

### Output format

The default output format is tailored to a quick overview that also
can be copy and pasted into our triage status reports.

But if someone wants to get more insight from those lists the
argument --extended-format will add more fields to the output.
Those are "date of the last update", "importance" and "assignee" (if there is any)

### Further options and use cases of bug expiration

The expiration is defined as 60 days of inactivity in server-todo tagged bugs, and 180 days for the other ubuntu-server subscribed bugs.
These durations as well as the tag it considers for the "active" list can be tuned via the arguments, --expire-tagged, --expire and --tag.
This can be combined with a custom bug subscriber to be useful outside of the server team triage.
So the following example for example will list any bugs subscribed by hardcoredev which are inactive for 5 or more days with the tag super-urgent.

```bash
ustriage 2016-09-10 2016-09-12 --expire-tagged 5 --tag super-urgent --bugsubscriber hardcoredev
```

### Usage for server bug housekeeping

One can disable the default triage output via `--no-show-triage` and instead
request lists of tagged bugs `--show-tagged` or just subscribed `--show-subscribed`.
This is pretty handy on bug housekeeping as we use server-todo and subscription to
ubuntu-server as our current two levels of [bug tracking](https://github.com/canonical/ubuntu-maintainers-handbook/blob/main/BugTriage.md).

Thereby one can easily check all our current subscribed and `server-todo`
tagged bugs (or any other tag via `--tag`):

It turned out to be a common need to identify differences since the last
meeting. Since the situation in launchpad might have changed (dropped tag,
closed the bug, assigned to other teams, changed subscription) and not all of
them can be detected from launchpad-api after the fast ustriage now also
provides the option to save and compare a list of stored bugs.
On a usual run checking tagged bugs one can add -S to save the reported
bugs to a file. It is recommended to include the timestamp like:
`-S ~/savebugs/todo-$(date -I'seconds').yaml`

On later runs ustriage can compare the current set of bugs with any such stored
list and report new bugs (flag "N") and reports a list of cases gone from the
report.

Furthermore a common need is to see which bugs have had any updates recently.
The option `--flag-recent` allows to specify an amount of days (we use 6
usually) that will make a bug touched in that period get an updated flag "U"
in the report.

All that combined means that we usually run the following command for our
weekly checks

```bash
ustriage --no-show-triage --extended --show-tagged --flag-recent 6 -S ~/savebugs/todo-$(date -I'seconds').yaml -C ~/savebugs/todo-2022-02-01T12:45:10+01:00.yaml
```

Or our bigger backlog of any open `ubuntu-server` (or any other via --lpname)
subscribed bug task. This list can be rather long so `--show-subscribed-max`
reduces it to that many entries from top and bottom of the list.
This shows the most recent and the oldest 20 entries that are `ubuntu-server` subscribed.

```bash
ustriage --no-show-triage --show-subscribed --show-subscribed-max 20 --extended-format
```

Note: The file format on the save/compare feature isn't well defined, do
consider it experimental as it might change without warning. OTOH right now
being just a yaml list of bug numbers makes it very easy to - if needed - modify
it.

### Build and release the snap

Snaps like [ustriage](https://snapcraft.io/ustriage) can be managed via the
[my snaps interface](https://snapcraft.io/ustriage/listing) on snapcraft.io
via the snap [release management](https://snapcraft.io/docs/release-management).

There are usually two reasons to build and release a new revision of ustriage:
* Changes we landed in the repository
* Doing no-change rebuilds to pick up updated dependencies
The latter are usually needed when we get automated notifications that one such dependency had security updates.

#### Builds for Code changes

The [ustriage snap](https://snapcraft.io/ustriage) is configured to be linked to
this [repository](https://github.com/canonical/ubuntu-server-triage) and
therefore, whenever we push to the
[repository](https://github.com/canonical/ubuntu-server-triage) it will
automatically trigger new builds that one can find in the
[builds overview](https://snapcraft.io/ustriage/builds).

#### No change rebuilds

If a rebuild is needed without a code change one can manually hit
_"Trigger new build"_ at the top of the
[builds overview](https://snapcraft.io/ustriage/builds) page of the snap.

#### Releasing new snap revisions

No matter which of the two ways above was triggering the builds, they will
automatically create new revisions which will go to the `latest/edge` channel
as seen in the [releases](https://snapcraft.io/ustriage/releases) overview.

From there, these new revisions can be verified manually (manually because so far
we only have superficial tox testing for some basics). Furthermore, tests are
mostly manual, because most meaningful functions need valid launchpad
credentials to be able to be executed.

Builds for code changes can stay in `latest/edge` until we want to push it
to all users via the channel `latest/stable`. Rebuilds for security reasons
should be pushed to `latest/stable` right after successful verification.

To do so one can hit _"promote"_, which is a hidden button only shown when
hovering over the revision seen on the
[releases overview](https://snapcraft.io/ustriage/releases).
This will show a list of potential channels to promote to, usually
`latest/stable` will be the target. Once that was done for all architectures
hit "Save" in the top right and the new release is complete.
