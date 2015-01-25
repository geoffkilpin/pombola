from collections import defaultdict
import datetime
import warnings
import re

from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.http import Http404
from django.db.models import Count
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import redirect
from django.core.urlresolvers import reverse
from django.views.generic import RedirectView, TemplateView
from django.shortcuts import get_object_or_404
from django import forms
from django.utils.translation import ugettext_lazy as _
from django.db.models import Q
from django.contrib.contenttypes.models import ContentType

import mapit
from haystack.query import SearchQuerySet, SQ
from haystack.inputs import AutoQuery
from haystack.forms import SearchForm

from popit.models import Person as PopitPerson
from speeches.models import Section, Speech, Speaker, Tag
from speeches.views import NamespaceMixin, SpeechView, SectionView

from pombola.core import models
from pombola.core.views import (HomeView, BasePlaceDetailView, PlaceDetailView,
    PlaceDetailSub, OrganisationDetailView, PersonDetail, PlaceDetailView,
    OrganisationDetailSub, PersonSpeakerMappingsMixin)
from pombola.info.models import InfoPage, Category
from pombola.info.views import InfoPageView
from pombola.search.views import GeocoderView, SearchBaseView
from pombola.slug_helpers.views import SlugRedirect

from pombola.south_africa.models import ZAPlace

from pombola.interests_register.models import Release, Category, Entry, EntryLineItem
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django_date_extensions.fields import ApproximateDateField, ApproximateDate

# In the short term, until we have a list of constituency offices and
# addresses from DA, let's bundle these together.
CONSTITUENCY_OFFICE_PLACE_KIND_SLUGS = (
    'constituency-office',
    'constituency-area', # specific to DA party
)

class SAHomeView(HomeView):

    def get_context_data(self, **kwargs):
        context = super(SAHomeView, self).get_context_data(**kwargs)
        articles = InfoPage.objects.filter(
            kind=InfoPage.KIND_BLOG).order_by("-publication_date")

        articles_for_front_page = \
            InfoPage.objects.filter(
                categories__slug__in=(
                    'week-parliament',
                    'impressions'
                )
            ).order_by('-publication_date')

        context['article_columns'] = [
            articles_for_front_page[0:3],
            articles_for_front_page[3:6],
        ]

        context['other_news_categories'] = []
        for slug in ('advocacy-campaigns', 'commentary', 'mp-corner'):
            try:
                c = Category.objects.get(slug=slug)
                context['other_news_categories'].append(
                    (c, articles.filter(categories=c)[:1]))
            except Category.DoesNotExist:
                pass

        # If there is editable homepage content make it available to the templates.
        try:
            page = InfoPage.objects.get(slug="homepage-quote")
            context['quote_content'] = page.content
        except InfoPage.DoesNotExist:
            context['quote_content'] = None

        return context

class SAGeocoderView(GeocoderView):

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        results = context.get('geocoder_results')
        if results is not None and len(results) == 1:
            result = results[0]
            redirect_url = reverse('latlon', kwargs={
                'lat': result['latitude'],
                'lon': result['longitude']})
            return redirect(redirect_url)
        else:
            return self.render_to_response(context)

class LocationSearchForm(SearchForm):
    q = forms.CharField(required=False, label=_('Search'), widget=forms.TextInput(attrs={'placeholder': 'Your location'}))

class LatLonDetailBaseView(BasePlaceDetailView):

    # Using 25km as the default, as that's what's used on MyReps.
    constituency_office_search_radius = 25

    # The codes used here should match the party slugs, and the names of the
    # icon files in .../static/images/party-map-icons/
    party_slugs_that_have_logos = set((
        'adcp', 'anc', 'apc', 'azapo', 'cope', 'da', 'ff', 'id', 'ifp', 'mf',
        'pac', 'sacp', 'ucdp', 'udm'
    ));

    def get_object(self):
        # FIXME - handle bad args better.
        lat = float(self.kwargs['lat'])
        lon = float(self.kwargs['lon'])

        self.location = Point(lon, lat)

        areas = mapit.models.Area.objects.by_location(self.location)

        try:
            # FIXME - Handle getting more than one province.
            province = models.Place.objects.get(mapit_area__in=areas, kind__slug='province')
        except models.Place.DoesNotExist:
            raise Http404

        return province

    def get_context_data(self, **kwargs):
        context = super(LatLonDetailBaseView, self).get_context_data(**kwargs)
        context['location'] = self.location

        context['office_search_radius'] = self.constituency_office_search_radius

        context['nearest_offices'] = nearest_offices = (
            ZAPlace.objects
            .filter(kind__slug__in=CONSTITUENCY_OFFICE_PLACE_KIND_SLUGS)
            .distance(self.location)
            .filter(location__distance_lte=(self.location, D(km=self.constituency_office_search_radius)))
            .order_by('distance')
            )

        #exclude non-active offices
        #FIXME - this can probably be better implemented by a
        #organisation_currently_active() filter for places
        for office in nearest_offices:
            if not office.organisation.is_ongoing():
                context['nearest_offices'] = nearest_offices = nearest_offices.exclude(id=office.id)

        # FIXME - There must be a cleaner way/place to do this.
        for office in nearest_offices:
            try:
                cc_positions = office \
                    .organisation \
                    .position_set \
                    .filter(
                        person__position__title__slug='constituency-contact',
                    ) \
                    .currently_active()

                constituency_contacts = models.Person.objects.filter(position__in=cc_positions)
                office_people_entries = []

                for constituency_contact in constituency_contacts:

                    # Find positions for this person that are relevant to the office.
                    positions = constituency_contact.position_set \
                        .filter(
                            organisation__slug__in = [
                                "national-assembly",
                                office.organisation.slug,
                            ]
                        ) \
                        .currently_active()

                    office_people_entries.append({
                        'person': constituency_contact,
                        'positions': positions
                    })

                if len(office_people_entries):
                    office.office_people_entries = office_people_entries

            except models.Person.DoesNotExist:
                warnings.warn("{0} has no MPs".format(office.organisation))

            # Try to extract the political membership of this person and store
            # it next to the office. TODO - deal with several parties sharing
            # an office (should this happen?) and MPs with no party connection.
            if hasattr(office, 'office_people_entries'):
                for entry in office.office_people_entries:
                    try:
                        party_slug = entry['person'].position_set.filter(title__slug="member", organisation__kind__slug="party")[0].organisation.slug
                        if party_slug in self.party_slugs_that_have_logos:
                            office.party_slug_for_icon = party_slug
                    except IndexError:
                        warnings.warn("{0} has no party membership".format(entry['person']))

        context['form'] = LocationSearchForm()

        context['politicians'] = (self.object
            .all_related_current_politicians()
            .filter(position__organisation__slug='national-assembly')
        )

        return context


