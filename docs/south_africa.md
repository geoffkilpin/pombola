# Setting up South Africa pombola data

Firstly, download the boundary files from
http://www.demarcation.org.za/Downloads/Boundary/Boundaries.html and unrar
them.

To set up MapIt with these boundaries, you need to create a new MapIt
generation, load in the South Africa fixture, load in the boundaries, then
activate the generation. Hopefully something like this:

    $ python manage.py mapit_generation_create --desc "Initial import" --commit
    $ python manage.py loaddata mapit_south_africa
    $ python manage.py south_africa_import_boundaries --wards=pombola/south_africa/boundary-data/Wards2011.shp --districts=pombola/south_africa/boundary-data/DistrictMunicipalities2011.shp --provinces=pombola/south_africa/boundary-data/Province_New_SANeighbours.shp --locals=pombola/south_africa/boundary-data/LocalMunicipalities2011.shp --commit
    $ python manage.py mapit_generation_activate --commit

Then, to load in some people and organisation data:

    $ python manage.py core_import_popolo pombola/south_africa/data/south-africa-popolo.json  --commit

Run the command to clean up imported slugs:

    $ python manage.py core_list_malformed_slugs --correct

To load in constituency offices run the following.
If you run into any issues with this you might need to remove the
geocode cache at `pombola/south_africa/management/commands/.geocode-request-cache`.

    $ python manage.py south_africa_import_constituency_offices --commit --verbose pombola/south_africa/data/constituencies_and_offices/all_constituencies.csv
    $ python manage.py south_africa_import_constituency_offices --commit --verbose pombola/south_africa/data/constituencies_and_offices/new-entries-for-1115.csv

To load in some example SayIt data, fetch the speeches/fixtures/test_inputs/

    $ python manage.py load_akomantoso --dir <speeches/fixtures/test_inputs/> --commit

Places can be created to match all those in the mapit database.

    $ ./manage.py core_create_places_from_mapit_entries PRV
    $ ./manage.py core_create_places_from_mapit_entries DMN
    $ ./manage.py core_create_places_from_mapit_entries LMN
    $ ./manage.py core_create_places_from_mapit_entries WRD

Once created they can then be structured into a hierarchy.

    $ ./manage.py core_set_area_parents wards:local-municipality
    $ ./manage.py core_set_area_parents local-municipality:district-municipality
    $ ./manage.py core_set_area_parents district-municipality:province

Currently the data for the metropolitan municipalites is in a funny place - see
https://github.com/mysociety/pombola/issues/695. Once fixed though these
commands should add the data for them:

    !!TODO!! ./manage.py core_create_places_from_mapit_entries MMN
    !!TODO!! ./manage.py core_set_area_parents wards:metropolitan-municipality
    !!TODO!! ./manage.py core_set_area_parents metropolitan-municipality:province

Load in the Members' Interests data:

    $ ./manage.py interests_register_import_from_json pombola/south_africa/data/members-interests/2011_for_import.json
    $ ./manage.py interests_register_import_from_json pombola/south_africa/data/members-interests/2012_for_import.json

