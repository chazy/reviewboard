import os
import re
import logging
from datetime import datetime

from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _

from djblets.util.db import ConcurrencyManager
from djblets.util.fields import CounterField, ModificationTimestampField
from djblets.util.misc import get_object_or_none
from djblets.util.templatetags.djblets_images import crop_image, thumbnail

from reviewboard.changedescs.models import ChangeDescription
from reviewboard.diffviewer.models import DiffSet, DiffSetHistory, FileDiff
from reviewboard.attachments.models import FileAttachment
from reviewboard.reviews.signals import review_request_published, \
                                        review_request_reopened, \
                                        review_request_closed, \
                                        reply_published, review_published
from reviewboard.reviews.errors import PermissionError
from reviewboard.reviews.managers import DefaultReviewerManager, \
                                         ReviewGroupManager, \
                                         ReviewRequestManager, \
                                         ReviewManager
from reviewboard.scmtools.errors import EmptyChangeSetError, \
                                        InvalidChangeNumberError
from reviewboard.scmtools.models import Repository
from reviewboard.site.models import LocalSite
from reviewboard.site.urlresolvers import local_site_reverse


# The model for the review request summary only allows it to be 300 chars long
MAX_SUMMARY_LENGTH = 300


def update_obj_with_changenum(obj, repository, changenum):
    """
    Utility helper to update a review request or draft from the
    specified changeset's contents on the server.
    """
    changeset = repository.get_scmtool().get_changeset(changenum)

    if not changeset:
        raise InvalidChangeNumberError()

    # If the SCM supports changesets, they should always include a number,
    # summary and description, parsed from the changeset description. Some
    # specialized systems may support the other fields, but we don't want to
    # clobber the user-entered values if they don't.
    obj.changenum = changenum
    obj.summary = changeset.summary
    obj.description = changeset.description
    if changeset.testing_done:
        obj.testing_done = changeset.testing_done
    if changeset.branch:
        obj.branch = changeset.branch
    if changeset.bugs_closed:
        obj.bugs_closed = ','.join(changeset.bugs_closed)


def truncate(string, num):
   if len(string) > num:
      string = string[0:num]
      i = string.rfind('.')

      if i != -1:
         string = string[0:i + 1]

   return string


class Group(models.Model):
    """
    A group of reviewers identified by a name. This is usually used to
    separate teams at a company or components of a project.

    Each group can have an e-mail address associated with it, sending
    all review requests and replies to that address. If that e-mail address is
    blank, e-mails are sent individually to each member of that group.
    """
    name = models.SlugField(_("name"), max_length=64, blank=False)
    display_name = models.CharField(_("display name"), max_length=64)
    mailing_list = models.EmailField(_("mailing list"), blank=True,
        help_text=_("The mailing list review requests and discussions "
                    "are sent to."))
    users = models.ManyToManyField(User, blank=True,
                                   related_name="review_groups",
                                   verbose_name=_("users"))
    local_site = models.ForeignKey(LocalSite, blank=True, null=True)

    incoming_request_count = CounterField(
        _('incoming review request count'),
        initializer=lambda g: ReviewRequest.objects.to_group(
            g, local_site=g.local_site).count())

    invite_only = models.BooleanField(_('invite only'), default=False)
    visible = models.BooleanField(default=True)
    ready_for_reviews = models.BooleanField(default=False)

    objects = ReviewGroupManager()

    def is_accessible_by(self, user):
        "Returns true if the user can access this group."""
        if self.local_site and not self.local_site.is_accessible_by(user):
            return False

        return (not self.invite_only or
                user.is_superuser or
                (user.is_authenticated() and
                 self.users.filter(pk=user.pk).count() > 0))

    def __unicode__(self):
        return self.name

    def get_absolute_url(self):
        if self.local_site:
            local_site_name = self.local_site.name
        else:
            local_site_name = None

        return local_site_reverse('group', local_site_name=local_site_name,
                                  kwargs={'name': self.name})

    class Meta:
        unique_together = (('name', 'local_site'),)
        verbose_name = _("review group")
        ordering = ['name']


class DefaultReviewer(models.Model):
    """
    A default reviewer entry automatically adds default reviewers to a
    review request when the diff modifies a file matching the ``file_regex``
    pattern specified.

    This is useful when different groups own different parts of a codebase.
    Adding DefaultReviewer entries ensures that the right people will always
    see the review request and discussions.

    A ``file_regex`` of ``".*"`` will add the specified reviewers by
    default for every review request.

    Note that this is keyed off the same LocalSite as its "repository" member.
    """
    name = models.CharField(_("name"), max_length=64)
    file_regex = models.CharField(_("file regex"), max_length=256,
        help_text=_("File paths are matched against this regular expression "
                    "to determine if these reviewers should be added."))
    repository = models.ManyToManyField(Repository, blank=True)
    groups = models.ManyToManyField(Group, verbose_name=_("default groups"),
                                    blank=True)
    people = models.ManyToManyField(User, verbose_name=_("default people"),
                                    related_name="default_review_paths",
                                    blank=True)
    local_site = models.ForeignKey(LocalSite, blank=True, null=True,
                                   related_name='default_reviewers')

    objects = DefaultReviewerManager()

    def __unicode__(self):
        return self.name


class Screenshot(models.Model):
    """
    A screenshot associated with a review request.

    Like diffs, a screenshot can have comments associated with it.
    These comments are of type :model:`reviews.ScreenshotComment`.
    """
    caption = models.CharField(_("caption"), max_length=256, blank=True)
    draft_caption = models.CharField(_("draft caption"),
                                     max_length=256, blank=True)
    image = models.ImageField(_("image"),
                              upload_to=os.path.join('uploaded', 'images',
                                                     '%Y', '%m', '%d'))

    def get_thumbnail_url(self):
        """
        Returns the URL for the thumbnail, creating it if necessary.
        """
        return thumbnail(self.image)

    def thumb(self):
        """
        Creates a thumbnail of this screenshot and returns the HTML
        output embedding the thumbnail.
        """
        url = self.get_thumbnail_url()
        return mark_safe('<img src="%s" alt="%s" />' % (url, self.caption))
    thumb.allow_tags = True

    def __unicode__(self):
        return u"%s (%s)" % (self.caption, self.image)

    def get_review_request(self):
        try:
            return self.review_request.all()[0]
        except IndexError:
            try:
                return self.inactive_review_request.all()[0]
            except IndexError:
                # Maybe it's on a draft.
                try:
                    draft = self.drafts.get()
                except ReviewRequestDraft.DoesNotExist:
                    draft = self.inactive_drafts.get()

                return draft.review_request

    def get_absolute_url(self):
        review_request = self.get_review_request()

        if review_request.local_site:
            local_site_name = review_request.local_site.name
        else:
            local_site_name = None

        return local_site_reverse(
            'screenshot',
            local_site_name=local_site_name,
            kwargs={
                'review_request_id': review_request.display_id,
                'screenshot_id': self.pk,
            })

    def save(self, **kwargs):
        super(Screenshot, self).save()

        try:
            draft = self.drafts.get()
            draft.timestamp = datetime.now()
            draft.save()
        except ReviewRequestDraft.DoesNotExist:
            pass

    def save(self, **kwargs):
        super(Screenshot, self).save()

        try:
            draft = self.drafts.get()
            draft.timestamp = datetime.now()
            draft.save()
        except ReviewRequestDraft.DoesNotExist:
            pass


