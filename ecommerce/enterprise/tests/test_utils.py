from __future__ import unicode_literals

import uuid

import ddt
import httpretty
from django.conf import settings
from django.http.response import HttpResponse
from edx_django_utils.cache import TieredCache
from mock import patch
from oscar.test.factories import VoucherFactory

from ecommerce.core.constants import SYSTEM_ENTERPRISE_ADMIN_ROLE, SYSTEM_ENTERPRISE_LEARNER_ROLE
from ecommerce.enterprise.tests.mixins import EnterpriseServiceMockMixin
from ecommerce.enterprise.utils import (
    enterprise_customer_user_needs_consent,
    get_enterprise_catalog,
    get_enterprise_customer,
    get_enterprise_customer_uuid,
    get_enterprise_customers,
    get_enterprise_id_for_current_request_user_from_jwt,
    get_enterprise_id_for_user,
    get_or_create_enterprise_customer_user,
    set_enterprise_customer_cookie
)
from ecommerce.extensions.test.factories import prepare_voucher
from ecommerce.tests.testcases import TestCase

TEST_ENTERPRISE_CUSTOMER_UUID = 'cf246b88-d5f6-4908-a522-fc307e0b0c59'


@ddt.ddt
@httpretty.activate
class EnterpriseUtilsTests(EnterpriseServiceMockMixin, TestCase):
    def setUp(self):
        super(EnterpriseUtilsTests, self).setUp()
        self.learner = self.create_user(is_staff=True)
        self.client.login(username=self.learner.username, password=self.password)

    def test_get_enterprise_customers(self):
        """
        Verify that "get_enterprise_customers" returns an appropriate response from the
        "enterprise-customer" Enterprise service API endpoint.
        """
        self.mock_access_token_response()
        self.mock_enterprise_customer_list_api_get()
        response = get_enterprise_customers(self.site)
        self.assertEqual(response[0]['name'], "Enterprise Customer 1")
        self.assertEqual(response[1]['name'], "Enterprise Customer 2")

    def test_get_enterprise_customer(self):
        """
        Verify that "get_enterprise_customer" returns an appropriate response from the
        "enterprise-customer" Enterprise service API endpoint.
        """
        self.mock_access_token_response()
        self.mock_specific_enterprise_customer_api(TEST_ENTERPRISE_CUSTOMER_UUID)

        # verify the caching
        with patch.object(TieredCache, 'set_all_tiers', wraps=TieredCache.set_all_tiers) as mocked_set_all_tiers:
            mocked_set_all_tiers.assert_not_called()

            response = get_enterprise_customer(self.site, TEST_ENTERPRISE_CUSTOMER_UUID)
            self.assertEqual(TEST_ENTERPRISE_CUSTOMER_UUID, response.get('id'))
            self.assertEqual(mocked_set_all_tiers.call_count, 2)

            cached_response = get_enterprise_customer(self.site, TEST_ENTERPRISE_CUSTOMER_UUID)
            self.assertEqual(mocked_set_all_tiers.call_count, 2)
            self.assertEqual(response, cached_response)

    @ddt.data(
        (
            ['mock_enterprise_learner_api'],
            {'user_id': 5},
        ),
        (
            [
                'mock_enterprise_learner_api_for_learner_with_no_enterprise',
                'mock_enterprise_learner_post_api',
            ],
            {
                'enterprise_customer': TEST_ENTERPRISE_CUSTOMER_UUID,
                'username': 'the_j_meister',
            },
        )
    )
    @ddt.unpack
    def test_post_enterprise_customer_user(self, mock_helpers, expected_return):
        """
        Verify that "get_enterprise_customer" returns an appropriate response from the
        "enterprise-customer" Enterprise service API endpoint.
        """
        for mock in mock_helpers:
            getattr(self, mock)()

        self.mock_access_token_response()
        response = get_or_create_enterprise_customer_user(
            self.site,
            TEST_ENTERPRISE_CUSTOMER_UUID,
            self.learner.username
        )

        self.assertDictContainsSubset(expected_return, response)

    @httpretty.activate
    def test_ecu_needs_consent(self):
        opts = {
            'ec_uuid': 'fake-uuid',
            'course_id': 'course-v1:real+course+id',
            'username': 'johnsmith',
        }
        kw = {
            'enterprise_customer_uuid': 'fake-uuid',
            'course_id': 'course-v1:real+course+id',
            'username': 'johnsmith',
            'site': self.site
        }
        self.mock_access_token_response()
        self.mock_consent_get(**opts)
        self.assertEqual(enterprise_customer_user_needs_consent(**kw), False)
        self.mock_consent_missing(**opts)
        self.assertEqual(enterprise_customer_user_needs_consent(**kw), True)
        self.mock_consent_not_required(**opts)
        self.assertEqual(enterprise_customer_user_needs_consent(**kw), False)

    def test_get_enterprise_customer_uuid(self):
        """
        Verify that enterprise customer UUID is returned for a voucher with an associated enterprise customer.
        """
        enterprise_customer_uuid = uuid.uuid4()
        voucher, __ = prepare_voucher(enterprise_customer=enterprise_customer_uuid)

        self.assertEqual(
            enterprise_customer_uuid,
            get_enterprise_customer_uuid(voucher.code),
        )

    def test_get_enterprise_customer_uuid_non_existing_voucher(self):
        """
        Verify that None is returned when voucher with given code does not exist.
        """
        voucher = VoucherFactory()
        self.assertIsNone(get_enterprise_customer_uuid(voucher.code))

    def test_get_enterprise_customer_uuid_non_existing_conditional_offer(self):
        """
        Verify that None is returned if voucher exists but conditional offer
        does not exist.
        """
        voucher = VoucherFactory()
        self.assertIsNone(get_enterprise_customer_uuid(voucher.code))

    def test_set_enterprise_customer_cookie(self):
        """
        Verify that enterprise cookies are set properly.
        """
        enterprise_customer_uuid = uuid.uuid4()
        response = HttpResponse()

        result = set_enterprise_customer_cookie(self.site, response, enterprise_customer_uuid)

        cookie = result.cookies[settings.ENTERPRISE_CUSTOMER_COOKIE_NAME]
        self.assertEqual(str(enterprise_customer_uuid), cookie.value)

    def test_set_enterprise_customer_cookie_empty_cookie_domain(self):
        """
        Verify that enterprise cookie is not set if base_cookie_domain is empty
        in site configuration.
        """
        self.site.siteconfiguration.base_cookie_domain = ''
        self.site.siteconfiguration.save()

        enterprise_customer_uuid = uuid.uuid4()
        response = HttpResponse()

        result = set_enterprise_customer_cookie(self.site, response, enterprise_customer_uuid)

        self.assertNotIn(settings.ENTERPRISE_CUSTOMER_COOKIE_NAME, result.cookies)

    def test_get_enterprise_catalog(self):
        """
        Verify that "get_enterprise_catalog" returns an appropriate response from the
        "enterprise-catalog" Enterprise service API endpoint.
        """
        enterprise_catalog_uuid = str(uuid.uuid4())
        self.mock_access_token_response()
        self.mock_enterprise_catalog_api_get(enterprise_catalog_uuid)
        response = get_enterprise_catalog(self.site, enterprise_catalog_uuid, 50, 1)
        self.assertTrue(enterprise_catalog_uuid in response['next'])
        self.assertTrue(len(response['results']) == 3)
        for result in response['results']:
            self.assertTrue('course_runs' in result)

        cached_response = get_enterprise_catalog(self.site, enterprise_catalog_uuid, 50, 1)
        self.assertEqual(response, cached_response)

    @patch('ecommerce.enterprise.utils.get_decoded_jwt')
    def test_get_enterprise_id_for_current_request_user_from_jwt_request_has_no_jwt(self, mock_decode_jwt):
        """
        Verify get_enterprise_id_for_current_request_user_from_jwt returns None if
        decoded_jwt is None
        """
        mock_decode_jwt.return_value = None
        assert get_enterprise_id_for_current_request_user_from_jwt() is None

    @patch('ecommerce.enterprise.utils.get_decoded_jwt')
    def test_get_enterprise_id_for_current_request_user_from_jwt_request_has_jwt(self, mock_decode_jwt):
        """
        Verify get_enterprise_id_for_current_request_user_from_jwt returns jwt context
        for user if request has jwt and user has proper role
        """
        mock_decode_jwt.return_value = {
            'roles': ['{}:some-uuid'.format(SYSTEM_ENTERPRISE_LEARNER_ROLE)]
        }
        assert get_enterprise_id_for_current_request_user_from_jwt() == 'some-uuid'

    @patch('ecommerce.enterprise.utils.get_decoded_jwt')
    def test_get_enterprise_id_for_current_request_user_from_jwt_request_has_jwt_no_context(self, mock_decode_jwt):
        """
        Verify get_enterprise_id_for_current_request_user_from_jwt returns None if jwt
        context is missing
        """
        mock_decode_jwt.return_value = {
            'roles': ['{}'.format(SYSTEM_ENTERPRISE_LEARNER_ROLE)]
        }
        assert get_enterprise_id_for_current_request_user_from_jwt() is None

    @patch('ecommerce.enterprise.utils.get_decoded_jwt')
    def test_get_enterprise_id_for_current_request_user_from_jwt_request_has_jwt_non_learner(self, mock_decode_jwt):
        """
        Verify get_enterprise_id_for_current_request_user_from_jwt returns None if
        user role is incorrect
        """

        mock_decode_jwt.return_value = {
            'roles': ['{}:some-uuid'.format(SYSTEM_ENTERPRISE_ADMIN_ROLE)]
        }
        assert get_enterprise_id_for_current_request_user_from_jwt() is None

    @patch('ecommerce.enterprise.utils.get_enterprise_id_for_current_request_user_from_jwt')
    def test_get_enterprise_id_for_user_enterprise_in_jwt(self, mock_get_jwt_uuid):
        """
        Verify get_enterprise_id_for_user returns ent id if uuid in jwt context
        """
        mock_get_jwt_uuid.return_value = 'my-uuid'
        assert get_enterprise_id_for_user('some-site', self.learner) == 'my-uuid'

    @patch('ecommerce.enterprise.utils.fetch_enterprise_learner_data')
    @patch('ecommerce.enterprise.utils.get_enterprise_id_for_current_request_user_from_jwt')
    def test_get_enterprise_id_for_user_fetch_learner_data_has_uuid(self, mock_get_jwt_uuid, mock_fetch):
        """
        Verify get_enterprise_id_for_user returns enterprise id if jwt does not have
        enterprise uuid, but is able to fetch it via api call
        """
        mock_get_jwt_uuid.return_value = None
        mock_fetch.return_value = {
            'results': [
                {
                    'enterprise_customer': {
                        'uuid': 'my-uuid'
                    }
                }
            ]
        }
        assert get_enterprise_id_for_user('some-site', self.learner) == 'my-uuid'

    @patch('ecommerce.enterprise.utils.fetch_enterprise_learner_data')
    @patch('ecommerce.enterprise.utils.get_enterprise_id_for_current_request_user_from_jwt')
    def test_get_enterprise_id_for_user_fetch_errors(self, mock_get_jwt_uuid, mock_fetch):
        """
        Verify if that learner data fetch errors, get_enterprise_id_for_user
        returns None
        """
        mock_get_jwt_uuid.return_value = None
        mock_fetch.side_effect = [KeyError]

        assert get_enterprise_id_for_user('some-site', self.learner) is None

    @patch('ecommerce.enterprise.utils.fetch_enterprise_learner_data')
    @patch('ecommerce.enterprise.utils.get_enterprise_id_for_current_request_user_from_jwt')
    def test_get_enterprise_id_for_user_no_uuid_in_response(self, mock_get_jwt_uuid, mock_fetch):
        """
        Verify if learner data fetch is successful but does not include uuid field,
        None is returned
        """
        mock_get_jwt_uuid.return_value = None
        mock_fetch.return_value = {
            'results': []
        }
        assert get_enterprise_id_for_user('some-site', self.learner) is None
