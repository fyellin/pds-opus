# Run api test with manage.py:
* Run all tests against the app including db integrity and every test in the Django app (not including result count tests)
  * python manage.py test -v 2
* Run api tests:
  * against internal database (not including result count tests):
    * python manage.py test -s -v 2 api-internal-db
  * against production site:
    * python manage.py test -s -v 2 api-livetest-pro
  * against dev site:
    * python manage.py test -s -v 2 api-livetest-dev
* Run standalone result counts tests against internal database:
    * python manage.py test -s -v 2 api-internal-db-result-counts