class ReviewRequest(models.Model):
    """
    A review request.

    This is one of the primary models in Review Board. Most everything
    is associated with a review request.

    The ReviewRequest model contains detailed information on a review
    request. Some fields are user-modifiable, while some are used for
    internal state.
    """
    PENDING_REVIEW = "P"
    SUBMITTED      = "S"
    DISCARDED      = "D"

    STATUSES = (
        (PENDING_REVIEW, _('Pending Review')),
        (SUBMITTED,      _('Submitted')),
        (DISCARDED,      _('Discarded')),
    )

    submitter = models.ForeignKey(User, verbose_name=_("submitter"),
                                  related_name="review_requests")
    time_added = models.DateTimeField(_("time added"), default=datetime.now)
    last_updated = ModificationTimestampField(_("last updated"))
    status = models.CharField(_("status"), max_length=1, choices=STATUSES,
                              db_index=True)
    public = models.BooleanField(_("public"), default=False)
    changenum = models.PositiveIntegerField(_("change number"), blank=True,
                                            null=True, db_index=True)
    repository = models.ForeignKey(Repository,
                                   related_name="review_requests",
                                   verbose_name=_("repository"),
                                   null=True,
                                   blank=True)
    email_message_id = models.CharField(_("e-mail message ID"), max_length=255,
                                        blank=True, null=True)
    time_emailed = models.DateTimeField(_("time e-mailed"), null=True,
                                        default=None, blank=True)

    summary = models.CharField(_("summary"), max_length=300)
    description = models.TextField(_("description"), blank=True)
    testing_done = models.TextField(_("testing done"), blank=True)
    bugs_closed = models.CharField(_("bugs"), max_length=300, blank=True)
    diffset_history = models.ForeignKey(DiffSetHistory,
                                        related_name="review_request",
                                        verbose_name=_('diff set history'),
                                        blank=True)
    branch = models.CharField(_("branch"), max_length=300, blank=True)
    target_groups = models.ManyToManyField(
        Group,
        related_name="review_requests",
        verbose_name=_("target groups"),
        blank=True)
    target_people = models.ManyToManyField(
        User,
        verbose_name=_("target people"),
        related_name="directed_review_requests",
        blank=True)
    screenshots = models.ManyToManyField(
        Screenshot,
        related_name="review_request",
        verbose_name=_("screenshots"),
        blank=True)
    inactive_screenshots = models.ManyToManyField(Screenshot,
        verbose_name=_("inactive screenshots"),
        help_text=_("A list of screenshots that used to be but are no "
                    "longer associated with this review request."),
        related_name="inactive_review_request",
        blank=True)

    file_attachments = models.ManyToManyField(
        FileAttachment,
        related_name="review_request",
        verbose_name=_("file attachments"),
        blank=True)
    inactive_file_attachments = models.ManyToManyField(FileAttachment,
        verbose_name=_("inactive file attachments"),
        help_text=_("A list of file attachments that used to be but are no "
                    "longer associated with this review request."),
        related_name="inactive_review_request",
        blank=True)

    changedescs = models.ManyToManyField(ChangeDescription,
        verbose_name=_("change descriptions"),
        related_name="review_request",
        blank=True)

    # Review-related information
    last_review_timestamp = models.DateTimeField(_("last review timestamp"),
                                                 null=True, default=None,
                                                 blank=True)
    shipit_count = CounterField(_("ship-it count"), default=0)

    local_site = models.ForeignKey(LocalSite, blank=True, null=True)
    local_id = models.IntegerField('site-local ID', blank=True, null=True)

    # Set this up with the ReviewRequestManager
    objects = ReviewRequestManager()

    def get_participants(self):
        """
        Returns a list of all people who have been involved in discussing
        this review request.
        """
        # See the comment in Review.get_participants for this list
        # comprehension.
        return [u for review in self.reviews.all()
                  for u in review.participants]

    participants = property(get_participants)

    def get_bug_list(self):
        """
        Returns a sorted list of bugs associated with this review request.
        """
        if self.bugs_closed == "":
            return []

        bugs = re.split(r"[, ]+", self.bugs_closed)

        # First try a numeric sort, to show the best results for the majority
        # case of bug trackers with numeric IDs.  If that fails, sort
        # alphabetically.
        try:
            bugs.sort(cmp=lambda x,y: cmp(int(x), int(y)))
        except ValueError:
            bugs.sort()

        return bugs

    def get_new_reviews(self, user):
        """
        Returns any new reviews since the user last viewed the review request.
        """
        if user.is_authenticated():
            # If this ReviewRequest was queried using with_counts=True,
            # then we should know the new review count and can use this to
            # decide whether we have anything at all to show.
            if hasattr(self, "new_review_count") and self.new_review_count > 0:
                query = self.visits.filter(user=user)

                try:
                    visit = query[0]

                    return [review for review in
                        self.reviews.filter(
                            public=True,
                            timestamp__gt=visit.timestamp).exclude(user=user)
                        if review.is_accessible_by(user)]
                except IndexError:
                    # This visit doesn't exist, so bail.
                    pass


        return self.reviews.get_empty_query_set()

    def add_default_reviewers(self):
        """
        Add default reviewers to this review request based on the diffset.

        This method goes through the DefaultReviewer objects in the database and
        adds any missing reviewers based on regular expression comparisons with
        the set of files in the diff.
        """

        if self.diffset_history.diffsets.count() != 1:
            return

        diffset = self.diffset_history.diffsets.get()

        people = set()
        groups = set()

        # TODO: This is kind of inefficient, and could maybe be optimized in
        # some fancy way.  Certainly the most superficial optimization that
        # could be made would be to cache the compiled regexes somewhere.
        files = diffset.files.all()
        for default in DefaultReviewer.objects.for_repository(self.repository):
            regex = re.compile(default.file_regex)

            for filediff in files:
                if regex.match(filediff.source_file or filediff.dest_file):
                    for person in default.people.all():
                        people.add(person)
                    for group in default.groups.all():
                        groups.add(group)
                    break

        existing_people = self.target_people.all()
        for person in people:
            if person not in existing_people:
                self.target_people.add(person)

        existing_groups = self.target_groups.all()
        for group in groups:
            if group not in existing_groups:
                self.target_groups.add(group)

    def get_display_id(self):
        """Gets the ID which should be exposed to the user."""
        if self.local_site_id:
            return self.local_id
        else:
            return self.id

    display_id = property(get_display_id)

    def get_public_reviews(self, request_user):
        """
        Returns all public top-level reviews for this review request.
        """
        return [review for review in
            self.reviews.filter(public=True, base_reply_to__isnull=True)
            if review.is_accessible_by(request_user)]

    def update_from_changenum(self, changenum):
        """
        Updates this review request from the specified changeset's contents
        on the server.
        """
        update_obj_with_changenum(self, self.repository, changenum)

    def is_accessible_by(self, user, local_site=None):
        """Returns whether or not the user can read this review request.

        This performs several checks to ensure that the user has access.
        This user has access if:

          * The review request is public or the user can modify it (either
            by being an owner or having special permissions).

          * The repository is public or the user has access to it (either by
            being explicitly on the allowed users list, or by being a member
            of a review group on that list).

          * The user is listed as a requested reviewer or the user has access
            to one or more groups listed as requested reviewers (either by
            being a member of an invite-only group, or the group being public).
        """
        if not self.public and not self.is_mutable_by(user):
            return False

        if self.repository and not self.repository.is_accessible_by(user):
            return False

        if local_site and not local_site.is_accessible_by(user):
            return False

        if (user.is_authenticated() and
            self.target_people.filter(pk=user.pk).count() > 0):
            return True

        groups = list(self.target_groups.all())

        if not groups:
            return True

        # We specifically iterate over these instead of making it part
        # of the query in order to keep the logic in Group, and to allow
        # for future expansion (extensions, more advanced policy)
        #
        # We're looking for at least one group that the user has access
        # to. If they can access any of the groups, then they have access
        # to the review request.
        for group in groups:
            if group.is_accessible_by(user):
                return True

        # If the submitter of the review belongs to a review group, to which
        # the current user also belongs, then we allow them to see the review
        # request.
        for group in Group.objects.accessible(self.submitter):
            if group.is_accessible_by(user):
                return True

        # If the user submitted the review request (or it was submitted as the
        # user) then it should obviously be OK for the user to see the review
        # request.
        if self.submitter == user:
            return True

        return False

    def is_mutable_by(self, user):
        "Returns true if the user can modify this review request"
        #return self.submitter == user or \
        #       user.has_perm('reviews.can_edit_reviewrequest')
        return user.has_perm('reviews.can_edit_reviewrequest')

    def get_draft(self, user=None):
        """
        Returns the draft of the review request. If a user is specified,
        than the draft will be returned only if owned by the user. Otherwise,
        None will be returned.
        """
        if not user:
            return get_object_or_none(self.draft)
        elif user.is_authenticated():
            return get_object_or_none(self.draft,
                                      review_request__submitter=user)

        return None

    def get_pending_review(self, user):
        """
        Returns the pending review owned by the specified user, if any.
        This will return an actual review, not a reply to a review.
        """
        return Review.objects.get_pending_review(self, user)

    def get_last_activity(self):
        """Returns the last public activity information on the review request.

        This will return the last object updated, along with the timestamp
        of that object. It can be used to judge whether something on a
        review request has been made public more recently.
        """
        timestamp = self.last_updated
        updated_object = self

        # Check if the diff was updated along with this.
        try:
            diffset = self.diffset_history.diffsets.latest()

            if diffset.timestamp >= timestamp:
                timestamp = diffset.timestamp
                updated_object = diffset
        except DiffSet.DoesNotExist:
            pass

        # Check for the latest review or reply.
        try:
            review = self.reviews.filter(public=True).latest()

            if review.timestamp >= timestamp:
                timestamp = review.timestamp
                updated_object = review
        except Review.DoesNotExist:
            pass

        return timestamp, updated_object

    def changeset_is_pending(self):
        """
        Returns True if the current changeset associated with this review
        request is pending under SCM.
        """
        changeset = None
        if self.changenum:
            try:
                changeset = self.repository.get_scmtool().get_changeset(self.changenum)
            except (EmptyChangeSetError, NotImplementedError):
                pass

        return changeset and changeset.pending

    def get_absolute_url(self):
        if self.local_site:
            local_site_name = self.local_site.name
        else:
            local_site_name = None

        return local_site_reverse('review-request-detail',
                                  local_site_name=local_site_name,
                                  kwargs={'review_request_id': self.display_id})

    def __unicode__(self):
        if self.summary:
            return self.summary
        else:
            return unicode(_('(no summary)'))

    def save(self, update_counts=False, **kwargs):
        self.bugs_closed = self.bugs_closed.strip()
        self.summary = truncate(self.summary, MAX_SUMMARY_LENGTH)

        if update_counts or self.id is None:
            self._update_counts()

        if self.status != self.PENDING_REVIEW:
            # If this is not a pending review request now, delete any
            # and all ReviewRequestVisit objects.
            self.visits.all().delete()

        super(ReviewRequest, self).save(**kwargs)

    def delete(self, **kwargs):
        from reviewboard.accounts.models import Profile, LocalSiteProfile

        profile, profile_is_new = \
            Profile.objects.get_or_create(user=self.submitter)

        if profile_is_new:
            profile.save()

        local_site = self.local_site
        site_profile, site_profile_is_new = \
            LocalSiteProfile.objects.get_or_create(user=self.submitter,
                                                   profile=profile,
                                                   local_site=local_site)

        site_profile.decrement_total_outgoing_request_count()

        if self.status == self.PENDING_REVIEW:
            site_profile.decrement_pending_outgoing_request_count()

        if self.public:
            people = self.target_people.all()
            groups = self.target_groups.all()

            Group.incoming_request_count.decrement(groups)
            LocalSiteProfile.direct_incoming_request_count.decrement(
                LocalSiteProfile.objects.filter(user__in=people,
                                                local_site=local_site))
            LocalSiteProfile.total_incoming_request_count.decrement(
                LocalSiteProfile.objects.filter(
                    Q(local_site=local_site) &
                    Q(Q(user__review_groups__in=groups) |
                      Q(user__in=people))))
            LocalSiteProfile.starred_public_request_count.decrement(
                LocalSiteProfile.objects.filter(
                    profile__starred_review_requests=self,
                    local_site=local_site))

        super(ReviewRequest, self).delete(**kwargs)

    def can_publish(self):
        return not self.public or get_object_or_none(self.draft) is not None

    def close(self, type, user=None, description=None):
        """
        Closes the review request. The type must be one of
        SUBMITTED or DISCARDED.
        """
        if (user and not self.is_mutable_by(user) and
            not user.has_perm("reviews.can_change_status")):
            raise PermissionError

        if type not in [self.SUBMITTED, self.DISCARDED]:
            raise AttributeError("%s is not a valid close type" % type)

        if self.status != type:
            changedesc = ChangeDescription(public=True, text=description or "")
            changedesc.record_field_change('status', self.status, type)
            changedesc.save()

            self.changedescs.add(changedesc)
            self.status = type
            self.save(update_counts=True)

            review_request_closed.send(sender=self.__class__, user=user,
                                       review_request=self,
                                       type=type)
        else:
            # Update submission description.
            changedesc = self.changedescs.filter(public=True).latest()
            changedesc.timestamp = datetime.now()
            changedesc.text = description or ""
            changedesc.save()

            # Needed to renew last-update.
            self.save()

        try:
            draft = self.draft.get()
        except ReviewRequestDraft.DoesNotExist:
            pass
        else:
            draft.delete()

    def reopen(self, user=None):
        """
        Reopens the review request for review.
        """
        if (user and not self.is_mutable_by(user) and
            not user.has_perm("reviews.can_change_status")):
            raise PermissionError

        if self.status != self.PENDING_REVIEW:
            changedesc = ChangeDescription()
            changedesc.record_field_change('status', self.status,
                                           self.PENDING_REVIEW)

            if self.status == self.DISCARDED:
                # A draft is needed if reopening a discarded review request.
                self.public = False
                changedesc.save()
                draft = ReviewRequestDraft.create(self)
                draft.changedesc = changedesc
                draft.save()
            else:
                changedesc.public = True
                changedesc.save()
                self.changedescs.add(changedesc)

            self.status = self.PENDING_REVIEW
            self.save(update_counts=True)

        review_request_reopened.send(sender=self.__class__, user=user,
                                     review_request=self)

    def update_changenum(self,changenum, user=None):
        if (user and not self.is_mutable_by(user)):
            raise PermissionError

        self.changenum = changenum
        self.save()

    def publish(self, user):
        from reviewboard.accounts.models import LocalSiteProfile

        """
        Save the current draft attached to this review request. Send out the
        associated email. Returns the review request that was saved.
        """
        if not self.is_mutable_by(user):
            raise PermissionError

        # Decrement the counts on everything. we lose them.
        # We'll increment the resulting set during ReviewRequest.save.
        # This should be done before the draft is published.
        # Once the draft is published, the target people
        # and groups will be updated with new values.
        # Decrement should not happen while publishing
        # a new request or a discarded request
        if self.public:
            Group.incoming_request_count.decrement(self.target_groups.all())
            LocalSiteProfile.direct_incoming_request_count.decrement(
                    LocalSiteProfile.objects.filter(
                        user__in=self.target_people.all(),
                        local_site=self.local_site))
            LocalSiteProfile.total_incoming_request_count.decrement(
                    LocalSiteProfile.objects.filter(
                        Q(local_site=self.local_site) &
                        Q(Q(user__review_groups__in= \
                            self.target_groups.all()) |
                          Q(user__in=self.target_people.all()))))
            LocalSiteProfile.starred_public_request_count.decrement(
                    LocalSiteProfile.objects.filter(
                        profile__starred_review_requests=self,
                        local_site=self.local_site))

        draft = get_object_or_none(self.draft)
        if draft is not None:
            # This will in turn save the review request, so we'll be done.
            changes = draft.publish(self, send_notification=False)
            draft.delete()
        else:
            changes = None

        self.public = True
        self.save(update_counts=True)

        review_request_published.send(sender=self.__class__, user=user,
                                      review_request=self,
                                      changedesc=changes)

    def _update_counts(self):
        from reviewboard.accounts.models import Profile, LocalSiteProfile

        profile, profile_is_new = \
            Profile.objects.get_or_create(user=self.submitter)

        if profile_is_new:
            profile.save()

        local_site = self.local_site
        site_profile, site_profile_is_new = \
            LocalSiteProfile.objects.get_or_create(user=self.submitter,
                                              profile=profile,
                                              local_site=local_site)

        if site_profile_is_new:
            site_profile.save()

        if self.id is None:
            # This hasn't been created yet. Bump up the outgoing request
            # count for the user.
            site_profile.increment_total_outgoing_request_count()
            old_status = None
            old_public = False
        else:
            # We need to see if the status has changed, so that means
            # finding out what's in the database.
            r = ReviewRequest.objects.get(pk=self.id)
            old_status = r.status
            old_public = r.public

        if self.status == self.PENDING_REVIEW:
            if old_status != self.status:
                site_profile.increment_pending_outgoing_request_count()

            if self.public and self.id is not None:
                groups = self.target_groups.all()
                people = self.target_people.all()

                Group.incoming_request_count.increment(groups)
                LocalSiteProfile.direct_incoming_request_count.increment(
                    LocalSiteProfile.objects.filter(user__in=people,
                                                    local_site=local_site))
                LocalSiteProfile.total_incoming_request_count.increment(
                    LocalSiteProfile.objects.filter(
                        Q(local_site=local_site) &
                        Q(Q(user__review_groups__in=groups) |
                          Q(user__in=people))))
                LocalSiteProfile.starred_public_request_count.increment(
                    LocalSiteProfile.objects.filter(
                        profile__starred_review_requests=self,
                        local_site=local_site))
        else:
            if old_status != self.status:
                site_profile.decrement_pending_outgoing_request_count()

            if old_public:
                groups = self.target_groups.all()
                people = self.target_people.all()

                Group.incoming_request_count.decrement(groups)
                LocalSiteProfile.direct_incoming_request_count.decrement(
                    LocalSiteProfile.objects.filter(user__in=people,
                                                    local_site=local_site))
                LocalSiteProfile.total_incoming_request_count.decrement(
                    LocalSiteProfile.objects.filter(
                        Q(local_site=local_site) &
                        Q(Q(user__review_groups__in=groups) |
                          Q(user__in=people))))
                LocalSiteProfile.starred_public_request_count.decrement(
                    LocalSiteProfile.objects.filter(
                        profile__starred_review_requests=self,
                        local_site=local_site))

    class Meta:
        ordering = ['-last_updated', 'submitter', 'summary']
        unique_together = (('changenum', 'repository'),
                           ('local_site', 'local_id'))
        permissions = (
            ("can_change_status", "Can change status"),
            ("can_submit_as_another_user", "Can submit as another user"),
            ("can_edit_reviewrequest", "Can edit review request"),
        )