class LatLonDetailNationalView(LatLonDetailBaseView):
    template_name = 'south_africa/latlon_national_view.html'


class LatLonDetailLocalView(LatLonDetailBaseView):
    template_name = 'south_africa/latlon_local_view.html'



class SAPlaceDetailView(PlaceDetailView):

    def get_context_data(self, **kwargs):
        """
        Get back the people for this place in separate lists so they can
        be displayed separately on the place detail page.
        """
        context = super(SAPlaceDetailView, self).get_context_data(**kwargs)

        for context_string, position_filter in (
                ('national_assembly_people', {'organisation__slug': 'national-assembly'}),
                ('ncop_people', {'organisation__slug': 'ncop'}),
                ('legislature_people', {'organisation__kind__slug': 'provincial-legislature'}),
        ):
            all_member_positions = self.object.all_related_positions(). \
                filter(title__slug='member', **position_filter).select_related('person')
            current_positions = all_member_positions.currently_active()
            current_people = models.Person.objects.filter(position__in=current_positions).distinct()
            former_positions = all_member_positions.currently_inactive()

            context[context_string + '_count'] = current_people.count()
            context[context_string] = current_people
            context['former_' + context_string] = models.Person.objects.filter(
                position__in=former_positions
            ).distinct()

        context['other_people'] = (
            models.Person.objects
              .filter(position__place=self.object)
              .exclude(id__in=context['national_assembly_people'])
              .exclude(id__in=context['former_national_assembly_people'])
              .exclude(id__in=context['ncop_people'])
              .exclude(id__in=context['former_ncop_people'])
              .exclude(id__in=context['legislature_people'])
              .exclude(id__in=context['former_legislature_people'])
        )
        return context


class SAPlaceDetailSub(PlaceDetailSub):
    child_place_template = "south_africa/constituency_office_list_item.html"
    child_place_list_template = "south_africa/constituency_office_list.html"

    def get_context_data(self, **kwargs):
        context = super(SAPlaceDetailSub, self).get_context_data(**kwargs)

        context['child_place_template'] = self.child_place_template
        context['child_place_list_template'] = self.child_place_list_template
        context['subcontent_title'] = 'Constituency Offices'

        if self.object.kind.slug == 'province':
            context['child_places'] = (
                ZAPlace.objects
                .filter(kind__slug__in=CONSTITUENCY_OFFICE_PLACE_KIND_SLUGS)
                .filter(location__coveredby=self.object.mapit_area.polygons.collect())
                )

        return context

def key_position_sort_last_name(position):
    """Take a position and return a tuple for sorting it with.

    This is intended for use as the key attribute of .sort() or
    sorted() when sorting positions, so that the positions are sorted
    by the appropriate name of the associated person. It also needs to sort by
    the person's id so as to avoid mixing up people with the similar names,
    and additionally, it puts positions which are membership
    of a parliament at the top of the group of positions for a single
    person.
    """

    org_kind_slug = position.organisation.kind.slug
    title_slug = position.title and position.title.slug
    is_parliamentary = org_kind_slug == 'parliament'
    is_member = title_slug in ('member', 'delegate')

    return (
        position.person.sort_name,
        position.person.id,
        # False if the position is member or delegate to a parliament
        # (False sorts to before True)
        not(is_parliamentary and is_member),
        )

class SAOrganisationDetailView(OrganisationDetailView):

    def get_context_data(self, **kwargs):
        context = super(SAOrganisationDetailView, self).get_context_data(**kwargs)

        if self.object.kind.slug == 'parliament':
            self.add_parliament_counts_to_context_data(context)

        # Sort the list of positions in an organisation by an approximation
        # of their holder's last name.
        context['positions'] = sorted(
            context['positions'].currently_active(),
            key=key_position_sort_last_name,
        )

        return context

    def add_parliament_counts_to_context_data(self, context):
        # Get all the currently active positions in the house:
        positions_in_house = models.Position.objects.filter(
            organisation=self.object). \
            select_related('person').currently_active()
        # Then find the distinct people who have those positions:
        people_in_house = set(p.person for p in positions_in_house)
        # Now find all the active party memberships for those people:
        party_counts = defaultdict(int)
        for current_party_position in models.Position.objects.filter(
            title__slug='member',
            organisation__kind__slug='party',
            person__in=people_in_house).currently_active(). \
            select_related('organisation'):
            party_counts[current_party_position.organisation] += 1
        parties = sorted(party_counts.keys(), key=lambda o: o.name)
        total_people = len(people_in_house)

        # Calculate the % of the house each party occupies.
        context['parties_and_percentages'] = [
            (party, (float(party_counts[party]) * 100) / total_people)
            for party in parties]

        context['total_people'] =  total_people

    def get_template_names(self):
        if self.object.kind.slug == 'parliament':
            return [ 'south_africa/organisation_house.html' ]
        else:
            return super(SAOrganisationDetailView, self).get_template_names()


class SAOrganisationDetailSub(OrganisationDetailSub):
    sub_page = None

    def add_sub_page_context(self, context):
        pass

    def get_context_data(self, *args, **kwargs):
        context = super(SAOrganisationDetailSub, self).get_context_data(*args, **kwargs)

        self.add_sub_page_context(context)

        context['sorted_positions'] = sorted(
                context['sorted_positions'],
                key=key_position_sort_last_name)

        return context


