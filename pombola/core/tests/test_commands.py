# Tests for the Django management commands (i.e. those invoked with
# ./manage.py or django-admin.py).

import contextlib
from mock import patch
import sys

from pombola.core.models import (
    Contact, ContactKind, Organisation, OrganisationKind, Person,
    Position, PositionTitle
)
from pombola.slug_helpers.models import SlugRedirect

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase


# A context manager to suppress standard output, as suggested in:
# http://stackoverflow.com/a/1810086/223092

@contextlib.contextmanager
def no_stdout_or_stderr():
    save_stdout = sys.stdout
    save_stderr = sys.stderr
    class DevNull(object):
        def write(self, _): pass
    sys.stdout = DevNull()
    sys.stderr = DevNull()
    try:
        yield
    finally:
        sys.stdout = save_stdout
        sys.stderr = save_stderr


class MergePeopleCommandTest(TestCase):

    def setUp(self):
        self.person_a = Person.objects.create(
            name="Jimmy Stewart",
            slug="jimmy-stewart")
        self.person_b = Person.objects.create(
            name="James Stewart",
            slug="james-stewart")

        self.organisation_kind = OrganisationKind.objects.create(
            name="Example Organisations",
            slug="example-orgs")

        self.organisation_a = Organisation.objects.create(
            name="Organisation A",
            kind=self.organisation_kind,
            slug="organisation-a")
        self.organisation_b = Organisation.objects.create(
            name="Organisation B",
            kind=self.organisation_kind,
            slug="organisation-b")

        self.position_title = PositionTitle.objects.create(
            name="Member")

        self.position_a = Position.objects.create(
            title=self.position_title,
            person=self.person_a,
            category="other",
            organisation=self.organisation_a)
        self.position_b = Position.objects.create(
            title=self.position_title,
            person=self.person_b,
            category="other",
            organisation=self.organisation_b)

        self.phone_kind = ContactKind.objects.create(
            slug="example-phones",
            name="Phone Number")
        self.email_kind = ContactKind.objects.create(
            slug="example-emails",
            name="Email Address")

        self.contact_a = Contact.objects.create(
            content_type = ContentType.objects.get_for_model(Person),
            object_id = self.person_a.id,
            kind = self.phone_kind,
            value = '555 5555',
        )
        self.contact_a.save()
        self.contact_b = Contact.objects.create(
            content_type=ContentType.objects.get_for_model(Person),
            object_id=self.person_b.id,
            kind=self.email_kind,
            value='jimmy@example.org',
        )
        self.contact_b.save()

        self.options = {
            'keep_person': self.person_a.id,
            'delete_person': self.person_b.id,
            'sayit_id_scheme': 'org.mysociety.pombolatest',
            'quiet': True,
            'interactive': False,
        }

    def test_conflicting_dob(self):
        self.person_a.date_of_birth = "1908-05-20"
        self.person_a.save()

        self.person_b.date_of_birth = "1908-05-21"
        self.person_b.save()

        # There should be an error - this would lose the B version of
        # the date of birth:
        with self.assertRaises(CommandError):
            with no_stdout_or_stderr():
                call_command('core_merge_people', **self.options)

    def test_losing_summary(self):
        self.person_a.summary = ""
        self.person_a.date_of_birth = "1908-05-20"
        self.person_a.save()

        self.person_b.summary = "The famous actor."
        self.person_b.date_of_birth = "1908-05-20"
        self.person_b.save()

        # This should also error - it would lose the B version of the
        # summary field:
        with self.assertRaises(CommandError):
            with no_stdout_or_stderr():
                call_command('core_merge_people', **self.options)

    def test_merge_people(self):
        # This one should succeed:
        with no_stdout_or_stderr():
            call_command('core_merge_people', **self.options)

        # Check that only person_a exists any more:
        Person.objects.get(pk=self.person_a.id)
        with self.assertRaises(Person.DoesNotExist):
            Person.objects.get(pk=self.person_b.id)

        # Check that both positions are present on the kept person:
        self.assertEqual(1, Position.objects.filter(
                person=self.person_a,
                title=self.position_title,
                organisation=self.organisation_a).count())
        self.assertEqual(1, Position.objects.filter(
                person=self.person_a,
                title=self.position_title,
                organisation=self.organisation_b).count())

        # Check that there are now two contacts for the kept person:
        contacts = Contact.objects.filter(
            content_type=ContentType.objects.get_for_model(Person),
            object_id=self.person_a.id).order_by('value')
        self.assertEqual(2, contacts.count())
        self.assertEqual(contacts[0].value, '555 5555')
        self.assertEqual(contacts[1].value, 'jimmy@example.org')

    def test_merge_with_slugs(self):
        options = {
            'keep_person': self.person_a.slug,
            'delete_person': self.person_b.slug,
            'sayit_id_scheme': 'org.mysociety.pombolatest',
            'quiet': True,
            'interactive': False
        }
        with no_stdout_or_stderr():
            call_command('core_merge_people', **options)

        # Check that only person_a exists any more:
        Person.objects.get(pk=self.person_a.id)
        with self.assertRaises(Person.DoesNotExist):
            Person.objects.get(pk=self.person_b.id)

    @patch('__builtin__.raw_input', return_value='n')
    def test_no_continue(self, mock_input):
        options = {
            'keep_person': self.person_a.id,
            'delete_person': self.person_b.id,
            'sayit_id_scheme': 'org.mysociety.pombolatest',
            'quiet': True
        }
        with self.assertRaises(CommandError):
            with no_stdout_or_stderr():
                call_command('core_merge_people', **options)

        # Check that nothing was deleted:
        Person.objects.get(pk=self.person_a.id)
        Person.objects.get(pk=self.person_b.id)

    def test_inputs_are_the_same(self):
        options = {
            'keep_person': self.person_a.id,
            'delete_person': self.person_a.slug,
            'sayit_id_scheme': 'org.mysociety.pombolatest',
            'quiet': True
        }

        with self.assertRaises(CommandError):
            with no_stdout_or_stderr():
                call_command('core_merge_people', **options)