class ReviewRequestDraft(models.Model):
    """
    A draft of a review request.

    When a review request is being modified, a special draft copy of it is
    created containing all the details of the review request. This copy can
    be modified and eventually saved or discarded. When saved, the new
    details are copied back over to the originating ReviewRequest.
    """
    review_request = models.ForeignKey(ReviewRequest,
                                       related_name="draft",
                                       verbose_name=_("review request"),
                                       unique=True)
    last_updated = ModificationTimestampField(_("last updated"))
    summary = models.CharField(_("summary"), max_length=300)
    description = models.TextField(_("description"))
    testing_done = models.TextField(_("testing done"))
    bugs_closed = models.CommaSeparatedIntegerField(_("bugs"),
                                                    max_length=300, blank=True)
    diffset = models.ForeignKey(DiffSet, verbose_name=_('diff set'),
                                blank=True, null=True,
                                related_name='review_request_draft')
    changedesc = models.ForeignKey(ChangeDescription,
                                   verbose_name=_('change description'),
                                   blank=True, null=True)
    branch = models.CharField(_("branch"), max_length=300, blank=True)
    target_groups = models.ManyToManyField(Group,
                                           related_name="drafts",
                                           verbose_name=_("target groups"),
                                           blank=True)
    target_people = models.ManyToManyField(User,
                                           verbose_name=_("target people"),
                                           related_name="directed_drafts",
                                           blank=True)
    screenshots = models.ManyToManyField(Screenshot,
                                         related_name="drafts",
                                         verbose_name=_("screenshots"),
                                         blank=True)
    inactive_screenshots = models.ManyToManyField(Screenshot,
        verbose_name=_("inactive screenshots"),
        related_name="inactive_drafts",
        blank=True)

    file_attachments = models.ManyToManyField(
        FileAttachment,
        related_name="drafts",
        verbose_name=_("file attachments"),
        blank=True)
    inactive_file_attachments = models.ManyToManyField(
        FileAttachment,
        verbose_name=_("inactive files"),
        related_name="inactive_drafts",
        blank=True)

    submitter = property(lambda self: self.review_request.submitter)

    # Set this up with a ConcurrencyManager to help prevent race conditions.
    objects = ConcurrencyManager()

    def get_bug_list(self):
        """
        Returns a sorted list of bugs associated with this review request.
        """
        if self.bugs_closed == "":
            return []

        bugs = re.split(r"[, ]+", self.bugs_closed)

        # First try a numeric sort, to show the best results for the majority
        # case of bug trackers with numeric IDs.  If that fails, sort
        # alphabetically.
        try:
            bugs.sort(cmp=lambda x,y: cmp(int(x), int(y)))
        except ValueError:
            bugs.sort()

        return bugs

    def __unicode__(self):
        return self.summary

    def save(self, **kwargs):
        self.bugs_closed = self.bugs_closed.strip()
        self.summary = truncate(self.summary, MAX_SUMMARY_LENGTH)
        super(ReviewRequestDraft, self).save()

    @staticmethod
    def create(review_request):
        """
        Creates a draft based on a review request.

        This will copy over all the details of the review request that
        we care about. If a draft already exists for the review request,
        the draft will be returned.
        """
        draft, draft_is_new = \
            ReviewRequestDraft.objects.get_or_create(
                review_request=review_request,
                defaults={
                    'summary': review_request.summary,
                    'description': review_request.description,
                    'testing_done': review_request.testing_done,
                    'bugs_closed': review_request.bugs_closed,
                    'branch': review_request.branch,
                })

        if draft.changedesc is None and review_request.public:
            changedesc = ChangeDescription()
            changedesc.save()
            draft.changedesc = changedesc

        if draft_is_new:
            map(draft.target_groups.add, review_request.target_groups.all())
            map(draft.target_people.add, review_request.target_people.all())
            for screenshot in review_request.screenshots.all():
                screenshot.draft_caption = screenshot.caption
                screenshot.save()
                draft.screenshots.add(screenshot)

            for screenshot in review_request.inactive_screenshots.all():
                screenshot.draft_caption = screenshot.caption
                screenshot.save()
                draft.inactive_screenshots.add(screenshot)

            for attachment in review_request.file_attachments.all():
                attachment.draft_caption = attachment.caption
                attachment.save()
                draft.file_attachments.add(attachment)

            for attachment in review_request.inactive_file_attachments.all():
                attachment.draft_caption = attachment.caption
                attachment.save()
                draft.inactive_file_attachments.add(attachment)

            draft.save();

        return draft

    def add_default_reviewers(self):
        """
        Add default reviewers to this draft based on the diffset.

        This method goes through the DefaultReviewer objects in the database and
        adds any missing reviewers based on regular expression comparisons with
        the set of files in the diff.
        """

        if not self.diffset:
            return

        repository = self.review_request.repository
        people = set()
        groups = set()

        # TODO: This is kind of inefficient, and could maybe be optimized in
        # some fancy way.  Certainly the most superficial optimization that
        # could be made would be to cache the compiled regexes somewhere.
        files = self.diffset.files.all()
        for default in DefaultReviewer.objects.for_repository(repository):
            try:
                regex = re.compile(default.file_regex)
            except:
                continue

            for filediff in files:
                if regex.match(filediff.source_file or filediff.dest_file):
                    for person in default.people.all():
                        people.add(person)
                    for group in default.groups.all():
                        groups.add(group)
                    break

        existing_people = self.target_people.all()
        for person in people:
            if person not in existing_people:
                self.target_people.add(person)

        existing_groups = self.target_groups.all()
        for group in groups:
            if group not in existing_groups:
                self.target_groups.add(group)

    def publish(self, review_request=None, user=None,
                send_notification=True):
        """
        Publishes this draft. Uses the draft's assocated ReviewRequest
        object if one isn't passed in.

        This updates and returns the draft's ChangeDescription, which
        contains the changed fields. This is used by the e-mail template
        to tell people what's new and interesting.

        The keys that may be saved in 'fields_changed' in the
        ChangeDescription are:

           *  'summary'
           *  'description'
           *  'testing_done'
           *  'bugs_closed'
           *  'branch'
           *  'target_groups'
           *  'target_people'
           *  'screenshots'
           *  'screenshot_captions'
           *  'diff'

        Each field in 'fields_changed' represents a changed field. This will
        save fields in the standard formats as defined by the
        'ChangeDescription' documentation, with the exception of the
        'screenshot_captions' and 'diff' fields.

        For the 'screenshot_captions' field, the value will be a dictionary
        of screenshot ID/dict pairs with the following fields:

           * 'old': The old value of the field
           * 'new': The new value of the field

        For the 'diff' field, there is only ever an 'added' field, containing
        the ID of the new diffset.

        The 'send_notification' parameter is intended for internal use only,
        and is there to prevent duplicate notifications when being called by
        ReviewRequest.publish.
        """
        if not review_request:
            review_request = self.review_request

        if not user:
            user = review_request.submitter

        if not self.changedesc and review_request.public:
            self.changedesc = ChangeDescription()

        def update_field(a, b, name, record_changes=True):
            # Apparently django models don't have __getattr__ or __setattr__,
            # so we have to update __dict__ directly.  Sigh.
            value = b.__dict__[name]
            old_value = a.__dict__[name]

            if old_value != value:
                if record_changes and self.changedesc:
                    self.changedesc.record_field_change(name, old_value, value)

                a.__dict__[name] = value

        def update_list(a, b, name, record_changes=True, name_field=None):
            aset = set([x.id for x in a.all()])
            bset = set([x.id for x in b.all()])

            if aset.symmetric_difference(bset):
                if record_changes and self.changedesc:
                    self.changedesc.record_field_change(name, a.all(), b.all(),
                                                        name_field)

                a.clear()
                map(a.add, b.all())

        update_field(review_request, self, 'summary')
        update_field(review_request, self, 'description')
        update_field(review_request, self, 'testing_done')
        update_field(review_request, self, 'branch')

        update_list(review_request.target_groups, self.target_groups,
                    'target_groups', name_field="name")
        update_list(review_request.target_people, self.target_people,
                    'target_people', name_field="username")

        # Specifically handle bug numbers
        old_bugs = review_request.get_bug_list()
        new_bugs = self.get_bug_list()

        if set(old_bugs) != set(new_bugs):
            update_field(review_request, self, 'bugs_closed',
                         record_changes=False)

            if self.changedesc:
                self.changedesc.record_field_change('bugs_closed',
                                                    old_bugs, new_bugs)


        # Screenshots are a bit special.  The list of associated screenshots can
        # change, but so can captions within each screenshot.
        screenshots = self.screenshots.all()
        caption_changes = {}

        for s in review_request.screenshots.all():
            if s in screenshots and s.caption != s.draft_caption:
                caption_changes[s.id] = {
                    'old': (s.caption,),
                    'new': (s.draft_caption,),
                }

                s.caption = s.draft_caption
                s.save()

        if caption_changes and self.changedesc:
            self.changedesc.fields_changed['screenshot_captions'] = \
                caption_changes

        update_list(review_request.screenshots, self.screenshots,
                    'screenshots', name_field="caption")

        # There's no change notification required for this field.
        review_request.inactive_screenshots.clear()
        map(review_request.inactive_screenshots.add,
            self.inactive_screenshots.all())

        # Files are treated like screenshots. The list of files can
        # change, but so can captions within each file.
        files = self.file_attachments.all()
        caption_changes = {}

        for f in review_request.file_attachments.all():
            if f in files and f.caption != f.draft_caption:
                caption_changes[f.id] = {
                    'old': (f.caption,),
                    'new': (f.draft_caption,),
                }

                f.caption = f.draft_caption
                f.save()

        if caption_changes and self.changedesc:
            self.changedesc.fields_changed['file_captions'] = \
                caption_changes

        update_list(review_request.file_attachments, self.file_attachments,
                    'files', name_field="caption")

        # There's no change notification required for this field.
        review_request.inactive_file_attachments.clear()
        map(review_request.inactive_file_attachments.add,
            self.inactive_file_attachments.all())

        if self.diffset:
            if self.changedesc:
                if review_request.local_site:
                    local_site_name = review_request.local_site.name
                else:
                    local_site_name = None

                url = local_site_reverse(
                    'view_diff_revision',
                    local_site_name=local_site_name,
                    args=[review_request.display_id, self.diffset.revision])
                self.changedesc.fields_changed['diff'] = {
                    'added': [(_("Diff r%s") % self.diffset.revision,
                               url,
                               self.diffset.id)],
                }

            self.diffset.history = review_request.diffset_history
            self.diffset.save()

        if self.changedesc:
            self.changedesc.timestamp = datetime.now()
            self.changedesc.public = True
            self.changedesc.save()
            review_request.changedescs.add(self.changedesc)

        review_request.save()

        if send_notification:
            review_request_published.send(sender=review_request.__class__,
                                          user=user,
                                          review_request=review_request,
                                          changedesc=self.changedesc)

        return self.changedesc

    def update_from_changenum(self, changenum):
        """
        Updates this draft from the specified changeset's contents on
        the server.
        """
        update_obj_with_changenum(self, self.review_request.repository,
                                  changenum)

    class Meta:
        ordering = ['-last_updated']