class SAOrganisationDetailSubParty(SAOrganisationDetailSub):
    sub_page = 'party'

    def add_sub_page_context(self, context):

        context['party'] = get_object_or_404(models.Organisation,slug=self.kwargs['sub_page_identifier'])

        # We need to find any position where someone was a member
        # of the organisation and that membership overlapped with
        # their membership of the party, and mark whether that
        # time includes the current date.
        current_date = str(datetime.date.today())
        params = [current_date, current_date, current_date, current_date,
            self.object.id, context['party'].id]
        # n.b. "hp" = "house position", "pp" = "party position"
        all_positions = models.raw_query_with_prefetch(
            models.Position,
            '''
SELECT
    hp.*,
    (hp.sorting_start_date <= %s AND
     hp.sorting_end_date_high >= %s AND
     pp.sorting_start_date <= %s AND
     pp.sorting_end_date_high >= %s) AS current
FROM core_position hp, core_position pp
  WHERE hp.person_id = pp.person_id
    AND hp.organisation_id = %s
    AND pp.organisation_id = %s
    AND hp.sorting_start_date <= pp.sorting_end_date_high
    AND pp.sorting_start_date <= hp.sorting_end_date_high
''',
            params,
            (('person', ('alternative_names', 'images')),
             ('place', ()),
             ('organisation', ()),
             ('title', ())))

        current_person_ids = set(p.person_id for p in all_positions if p.current)

        if self.request.GET.get('all'):
            context['sorted_positions'] = all_positions

        elif self.request.GET.get('historic'):
            context['historic'] = True
            context['sorted_positions'] = [
                p for p in all_positions if p.person_id not in current_person_ids
            ]
        else:
            # Otherwise we're looking current positions:
            context['historic'] = True
            context['sorted_positions'] = [
                p for p in all_positions if p.current
            ]


class SAOrganisationDetailSubPeople(SAOrganisationDetailSub):
    sub_page = 'people'

    def add_sub_page_context(self, context):
        all_positions = self.object.position_set.all()
        context['office_filter'] = False
        context['historic_filter'] = False
        context['all_filter'] = False
        context['current_filter'] = False

        if self.request.GET.get('all'):
            context['all_filter'] = True
            context['sorted_positions'] = all_positions
        elif self.request.GET.get('historic') and not self.request.GET.get('office'):
            context['historic_filter'] = True
            #FIXME - limited to members and delegates so that current members who are no longer officials are not displayed, but this
            #means that if a former member was an official this is not shown
            context['sorted_positions'] = all_positions.filter(Q(title__slug='member') | Q(title__slug='delegate')).currently_inactive()
        elif self.request.GET.get('historic'):
            context['historic_filter'] = True
            context['sorted_positions'] = all_positions.currently_inactive()
        else:
            context['current_filter'] = True
            context['sorted_positions'] = all_positions.currently_active()

        if self.request.GET.get('office'):
            context['office_filter'] = True
            context['current_filter'] = False
            context['sorted_positions'] = context['sorted_positions'].exclude(title__slug='member').exclude(title__slug='delegate')

        if self.object.slug == 'ncop':
            context['membertitle'] = 'delegate'
        else:
            context['membertitle'] = 'member'

class SAPersonDetail(PersonSpeakerMappingsMixin, PersonDetail):

    important_organisations = ('ncop', 'national-assembly', 'national-executive')

    def get_recent_speeches_for_section(self, section_title, limit=5):
        pombola_person = self.object
        sayit_speaker = self.pombola_person_to_sayit_speaker(
            pombola_person,
            'za.org.pa.www'
        )

        if not sayit_speaker:
            # Without a speaker we can't find any speeches
            return Speech.objects.none()

        try:
            # Add parent=None as the title is not unique, hopefully the top level will be.
            sayit_section = Section.objects.get(title=section_title, parent=None)
        except Section.DoesNotExist:
            # No match. Don't raise exception but do produce a warning and then return an empty queryset
            warnings.warn("Could not find top level sayit section '{0}'".format(section_title))
            return Speech.objects.none()

        speeches = (
            sayit_section.descendant_speeches()
                .filter(speaker=sayit_speaker)
                .order_by('-start_date', '-start_time'))

        if limit:
            speeches = speeches[:limit]

        return speeches


    def get_tabulated_interests(self):
        interests = self.object.interests_register_entries.all()
        tabulated = {}

        for entry in interests:
            release = entry.release
            category = entry.category

            if release.id not in tabulated:
                tabulated[release.id] = {'name':release.name, 'categories':{}}

            if category.id not in tabulated[release.id]['categories']:
                tabulated[release.id]['categories'][category.id] = {
                    'name': category.name,
                    'headings': [],
                    'headingindex': {},
                    'headingcount': 1,
                    'entries': []
                }

            #create row list
            tabulated[release.id]['categories'][category.id]['entries'].append(
                ['']*(tabulated[entry.release.id]['categories'][entry.category.id]['headingcount']-1)
            )

            #loop through each 'cell' in the row
            for entrylistitem in entry.line_items.all():
                #if the heading for the column does not yet exist, create it
                if entrylistitem.key not in tabulated[entry.release.id]['categories'][entry.category.id]['headingindex']:
                    tabulated[release.id]['categories'][category.id]['headingindex'][entrylistitem.key] = tabulated[entry.release.id]['categories'][entry.category.id]['headingcount']-1
                    tabulated[release.id]['categories'][category.id]['headingcount']+=1
                    tabulated[release.id]['categories'][category.id]['headings'].append(entrylistitem.key)

                    #loop through each row that already exists to ensure lists are the same size
                    for (key, line) in enumerate(tabulated[release.id]['categories'][category.id]['entries']):
                        tabulated[entry.release.id]['categories'][entry.category.id]['entries'][key].append('')

                #record the 'cell' in the correct position in the row list
                tabulated[release.id]['categories'][category.id]['entries'][-1][tabulated[release.id]['categories'][category.id]['headingindex'][entrylistitem.key]] = entrylistitem.value

        return tabulated

    def list_contacts(self, kind_slugs):
        return self.object.contacts.filter(kind__slug__in=kind_slugs).values_list(
            'value', flat=True)

    def get_context_data(self, **kwargs):
        context = super(SAPersonDetail, self).get_context_data(**kwargs)
        context['twitter_contacts'] = self.list_contacts(('twitter',))
        # The email attribute of the person might also be duplicated
        # in a contact of type email, so create a set of email
        # addresses:
        context['email_contacts'] = set(self.list_contacts(('email',)))
        if self.object.email:
            context['email_contacts'].add(self.object.email)
        context['phone_contacts'] = self.list_contacts(('cell', 'voice'))
        context['fax_contacts'] = self.list_contacts(('fax',))
        context['address_contacts'] = self.list_contacts(('address',))
        context['positions'] = self.object.politician_positions().filter(organisation__slug__in=self.important_organisations)

        # FIXME - the titles used here will need to be checked and fixed.
        context['hansard']   = self.get_recent_speeches_for_section("Hansard")
        context['committee'] = self.get_recent_speeches_for_section("Committee Minutes")
        context['question']  = self.get_recent_speeches_for_section("Questions")

        context['interests'] = self.get_tabulated_interests()

        return context


