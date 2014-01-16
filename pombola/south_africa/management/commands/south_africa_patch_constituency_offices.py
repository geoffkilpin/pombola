# This is a one-off script that patches the data originally loaded using the
# `south_africa_update_constituency_offices` command. See
# https://github.com/mysociety/pombola/issues/1115

# The original data contained many names like "Bloggs J" that a subseqeunt data
# update has improved to "Joe Bloggs". The previous script made valient attempts
# to match to the existing entries, only creating new people if that matching
# was unsuccessful

# This patch script takes the abbreviated and full names and attempts to find
# matches in the database, and update them if safe to do so. This is done in
# preference to clearing the database as it is not possible to be sure that the
# admin has not been used in the meantime to update entries.


from collections import defaultdict, namedtuple, OrderedDict
import csv
from difflib import SequenceMatcher
from itertools import chain
import json
from optparse import make_option
import os
import re
import requests
import sys
import time
import urllib
import json

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.contrib.gis.geos import Point
from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import LabelCommand, CommandError
from django.db.models import Q
from django.template.defaultfilters import slugify

from pombola.core.models import (OrganisationKind, Organisation, PlaceKind,
                         ContactKind, OrganisationRelationshipKind,
                         OrganisationRelationship, Identifier, Position,
                         PositionTitle, Person, Contact)

def tidy_person_name(name_string):

    """
    >>> tidy_person_name('Bloggs JJ')
    'JJ Bloggs'
    """

    # Strip off any phone number at the end:
    name_string = re.sub(r'[\s\d]+$', '', name_string).strip()
    # And trim any list numbers from the beginning:
    name_string = re.sub(r'^[\s\d\.]+', '', name_string)
    # Strip off some titles:
    name_string = re.sub(r'(?i)^(Min|Dep Min|Dep President|President) ', '', name_string)
    name_string = name_string.strip()
    if not name_string:
        return None

    # Move any initials to the front of the name:
    name_string = re.sub(r'^(.*?)(([A-Z] *)*)$', '\\2 \\1', name_string)
    name_string = re.sub(r'(?ms)\s+', ' ', name_string).strip()
    return name_string

VERBOSE = False

def verbose(message):
    if VERBOSE:
        print message