class BaseComment(models.Model):
    OPEN           = "O"
    RESOLVED       = "R"
    DROPPED        = "D"

    ISSUE_STATUSES = (
        (OPEN,      _('Open')),
        (RESOLVED,  _('Resolved')),
        (DROPPED,   _('Dropped')),
    )
    points_deducted = models.PositiveIntegerField(_("points deducted"), default=0)
    issue_opened = models.BooleanField(_("issue opened"), default=False)
    issue_status = models.CharField(_("issue status"),
                                    max_length=1,
                                    choices=ISSUE_STATUSES,
                                    blank=True,
                                    null=True,
                                    db_index=True)

    @staticmethod
    def issue_status_to_string(status):
        if status == "O":
            return "open"
        elif status == "R":
            return "resolved"
        elif status == "D":
            return "dropped"
        else:
            return ""

    @staticmethod
    def issue_string_to_status(status):
        if status == "open":
            return "O"
        elif status == "resolved":
            return "R"
        elif status == "dropped":
            return "D"
        else:
            raise Exception("Invalid issue status '%s'" % status)

    def save(self, **kwargs):
        self.timestamp = datetime.now()

        super(BaseComment, self).save()

        try:
            # Update the review timestamp, but only if it's a draft.
            # Otherwise, resolving an issue will change the timestamp of
            # the review.
            review = self.review.get()

            if not review.public:
                review.timestamp = self.timestamp
                review.save()
        except Review.DoesNotExist:
            pass

    class Meta:
        abstract = True
        ordering = ['timestamp']