class SASearchView(SearchBaseView):

    def __init__(self, *args, **kwargs):
        super(SASearchView, self).__init__(*args, **kwargs)
        del self.search_sections['speeches']
        self.search_sections['questions'] = {
            'model': Speech,
            'title': 'Questions and Answers',
            'filter': {
                'args': [SQ(tags='question') | SQ(tags='answer')],
            }
        }
        self.search_sections['committee'] = {
            'model': Speech,
            'title': 'Committee Appearance',
            'filter': {
                'kwargs': {
                    'tags': 'committee'
                }
            }
        }
        self.search_sections['hansard'] = {
            'model': Speech,
            'title': 'Hansard',
            'filter': {
                'kwargs': {
                    'tags': 'hansard'
                }
            }
        }
        self.section_ordering.remove('speeches')
        self.section_ordering += [
            'questions', 'committee', 'hansard'
        ]


class SANewsletterPage(InfoPageView):
    template_name = 'south_africa/info_newsletter.html'

class SASpeakerRedirectView(RedirectView):

    # see also SAPersonDetail for mapping in opposite direction
    def get_redirect_url(self, *args, **kwargs):
        try:
            slug = kwargs['slug']
            speaker = Speaker.objects.get(slug=slug)
            popit_id = speaker.person.popit_id
            scheme, primary_key = re.match('(.*?)/core_person/(\d+)$', popit_id).groups()
            person = models.Person.objects.get(id=primary_key)
            return reverse('person', args=(person.slug,))
        except Exception as e:
            raise Http404

class SASpeechesIndex(NamespaceMixin, TemplateView):
    template_name = 'south_africa/hansard_index.html'
    top_section_name='Hansard'
    sections_to_show = 25
    section_parent_field = 'section__parent__parent__parent__parent__parent'

    def get_context_data(self, **kwargs):
        context = super(SASpeechesIndex, self).get_context_data(**kwargs)

        # Get the top level section, or 404
        top_section = get_object_or_404(Section, title=self.top_section_name, parent=None)
        context['show_lateness_warning'] = (self.top_section_name == 'Hansard')

        # As we know that the hansard section structure is
        # "Hansard" -> yyyy -> mm -> dd -> section -> subsection -> [speeches]
        # we can create a very specific query to drill up to the top level one
        # that we want.

        section_parent_filter = { self.section_parent_field : top_section }
        entries = Speech \
            .objects \
            .filter(**section_parent_filter) \
            .values('section_id', 'start_date') \
            .annotate(speech_count=Count('id')) \
            .order_by('-start_date')

        # loop through and add all the section objects. This is not efficient,
        # but makes the templates easier as we can (for example) use get_absolute_url.
        # Also lets us retrieve the last N parent sections which is what we need for the
        # display.
        parent_sections = set()
        display_entries = []
        for entry in entries:
            section = Section.objects.get(pk=entry['section_id'])
            parent_sections.add(section.parent.id)
            if len(parent_sections) > self.sections_to_show:
                break
            display_entries.append(entry)
            display_entries[-1]['section'] = section

        # PAGINATION NOTE - it would be possible to add pagination to this by simply
        # removing the `break` after self.sections_to_show has been reached and then
        # finding a more efficient way to inflate the sections (perhaps using an
        # embedded lambda, or a custom templatetag). However paginating this page may
        # not be as useful as creating an easy to use drill down based on date, or
        # indeed using search.

        context['entries'] = display_entries
        return context

class SAHansardIndex(SASpeechesIndex):
    template_name = 'south_africa/hansard_index.html'
    top_section_name='Hansard'
    section_parent_field = 'section__parent__parent__parent__parent__parent'
    sections_to_show = 25

class SACommitteeIndex(SASpeechesIndex):
    template_name = 'south_africa/hansard_index.html'
    top_section_name='Committee Minutes'
    section_parent_field = 'section__parent__parent__parent'
    sections_to_show = 25

def questions_section_sort_key(section):
    """This function helps to sort question sections

    The intention is to have questions for the President first, then
    Deputy President, then offices associated with the presidency,
    then questions to ministers sorted by the name of the ministry,
    and finally anything else sorted just on the title.

    >>> questions_section_sort_key(Section(title="for the President"))
    'AAAfor the President'
    >>> questions_section_sort_key(Section(title="ask the deputy president"))
    'BBBask the deputy president'
    >>> questions_section_sort_key(Section(title="about the Presidency"))
    'CCCabout the Presidency'
    >>> questions_section_sort_key(Section(title="Questions asked to Minister for Foo"))
    'DDDFoo'
    >>> questions_section_sort_key(Section(title="Questions asked to the Minister for Bar"))
    'DDDBar'
    >>> questions_section_sort_key(Section(title="Questions asked to Minister of Baz"))
    'DDDBaz'
    >>> questions_section_sort_key(Section(title="Minister of Quux"))
    'DDDQuux'
    >>> questions_section_sort_key(Section(title="Random"))
    'MMMRandom'
    """
    title = section.title
    if re.search(r'(?i)Deputy\s+President', title):
        return "BBB" + title
    if re.search(r'(?i)President', title):
        return "AAA" + title
    if re.search(r'(?i)Presidency', title):
        return "CCC" + title
    stripped_title = title
    for regexp in (r'^(?i)Questions\s+asked\s+to\s+(the\s+)?Minister\s+(of|for(\s+the)?)\s+',
                   r'^(?i)Minister\s+(of|for(\s+the)?)\s+'):
        stripped_title = re.sub(regexp, '', stripped_title)
    if stripped_title == title:
        # i.e. it wasn't questions for a minister
        return "MMM" + title
    return "DDD" + stripped_title

class SAQuestionIndex(SASpeechesIndex):
    template_name = 'south_africa/question_index.html'
    top_section_name='Questions'

    def get_context_data(self, **kwargs):
        context = super(SASpeechesIndex, self).get_context_data(**kwargs)

        # Get the top level section, or 404
        top_section = get_object_or_404(Section, title=self.top_section_name, parent=None)

        # the question section structure is
        # "Questions" -> "Questions asked to Minister for X" -> "Date" ...

        sections = Section \
            .objects \
            .filter(parent=top_section) \
            .annotate(speech_count=Count('children__speech__id'))

        context['entries'] = sorted(sections,
                                    key=questions_section_sort_key)
        return context