class Command(LabelCommand):
    """Import constituency offices"""

    help = 'Import constituency office data for South Africa'

    option_list = LabelCommand.option_list + (
        make_option(
            '--test',
            action='store_true',
            dest='test',
            help='Run any doctests for this script'),
        make_option(
            '--verbose',
            action='store_true',
            dest='verbose',
            help='Output extra information for debugging'),
        make_option(
            '--commit',
            action='store_true',
            dest='commit',
            help='Actually update the database'),)

    commit = False

    def handle_label(self, input_filename, **options):

        if options['test']:
            import doctest
            failure_count, _ = doctest.testmod(sys.modules[__name__])
            sys.exit(0 if failure_count == 0 else 1)

        if options['commit']: self.commit = True

        # Load the data from csv
        self.load_data_from_csv(input_filename)
        print json.dumps(self.deltas, indent=4, sort_keys=True)

        # Change the database entries as required
        self.change_contact_details_in_database()
        self.change_names_in_database()


    # Store all the deltas in here. Form is
    # deltas = { type: { original: replacement, ... } }

    delta_name_cols = ('Administrator', 'MP', 'MPL')
    delta_contact_detail_cols = ('Fax', 'Physical Address', 'Tel')
    # ignored for now: 'Name', 'Party Code',
    delta_all_cols = delta_name_cols + delta_contact_detail_cols
    deltas = dict([(x, {}) for x in delta_all_cols])

    def load_data_from_csv(self, input_filename):

        with open(input_filename) as fp:
            reader = csv.DictReader(fp)
            row_count = 1 # first line is headers so can't start at 0
            for row in reader:
                row_count += 1
                for col in self.delta_all_cols:
                    original, replacement = row[col].strip(), row['new ' + col].strip()

                    # Names might need splitting into tuples
                    if col in self.delta_name_cols:
                        splitter = re.compile(r' [ \d]+')
                    else:
                        splitter = re.compile(r' \s+')

                    originals = filter(None, splitter.split(original))
                    replacements = filter(None, splitter.split(replacement))
                    # print '----'
                    # print originals, replacements
                    pairs = zip(originals, replacements)
                    # print pairs

                    # check that the pairs seem reasonable
                    error_tuple = None
                    errors_found = False
                    for o,r in pairs:
                        longest_word_in_original = max(re.split(r'\W+', o), key=len).lower()
                        if longest_word_in_original not in r.lower():
                            if errors_found:
                                if error_tuple:
                                    print "-----WARNING-----", row_count, '::', error_tuple[0], '!=', error_tuple[1]
                                    error_tuple = None
                                print "-----WARNING-----", row_count, '::', o, '!=', r
                            else:
                                errors_found = True
                                error_tuple = (o,r)


                    # Add to the deltas
                    for o, r in pairs:
                            # skip lines that we can do anything with
                            if not (o and r) or o == r: continue
                            self.deltas[col][o] = r

                # if row_count > 5: break



    def change_contact_details_in_database(self):

        kind_mappings = {
            'Fax': 'fax',
            'Physical Address': 'address',
            'Tel': 'voice',
        }

        for col in self.delta_contact_detail_cols:
            for original, replacement in self.deltas[col].items():

                if col == 'Physical Address':
                    original    = original.rstrip(',') + ", South Africa"
                    replacement = replacement.rstrip(',') + ", South Africa"

                entries = Contact.objects.filter(value=original, kind__slug=kind_mappings[col])

                if entries.count():
                    for entry in entries:
                        print "Changing %s value in %s to %s" % (col, entry, replacement)

                        if self.commit:
                            entry.value = replacement
                            entry.save()

                else:
                    # If it is not in DB we can't update it. Perhaps it has
                    # already been updated?
                    if not Contact.objects.filter(value=replacement, kind__slug=kind_mappings[col]).exists():
                        print "NOT FOUND: %s contact detail not found for '%s'" % (col, original)



    def change_names_in_database(self):

        for col in self.delta_name_cols:
            for original, replacement in self.deltas[col].items():

                alternatives = self.generate_name_alternatives(original)

                for name_to_try in alternatives:
                    entries = Person.objects.filter(legal_name=original)
                    if entries.count(): break

                if entries.count():
                    for entry in entries:
                        print "Changing %s value in %s to %s" % (col, entry, replacement)

                        if self.commit:
                            entry.legal_name = replacement
                            entry.save()

                            entry.slug = slugify(replacement)
                            # Check that the slug is not already in use
                            if Person.objects.filter(slug=entry.slug).exists():
                                print "ERROR: slug '%s' already in use" % entry.slug
                            else:
                                entry.save()

                else:
                    # If it is not in DB we can't update it. Perhaps it has
                    # already been updated?
                    if not Person.objects.filter(legal_name=replacement).exists():
                        print "NOT FOUND:", col, "contact detail not found for any of ", " --- ".join(alternatives)


    def generate_name_alternatives(self, name):

        # Store all the alternatives we generate in here. Strip out duplicates
        # later.
        alternatives = []
        alternatives.append(name)

        # try with leading numbers stripped
        name = re.sub(r'^\s*\d+[\s\.]*', '', name)
        alternatives.append(name)

        # loose trailing brackets with numbers in
        name = re.sub(r'\s*\(\s*\d+\s*\)\s*', '', name)
        alternatives.append(name)

        # try with non-alphanumeric stripped
        alternatives.append( re.sub(r'[^\s\w]+', '', name).strip() )

        seen = OrderedDict([(a, True) for a in alternatives])

        return seen.keys()