class Comment(BaseComment):
    """
    A comment made on a diff.

    A comment can belong to a single filediff or to an interdiff between
    two filediffs. It can also have multiple replies.
    """

    filediff = models.ForeignKey(FileDiff, verbose_name=_('file diff'),
                                 related_name="comments")
    interfilediff = models.ForeignKey(FileDiff,
                                      verbose_name=_('interdiff file'),
                                      blank=True, null=True,
                                      related_name="interdiff_comments")
    reply_to = models.ForeignKey("self", blank=True, null=True,
                                 related_name="replies",
                                 verbose_name=_("reply to"))
    timestamp = models.DateTimeField(_('timestamp'), default=datetime.now)
    text = models.TextField(_("comment text"))

    # A null line number applies to an entire diff.  Non-null line numbers are
    # the line within the entire file, starting at 1.
    first_line = models.PositiveIntegerField(_("first line"), blank=True,
                                             null=True)
    num_lines = models.PositiveIntegerField(_("number of lines"), blank=True,
                                            null=True)

    last_line = property(lambda self: self.first_line + self.num_lines - 1)

    # Set this up with a ConcurrencyManager to help prevent race conditions.
    objects = ConcurrencyManager()

    def public_replies(self, user=None):
        """
        Returns a list of public replies to this comment, optionally
        specifying the user replying.
        """
        if user:
            return self.replies.filter(Q(review__public=True) |
                                       Q(review__user=user))
        else:
            return self.replies.filter(review__public=True)

    def get_absolute_url(self):
        revision_path = str(self.filediff.diffset.revision)
        if self.interfilediff:
            revision_path += "-%s" % self.interfilediff.diffset.revision

        return "%sdiff/%s/?file=%s#file%sline%s" % \
             (self.review.get().review_request.get_absolute_url(),
              revision_path, self.filediff.id, self.filediff.id,
              self.first_line)

    def get_review_url(self):
        return "%s#comment%d" % \
            (self.review.get().review_request.get_absolute_url(), self.id)

    def __unicode__(self):
        return self.text

    def truncate_text(self):
        if len(self.text) > 60:
            return self.text[0:57] + "..."
        else:
            return self.text