class OldSpeechRedirect(RedirectView):

    """Redirects from an old speech URL to the current one"""

    def get_redirect_url(self, *args, **kwargs):
        try:
            speech_id = int(kwargs['pk'])
            speech = Speech.objects.get(pk=speech_id)
            return reverse(
                'speeches:speech-view',
                args=[speech_id])
        except (ValueError, Speech.DoesNotExist):
            raise Http404


class OldSectionRedirect(RedirectView):

    """Redirects from an old section URL to the current one"""

    def get_redirect_url(self, *args, **kwargs):
        try:
            section_id = int(kwargs['pk'])
            section = Section.objects.get(pk=section_id)
            return reverse(
                'speeches:section-view',
                args=[section.get_path])
        except (ValueError, Section.DoesNotExist):
            raise Http404


class SAPersonAppearanceView(PersonSpeakerMappingsMixin, TemplateView):

    template_name = 'south_africa/person_appearances.html'

    def get_context_data(self, **kwargs):
        context = super(SAPersonAppearanceView, self).get_context_data(**kwargs)

        # Extract slug and tag provided on url
        person_slug = self.kwargs['person_slug']
        speech_tag  = self.kwargs['speech_tag']

        # Find (or 404) matching objects
        person = get_object_or_404(models.Person, slug=person_slug)
        tag    = get_object_or_404(Tag, name=speech_tag)

        # SayIt speaker is different to core.Person, Load the speaker
        speaker = self.pombola_person_to_sayit_speaker(
            person,
            'za.org.pa.www'
        )

        # Load the speeches. Pagination is done in the template
        speeches = Speech.objects \
            .filter(tags=tag, speaker=speaker) \
            .order_by('-start_date', '-start_time')

        # Store person as 'object' for the person_base.html template
        context['object']  = person
        context['speeches'] = speeches

        # Add a hardcoded section-view url name to use for the speeches. Would
        # rather this was not hardcoded here but seems hard to avoid.
        # speech_tag not known. Use 'None' for template default instead
        context['section_url'] = None

        return context


def should_redirect_to_source(section):
    # If this committee is descended from the Committee Minutes
    # section, redirect to the source transcript on the PMG website:
    root_section = section.get_ancestors[0]
    return root_section.slug == 'committee-minutes'


class SASpeechView(SpeechView):

    def get(self, request, *args, **kwargs):
        speech = self.object = self.get_object()
        try_redirect = should_redirect_to_source(speech.section)
        if try_redirect and speech.source_url:
            return redirect(speech.source_url)
        context = self.get_context_data(object=speech)
        return self.render_to_response(context)

    def get_queryset(self):
        return Speech.objects.all()

class SASectionView(SectionView):

    def get(self, request, *args, **kwargs):
        try:
            self.object = self.get_object()
        except Http404:
            #if not found check if any sections have been redirected by
            #considering the deepest section first
            full_slug = self.kwargs.get('full_slug', None)
            slugs = full_slug.split('/')
            for i in range(len(slugs), 0, -1):
                print i
                try:
                    check_slug = '/'.join(slugs[:i])
                    sr = SlugRedirect.objects.get(
                        content_type=ContentType.objects.get_for_model(Section),
                        old_object_slug=check_slug
                    )
                    new_url = '/' + full_slug.replace(
                        check_slug,
                        sr.new_object.get_path,
                        1)
                    break
                except SlugRedirect.DoesNotExist:
                    pass

            try:
                return redirect(new_url)
            except NameError:
                raise Http404

        if should_redirect_to_source(self.object):
            # Find a URL to redirect to; try to get any speech in the
            # section with a non-blank source URL. FIXME: after
            # switching to Django 1.6, .first() will make this simpler.
            speeches = self.object.speech_set.exclude(source_url='')[:1]
            if speeches:
                speech = speeches[0]
                redirect_url = speech.source_url
                if redirect_url:
                    return redirect(redirect_url)
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

class SAElectionOverviewMixin(TemplateView):
    election_type = ''

    def get_context_data(self, **kwargs):
        context = super(SAElectionOverviewMixin, self).get_context_data(**kwargs)

        # XXX Does this need to only be parties standing in the election?
        party_kind = models.OrganisationKind.objects.get(slug='party')
        context['party_list'] = models.Organisation.objects.filter(
            kind=party_kind).order_by('name')

        province_kind = models.PlaceKind.objects.get(slug='province')
        context['province_list'] = models.Place.objects.filter(
            kind=province_kind).order_by('name')

        election_year = self.kwargs['election_year']

        # All lists of candidates share this kind
        election_list = models.OrganisationKind.objects.get(slug='election-list')

        context['election_year'] = election_year

        # Build the right election list names
        election_list_suffix = '-national-election-list-' + election_year

        # All the running parties live in this list
        running_parties = []

        # Find the slugs of all parties taking part in the national election
        national_running_party_lists = models.Organisation.objects.filter(
            kind=election_list,
            slug__endswith=election_list_suffix
        ).order_by('name')

        # Loop through national sets and extract slugs
        for l in national_running_party_lists:
            party_slug = l.slug.replace(election_list_suffix, '')
            if not party_slug in running_parties:
                running_parties.append(party_slug)

        # I am so sorry for this.
        context['running_party_list'] = []
        for running_party in running_parties:
            context['running_party_list'].append(models.Organisation.objects.get(slug=running_party))

        return context

class SAElectionOverviewView(SAElectionOverviewMixin):
    template_name = 'south_africa/election/overview.html'

