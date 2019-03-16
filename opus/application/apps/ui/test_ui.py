# ui/test_ui.py

import logging
import sys
from unittest import TestCase

import django.conf
from django.http import Http404
from django.test.client import Client

from ui.views import *

import settings

django.conf.settings.CACHE_BACKEND = 'dummy:///'

class uiTests(TestCase):

    def setUp(self):
        self.maxDiff = None
        sys.tracebacklimit = 0 # default: 1000
        logging.disable(logging.ERROR)

    def tearDown(self):
        sys.tracebacklimit = 1000 # default: 1000
        logging.disable(logging.NOTSET)


            ###################################################
            ######### api_last_blog_update UNIT TESTS #########
            ###################################################

    def test__api_last_blog_update_no_request(self):
        "[test_ui.py] api_last_blog_update: no request"
        with self.assertRaises(Http404):
            api_last_blog_update(None)

    def test__api_last_blog_update_no_get(self):
        "[test_ui.py] api_last_blog_update: no GET"
        c = Client()
        response = c.get('__lastblogupdate.json')
        request = response.wsgi_request
        request.GET = None
        with self.assertRaises(Http404):
            api_last_blog_update(request)

    def test__api_last_blog_update_ok(self):
        "[test_ui.py] api_last_blog_update: normal"
        settings.OPUS_LAST_BLOG_UPDATE_FILE = 'test_api/data/lastblogupdate.txt'
        c = Client()
        response = c.get('__lastblogupdate.json')
        request = response.wsgi_request
        ret = api_last_blog_update(request)
        print(ret)
        self.assertEqual(ret.content, b'{"lastupdate": "2019-JAN-01"}')

    def test__api_last_blog_update_bad(self):
        "[test_ui.py] api_last_blog_update: missing"
        settings.OPUS_LAST_BLOG_UPDATE_FILE = 'test_api/data/xyxyxyxyxyx.txt'
        c = Client()
        response = c.get('__lastblogupdate.json')
        request = response.wsgi_request
        ret = api_last_blog_update(request)
        print(ret)
        print(ret.content)
        self.assertEqual(ret.content, b'{"lastupdate": null}')


            ################################################
            ######### api_normalize_url UNIT TESTS #########
            ################################################

    def test__api_normalize_url_no_request(self):
        "[test_ui.py] api_normalize_url: no request"
        with self.assertRaises(Http404):
            api_normalize_url(None)

    def test__api_normalize_url_no_get(self):
        "[test_ui.py] api_normalize_url: no GET"
        c = Client()
        response = c.get('__normalizeurl.json')
        request = response.wsgi_request
        request.GET = None
        with self.assertRaises(Http404):
            api_normalize_url(request)