class ScreenshotComment(BaseComment):
    """
    A comment on a screenshot.
    """
    screenshot = models.ForeignKey(Screenshot, verbose_name=_('screenshot'),
                                   related_name="comments")
    reply_to = models.ForeignKey('self', blank=True, null=True,
                                 related_name='replies',
                                 verbose_name=_("reply to"))
    timestamp = models.DateTimeField(_('timestamp'), default=datetime.now)
    text = models.TextField(_('comment text'))

    # This is a sub-region of the screenshot.  Null X indicates the entire
    # image.
    x = models.PositiveSmallIntegerField(_("sub-image X"), null=True)
    y = models.PositiveSmallIntegerField(_("sub-image Y"))
    w = models.PositiveSmallIntegerField(_("sub-image width"))
    h = models.PositiveSmallIntegerField(_("sub-image height"))

    # Set this up with a ConcurrencyManager to help prevent race conditions.
    objects = ConcurrencyManager()

    def public_replies(self, user=None):
        """
        Returns a list of public replies to this comment, optionally
        specifying the user replying.
        """
        if user:
            return self.replies.filter(Q(review__public=True) |
                                       Q(review__user=user))
        else:
            return self.replies.filter(review__public=True)

    def get_image_url(self):
        """
        Returns the URL for the thumbnail, creating it if necessary.
        """
        return crop_image(self.screenshot.image, self.x, self.y, self.w, self.h)

    def image(self):
        """
        Generates the cropped part of the screenshot referenced by this
        comment and returns the HTML markup embedding it.
        """
        return '<img src="%s" width="%s" height="%s" alt="%s" />' % \
            (self.get_image_url(), self.w, self.h, escape(self.text))

    def get_review_url(self):
        return "%s#scomment%d" % \
            (self.review.get().review_request.get_absolute_url(), self.id)

    def __unicode__(self):
        return self.text