class SAElectionStatisticsView(SAElectionOverviewMixin):
    template_name = 'south_africa/election/statistics.html'
    election_type = 'statistics'

    def get_context_data(self, **kwargs):
        context = super(SAElectionStatisticsView, self).get_context_data(**kwargs)

        context['current_mps'] = {'all': {}, 'byparty': []}

        context['current_mps']['all']['current'] = models.Organisation.objects.get(slug='national-assembly').position_set.all().currently_active().distinct('person__id').count()
        context['current_mps']['all']['rerunning'] = models.Organisation.objects.get(slug='national-assembly').position_set.all().currently_active().filter(Q(person__position__organisation__slug__contains='national-election-list-2014') | (Q(person__position__organisation__slug__contains='election-list-2014') & Q(person__position__organisation__slug__contains='regional'))).distinct('person__id').count()
        context['current_mps']['all']['percent_rerunning'] = 100 * context['current_mps']['all']['rerunning'] / context['current_mps']['all']['current']

        #find the number of current MPs running for office per party
        for p in context['party_list']:
            party = models.Organisation.objects.get(slug=p.slug)
            current = models.Organisation.objects.get(slug='national-assembly').position_set.all().currently_active().filter(person__position__organisation__slug=p.slug).distinct('person__id').count()
            rerunning = models.Organisation.objects.get(slug='national-assembly').position_set.all().currently_active().filter(person__position__organisation__slug=p.slug).filter(Q(person__position__organisation__slug__contains='national-election-list-2014') | (Q(person__position__organisation__slug__contains='election-list-2014') & Q(person__position__organisation__slug__contains='regional'))).distinct('person__id').count()
            if current:
                percent = 100 * rerunning / current
                context['current_mps']['byparty'].append({'party': party, 'current': current, 'rerunning': rerunning, 'percent_rerunning': percent})


        #find individuals who appear to have switched party
        context['people_new_party'] = []
        people = models.Person.objects.filter(position__organisation__kind__slug='party', position__title__slug='member').annotate(num_parties=Count('position')).filter(num_parties__gt=1)

        for person in people:
            #check whether the person is a candidate - there is probably be a cleaner way to do this in the initial query
            person_list = person.position_set.all().filter(organisation__slug__contains='election-list-2014')
            if person_list:
                context['people_new_party'].append({
                    'person': person,
                    'current_positions': person.position_set.all().filter(organisation__kind__slug='party').currently_active(),
                    'former_positions': person.position_set.all().filter(organisation__kind__slug='party').currently_inactive(),
                    'person_list': person_list
                })

        return context

class SAElectionNationalView(SAElectionOverviewMixin):
    template_name = 'south_africa/election/national.html'
    election_type = 'national'

class SAElectionProvincialView(SAElectionOverviewMixin):
    template_name = 'south_africa/election/provincial.html'
    election_type = 'provincial'

class SAElectionPartyCandidatesView(TemplateView):

    template_name = 'south_africa/election_candidates_party.html'

    election_type = 'national'

    def get_context_data(self, **kwargs):
        context = super(SAElectionPartyCandidatesView, self).get_context_data(**kwargs)

        # These are common bits
        election_year = self.kwargs['election_year']
        election_type = self.election_type

        context['election_year'] = election_year
        context['election_type'] = election_type

        # Details from the URI
        if 'party_name' in self.kwargs:
            party_name = self.kwargs['party_name']
        else:
            party_name = None

        if 'province_name' in self.kwargs:
            province_name = self.kwargs['province_name']
            # Also get the province object, so we can use its details
            province_kind = models.PlaceKind.objects.get(slug='province')
            context['province'] = models.Place.objects.get(
                kind=province_kind,
                slug=province_name)
        else:
            province_name = None

        # All lists of candidates share this kind
        election_list = models.OrganisationKind.objects.get(slug='election-list')

        # Build the right election list name
        election_list_name = party_name

        if election_type == 'provincial':
            election_list_name += '-provincial'
            election_list_name += '-' + province_name
        elif election_type == 'national' and province_name is not None:
            election_list_name += '-regional'
            election_list_name += '-' + province_name
        else:
            election_list_name += '-national'

        election_list_name += '-election-list'
        election_list_name += '-' + election_year

        # This is a party template, so get the party
        context['party'] = get_object_or_404(models.Organisation, slug=party_name)

        # Now go get the party's election list (assuming it exists)
        election_list = get_object_or_404(models.Organisation,
            slug=election_list_name
        )

        candidates = election_list.position_set.select_related('title').all()

        context['party_election_list'] = sorted(candidates,key=lambda x:
            int(re.match('\d+', x.title.name).group())
        )

        # Grab a list of provinces in which the party is actually running
        # Only relevant for national election non-province party lists
        if election_type == 'national' and province_name is None:

            # Find the lists of regional candidates for this party
            party_election_lists_startwith = party_name + '-regional-'
            party_election_lists_endwith = '-election-list-' + election_year

            party_election_lists = models.Organisation.objects.filter(
                slug__startswith=party_election_lists_startwith,
                slug__endswith=party_election_lists_endwith
            ).order_by('name')

            # Loop through lists and extract province slugs
            party_provinces = []
            for l in party_election_lists:
                province_slug = l.slug.replace(party_election_lists_startwith, '')
                province_slug = province_slug.replace(party_election_lists_endwith, '')
                party_provinces.append(province_slug)

            context['province_list'] = models.Place.objects.filter(
                kind__slug='province',
                slug__in=party_provinces
            )

        return context

class SAElectionProvinceCandidatesView(TemplateView):

    template_name = 'south_africa/election_candidates_province.html'

    election_type = 'national'

    def get_context_data(self, **kwargs):
        context = super(SAElectionProvinceCandidatesView, self).get_context_data(**kwargs)

        # These are common bits
        election_year = self.kwargs['election_year']
        election_type = self.election_type

        context['election_year'] = election_year
        context['election_type'] = election_type

        # Details from the URI
        if 'party_name' in self.kwargs:
            party_name = self.kwargs['party_name']
        else:
            party_name = None

        # The province name should always exist
        province_name = self.kwargs['province_name']

        # All lists of candidates share this kind
        election_list = models.OrganisationKind.objects.get(slug='election-list')

        # Build the right election list name
        election_list_name = ''

        if election_type == 'provincial':
            election_list_name += '-provincial'
            election_list_name += '-' + province_name
        elif election_type == 'national' and province_name is not None:
            election_list_name += '-regional'
            election_list_name += '-' + province_name

        election_list_name += '-election-list'
        election_list_name += '-' + election_year

        # Go get all the election lists!
        election_lists = models.Organisation.objects.filter(
            slug__endswith=election_list_name
        ).order_by('name')

        # Loop round each election list so we can go do other necessary queries
        context['province_election_lists'] = []

        for election_list in election_lists:

            # Get the party data, finding the raw party slug from the list name
            party = models.Organisation.objects.get(
                slug=election_list.slug.replace(election_list_name, '')
            )

            # Get the candidates data for that list
            candidate_list = election_list.position_set.select_related('title').all()

            candidates = sorted(candidate_list, key=lambda x:
                int(re.match('\d+', x.title.name).group())
            )

            context['province_election_lists'].append({
                'party': party,
                'candidates': candidates
            })

        # Get the province object, so we can use its details
        province_kind = models.PlaceKind.objects.get(slug='province')
        context['province'] = models.Place.objects.get(
            kind=province_kind,
            slug=province_name)

        return context

class SAMembersInterestsIndex(TemplateView):
    template_name = "interests_register/index.html"

    def get_context_data(self, **kwargs):
        context = {}

        #get the data for the form
        kind_slugs = (
            'parliament',
            'executive',
            'joint-committees',
            'ncop-committees',
            'ad-hoc-committees',
            'national-assembly-committees'
        )
        context['categories'] = Category.objects.all()
        context['parties'] = models.Organisation.objects.filter(kind__slug='party')
        context['organisations'] = models.Organisation.objects \
            .filter(kind__slug__in=kind_slugs) \
            .order_by('kind__id','name')
        context['releases'] = Release.objects.all()

        #set filter values
        for key in ('display', 'category', 'party', 'organisation', 'release'):
            context[key] = 'all'
            if key in self.request.GET:
                context[key] = self.request.GET[key]

        if context['release']!='all':
            release = Release.objects.get(slug=context['release'])
            context['release_id'] = release.id

        if context['category']!='all':
            categorylookup = Category.objects.get(slug=self.request.GET['category'])
            context['category_id'] = categorylookup.id

        #complete view - declarations for multiple people in multiple categories
        if context['display']=='all' and context['category']=='all':
            context = self.get_complete_view(context)

        #section view - data for multiple people in different categories
        elif context['display']=='all' and context['category']!='all':
            context = self.get_section_view(context)

        #numberbyrepresentative view - number of declarations per person per category
        elif context['display']=='numberbyrepresentative':
            context = self.get_number_by_representative_view(context)

        #numberbysource view - number of declarations by source per category
        elif context['display']=='numberbysource':
            context = self.get_numberbysource_view(context)

        return context

    def get_complete_view(self, context):
        #complete view - declarations for multiple people in multiple categories
        context['layout'] = 'complete'

        when = datetime.date.today()
        now_approx = repr(ApproximateDate(year=when.year, month=when.month, day=when.day))

        people = Entry.objects.order_by(
            'person__legal_name',
            'release__date'
        ).distinct(
            'person__legal_name',
            'release__date'
        )

        if context['release']!='all':
            people = people.filter(release=context['release_id'])

        if context['party']!='all':
            people = people.filter(
                Q(person__position__end_date__gte=now_approx) | Q(person__position__end_date=''),
                person__position__organisation__slug=context['party'],
                person__position__start_date__lte=now_approx)

        if context['organisation']!='all':
            people = people.filter(
                Q(person__position__end_date__gte=now_approx) | Q(person__position__end_date=''),
                person__position__organisation__slug=context['organisation'],
                person__position__start_date__lte=now_approx)

        paginator = Paginator(people, 10)
        page = self.request.GET.get('page')

        try:
            people_paginated = paginator.page(page)
        except PageNotAnInteger:
            people_paginated = paginator.page(1)
        except EmptyPage:
            people_paginated = paginator.page(paginator.num_pages)

        context['paginator'] = people_paginated

        #tabulate the data
        data = []
        for entry_person in people_paginated:
            person = entry_person.person
            year = entry_person.release.date.year
            person_data = []
            person_categories = person.interests_register_entries.filter(
                release=entry_person.release
            ).order_by(
                'category__id'
            ).distinct(
                'category__id'
            )

            for cat in person_categories:
                entries = person.interests_register_entries.filter(
                    category=cat.category,
                    release=entry_person.release)

                headers = []
                headers_index = {}
                cat_data = []
                for entry in entries:
                    row = ['']*len(headers)

                    for entrylineitem in entry.line_items.all():
                        if entrylineitem.key not in headers:
                            headers_index[entrylineitem.key] = len(headers)
                            headers.append(entrylineitem.key)
                            row.append('')

                        row[headers_index[entrylineitem.key]] = entrylineitem.value

                    cat_data.append(row)

                person_data.append({
                    'category': cat.category,
                    'headers': headers,
                    'data': cat_data})
            data.append({
                'person': person,
                'data': person_data,
                'year': year})

        context['data'] = data

        return context

    def get_section_view(self, context):
        #section view - data for multiple people in different categories
        context['layout'] = 'section'

        when = datetime.date.today()
        now_approx = repr(ApproximateDate(year=when.year, month=when.month, day=when.day))

        entries = Entry.objects.select_related(
            'person__name',
            'category'
        ).all().filter(
            category__id=context['category_id']
        ).order_by(
            'person',
            'category'
        )

        if context['release']!='all':
            entries = entries.filter(release=context['release_id'])

        if context['party']!='all':
            entries = entries.filter(
                Q(person__position__end_date__gte=now_approx) | Q(person__position__end_date=''),
                person__position__organisation__slug=context['party'],
                person__position__start_date__lte=now_approx)

        if context['organisation']!='all':
            entries = entries.filter(
                Q(person__position__end_date__gte=now_approx) | Q(person__position__end_date=''),
                person__position__organisation__slug=context['organisation'],
                person__position__start_date__lte=now_approx)

        paginator = Paginator(entries, 25)
        page = self.request.GET.get('page')

        try:
            entries_paginated = paginator.page(page)
        except PageNotAnInteger:
            entries_paginated = paginator.page(1)
        except EmptyPage:
            entries_paginated = paginator.page(paginator.num_pages)

        headers = ['Year', 'Person', 'Type']
        headers_index = {'Year': 0, 'Person': 1, 'Type': 2}
        data = []
        for entry in entries_paginated:
            row = ['']*len(headers)
            row[0] = entry.release.date.year
            row[1] = entry.person
            row[2] = entry.category.name

            for entrylineitem in entry.line_items.all():
                if entrylineitem.key not in headers:
                    headers_index[entrylineitem.key] = len(headers)
                    headers.append(entrylineitem.key)
                    row.append('')

                row[headers_index[entrylineitem.key]] = entrylineitem.value

            data.append(row)

        context['data'] = data
        context['headers'] = headers
        context['paginator'] = entries_paginated

        return context

    def get_number_by_representative_view(self, context):
        #numberbyrepresentative view - number of declarations per person per category
        context['layout'] = 'numberbyrepresentative'

        #custom sql used as
        #Entry.objects.values('category', 'release', 'person').annotate(c=Count('id')).order_by('-c')
        #returns a ValueQuerySet with foreign keys, not model instances
        if context['category']=='all' and context['release']=='all':
            data = Entry.objects.raw(
                '''SELECT max(id) as id, category_id, release_id, person_id,
                count(*) as c FROM "interests_register_entry"
                GROUP BY category_id, release_id, person_id ORDER BY c DESC''',
                [])
        elif context['category'] == 'all':
            data = Entry.objects.raw(
                '''SELECT max(id) as id, category_id, release_id, person_id,
                count(*) as c FROM "interests_register_entry"
                WHERE "interests_register_entry"."release_id" = %s
                GROUP BY category_id, release_id, person_id ORDER BY c DESC''',
                [context['release_id']])
        elif context['category'] != 'all' and context['release']=='all':
            data = Entry.objects.raw(
                '''SELECT max(id) as id, category_id, release_id, person_id,
                count(*) as c FROM "interests_register_entry"
                WHERE "interests_register_entry"."category_id" = %s
                GROUP BY category_id, release_id, person_id ORDER BY c DESC''',
                [context['category_id']])
        else:
            data = Entry.objects.raw(
                '''SELECT max(id) as id, category_id, release_id, person_id,
                count(*) as c FROM "interests_register_entry"
                WHERE "interests_register_entry"."category_id" = %s
                AND "interests_register_entry"."release_id" = %s
                GROUP BY category_id, release_id, person_id ORDER BY c DESC''',
                [context['category_id'], context['release_id']])

        paginator = Paginator(data, 20)
        paginator._count = len(list(data))
        page = self.request.GET.get('page')

        try:
            data_paginated = paginator.page(page)
        except PageNotAnInteger:
            data_paginated = paginator.page(1)
        except EmptyPage:
            data_paginated = paginator.page(paginator.num_pages)

        context['data'] = data_paginated

        return context

    def get_numberbysource_view(self, context):
        #numberbysource view - number of declarations by source per category
        context['layout'] = 'numberbysource'
        if context['category']=='all' and context['release']=='all':
            data = Entry.objects.raw(
                '''SELECT max("interests_register_entrylineitem"."id") as id,
                value, count(*) as c, release_id, category_id
                FROM "interests_register_entrylineitem"
                INNER JOIN "interests_register_entry"
                ON ("interests_register_entrylineitem"."entry_id"
                = "interests_register_entry"."id")
                WHERE ("interests_register_entrylineitem"."key" = 'Source')
                GROUP BY value, release_id, category_id ORDER BY c DESC''',
                [])
        elif context['category'] == 'all':
            data = Entry.objects.raw(
                '''SELECT max("interests_register_entrylineitem"."id") as id,
                value, count(*) as c, release_id, category_id
                FROM "interests_register_entrylineitem"
                INNER JOIN "interests_register_entry"
                ON ("interests_register_entrylineitem"."entry_id"
                = "interests_register_entry"."id")
                WHERE ("interests_register_entrylineitem"."key" = 'Source'
                AND "interests_register_entry"."release_id" = %s)
                GROUP BY value, release_id, category_id ORDER BY c DESC''',
                [context['release_id']])
        elif context['release']=='all':
            data = Entry.objects.raw(
                '''SELECT max("interests_register_entrylineitem"."id") as id,
                value, count(*) as c, release_id, category_id
                FROM "interests_register_entrylineitem"
                INNER JOIN "interests_register_entry"
                ON ("interests_register_entrylineitem"."entry_id"
                = "interests_register_entry"."id")
                WHERE ("interests_register_entry"."category_id" = %s
                AND "interests_register_entrylineitem"."key" = 'Source')
                GROUP BY value, release_id, category_id ORDER BY c DESC''',
                [context['category_id']])
        else:
            data = Entry.objects.raw(
                '''SELECT max("interests_register_entrylineitem"."id") as id,
                value, count(*) as c, release_id, category_id
                FROM "interests_register_entrylineitem"
                INNER JOIN "interests_register_entry"
                ON ("interests_register_entrylineitem"."entry_id"
                = "interests_register_entry"."id")
                WHERE ("interests_register_entry"."category_id" = %s
                AND "interests_register_entrylineitem"."key" = 'Source'
                AND "interests_register_entry"."release_id" = %s)
                GROUP BY value, release_id, category_id ORDER BY c DESC''',
                [context['category_id'], context['release_id']])

        paginator = Paginator(data, 20)
        paginator._count = len(list(data))
        page = self.request.GET.get('page')

        try:
            data_paginated = paginator.page(page)
        except PageNotAnInteger:
            data_paginated = paginator.page(1)
        except EmptyPage:
            data_paginated = paginator.page(paginator.num_pages)

        context['data'] = data_paginated

        return context