class FileAttachmentComment(BaseComment):
    """A comment on a file attachment."""
    file_attachment = models.ForeignKey(FileAttachment,
                                        verbose_name=_('file_attachment'),
                                        related_name="comments")
    reply_to = models.ForeignKey('self', blank=True, null=True,
                                 related_name='replies',
                                 verbose_name=_("reply to"))
    timestamp = models.DateTimeField(_('timestamp'), default=datetime.now)
    text = models.TextField(_('comment text'))

    # Set this up with a ConcurrencyManager to help prevent race conditions.
    objects = ConcurrencyManager()

    def public_replies(self, user=None):
        """
        Returns a list of public replies to this comment, optionally
        specifying the user replying.
        """
        if user:
            return self.replies.filter(Q(review__public=True) |
                                       Q(review__user=user))
        else:
            return self.replies.filter(review__public=True)

    def get_file(self):
        """
        Generates the file referenced by this
        comment and returns the HTML markup embedding it.
        """
        return '<a href="%s" alt="%s" />' % (self.file_attachment.file,
                                             escape(self.text))

    def get_review_url(self):
        return "%s#fcomment%d" % \
            (self.review.get().review_request.get_absolute_url(), self.id)

    def __unicode__(self):
        return self.text


class Review(models.Model):
    """
    A review of a review request.
    """
    review_request = models.ForeignKey(ReviewRequest,
                                       related_name="reviews",
                                       verbose_name=_("review request"))
    user = models.ForeignKey(User, verbose_name=_("user"),
                             related_name="reviews")
    timestamp = models.DateTimeField(_('timestamp'), default=datetime.now)
    public = models.BooleanField(_("public"), default=False)
    ship_it = models.BooleanField(_("ship it"), default=False,
        help_text=_("Indicates whether the reviewer thinks this code is "
                    "ready to ship."))
    base_reply_to = models.ForeignKey(
        "self", blank=True, null=True,
        related_name="replies",
        verbose_name=_("Base reply to"),
        help_text=_("The top-most review in the discussion thread for "
                    "this review reply."))
    email_message_id = models.CharField(_("e-mail message ID"), max_length=255,
                                        blank=True, null=True)
    time_emailed = models.DateTimeField(_("time e-mailed"), null=True,
                                        default=None, blank=True)

    body_top = models.TextField(_("body (top)"), blank=True,
        help_text=_("The review text shown above the diff and screenshot "
                    "comments."))
    body_bottom = models.TextField(_("body (bottom)"), blank=True,
        help_text=_("The review text shown below the diff and screenshot "
                    "comments."))

    body_top_reply_to = models.ForeignKey(
        "self", blank=True, null=True,
        related_name="body_top_replies",
        verbose_name=_("body (top) reply to"),
        help_text=_("The review that the body (top) field is in reply to."))
    body_bottom_reply_to = models.ForeignKey(
        "self", blank=True, null=True,
        related_name="body_bottom_replies",
        verbose_name=_("body (bottom) reply to"),
        help_text=_("The review that the body (bottom) field is in reply to."))

    comments = models.ManyToManyField(Comment, verbose_name=_("comments"),
                                      related_name="review", blank=True)
    screenshot_comments = models.ManyToManyField(
        ScreenshotComment,
        verbose_name=_("screenshot comments"),
        related_name="review",
        blank=True)
    file_attachment_comments = models.ManyToManyField(
        FileAttachmentComment,
        verbose_name=_("file attachment comments"),
        related_name="review",
        blank=True)

    # XXX Deprecated. This will be removed in a future release.
    reviewed_diffset = models.ForeignKey(
        DiffSet, verbose_name="Reviewed Diff",
        blank=True, null=True,
        help_text=_("This field is unused and will be removed in a future "
                    "version."))

    # Set this up with a ReviewManager to help prevent race conditions and
    # to fix duplicate reviews.
    objects = ReviewManager()

    def get_participants(self):
        """
        Returns a list of all people who have been involved in discussing
        this review.
        """

        # This list comprehension gives us every user in every reply,
        # recursively.  It looks strange and perhaps backwards, but
        # works. We do it this way because get_participants gives us a
        # list back, which we can't stick in as the result for a
        # standard list comprehension. We could opt for a simple for
        # loop and concetenate the list, but this is more fun.
        return [self.user] + \
               [u for reply in self.replies.all()
                  for u in reply.participants]

    participants = property(get_participants)

    def __unicode__(self):
        return u"Review of '%s'" % self.review_request

    def is_reply(self):
        """
        Returns whether or not this review is a reply to another review.
        """
        return self.base_reply_to != None
    is_reply.boolean = True

    def public_replies(self):
        """
        Returns a list of public replies to this review.
        """
        return self.replies.filter(public=True)

    def get_pending_reply(self, user):
        """
        Returns the pending reply to this review owned by the specified
        user, if any.
        """
        if user.is_authenticated():
            return get_object_or_none(Review,
                                      user=user,
                                      public=False,
                                      base_reply_to=self)

        return None

    def save(self, **kwargs):
        self.timestamp = datetime.now()

        super(Review, self).save()

    def publish(self, user=None):
        """
        Publishes this review.

        This will make the review public and update the timestamps of all
        contained comments.
        """
        if not user:
            user = self.user

        self.public = True
        self.save()

        for comment in self.comments.all():
            comment.timetamp = self.timestamp
            comment.save()

        for comment in self.screenshot_comments.all():
            comment.timetamp = self.timestamp
            comment.save()

        for comment in self.file_attachment_comments.all():
            comment.timetamp = self.timestamp
            comment.save()

        # Update the last_updated timestamp on the review request.
        self.review_request.last_review_timestamp = self.timestamp
        self.review_request.save()

        # Atomicly update the shipit_count
        if self.ship_it:
            self.review_request.increment_shipit_count()

        if self.is_reply():
            reply_published.send(sender=self.__class__,
                                 user=user, reply=self)
        else:
            review_published.send(sender=self.__class__,
                                  user=user, review=self)

    def is_accessible_by(self, user):
        """
        This is a specific feature for using ReviewBoard as a grading tool
        where we let all students see all review requests, but only let the
        TAs and the students who submitted the review requests see the
        reviews themselves.

        Should probably be named something or contained in some module that
        extends the model with an appropriate name, but that is for later...
        """

        # There's a master switch on the review groups called
        # ready_for_reviews. The idea is that if a user is a member of a group
        # that doesn't have this switch set, then they can simply not see
        # reviews. This is a way for TAs to publish reviews and let all the
        # students see their reviews at once.
        for group in Group.objects.accessible(user):
            if not group.ready_for_reviews:
                return False

        # Obviously, the user who created the review request can see the
        # reviews.
        if (self.review_request.submitter == user):
            return True

        # If the requesting user is part of a review group that the user who
        # created the review is also a part of, then we let them see the
        # review.
        for group in Group.objects.accessible(self.user):
            if group.is_accessible_by(user):
                return True

        return False

    def delete(self):
        """
        Deletes this review.

        This will enforce that all contained comments are also deleted.
        """
        for comment in self.comments.all():
            comment.delete()

        for comment in self.screenshot_comments.all():
            comment.delete()

        for comment in self.file_attachment_comments.all():
            comment.delete()

        super(Review, self).delete()

    def get_absolute_url(self):
        return "%s#review%s" % (self.review_request.get_absolute_url(), self.id)

    class Meta:
        ordering = ['timestamp']
        get_latest_by = 'timestamp'