class SAMembersInterestsSource(TemplateView):
    template_name = "interests_register/source.html"

    def get_context_data(self, **kwargs):
        context = {}

        context['categories'] = Category.objects.filter(
            slug__in = ['sponsorships',
                        'gifts-and-hospitality',
                        'benefits',
                        'pensions'])
        context['releases'] = Release.objects.all()

        context['source'] = ''
        context['match'] = 'absolute'
        context['category'] = context['categories'][0].slug
        context['release'] = 'all'

        for key in ('source', 'match', 'category', 'release'):
            if key in self.request.GET:
                context[key] = self.request.GET[key]

        #if a source is specified perform the search
        if context['source']!='':
            if context['match']=='absolute':
                entries = Entry.objects.filter(line_items__key='Source',
                    line_items__value=context['source'],
                    category__slug=context['category'])
            else:
                entries = Entry.objects.filter(line_items__key='Source',
                    line_items__value__contains=context['source'],
                    category__slug=context['category'])

            if context['release']!='all':
                entries = entries.filter(release__slug=context['release'])

            paginator = Paginator(entries, 25)
            page = self.request.GET.get('page')

            try:
                entries_paginated = paginator.page(page)
            except PageNotAnInteger:
                entries_paginated = paginator.page(1)
            except EmptyPage:
                entries_paginated = paginator.page(paginator.num_pages)

            #tabulate the data
            headers = ['Year', 'Person', 'Type']
            headers_index = {'Year': 0, 'Person': 1, 'Type': 2}
            data = []
            for entry in entries_paginated:
                row = ['']*len(headers)
                row[0] = entry.release.date.year
                row[1] = entry.person
                row[2] = entry.category.name

                for entrylineitem in entry.line_items.all():
                    if entrylineitem.key not in headers:
                        headers_index[entrylineitem.key] = len(headers)
                        headers.append(entrylineitem.key)
                        row.append('')

                    row[headers_index[entrylineitem.key]] = entrylineitem.value

                data.append(row)

            context['data'] = data
            context['headers'] = headers
            context['paginator'] = entries_paginated

        else:
            context['data'] = None

        return context
