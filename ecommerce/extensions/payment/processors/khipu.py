""" Khipu payment processing. """
from __future__ import absolute_import, unicode_literals

import hashlib
import hmac
import logging
import requests
import urllib
from urlparse import urljoin
from collections import OrderedDict
from decimal import Decimal

from django.urls import reverse
from oscar.apps.payment.exceptions import GatewayError, TransactionDeclined
from oscar.core.loading import get_class, get_model

from ecommerce.extensions.payment.processors import BasePaymentProcessor, HandledProcessorResponse
from ecommerce.extensions.checkout.utils import get_receipt_page_url
from ecommerce.core.url_utils import get_ecommerce_url

Order = get_model('order', 'Order')

logger = logging.getLogger(__name__)


class KhipuAlreadyProcessed(Exception):
    """Raised when the order was successful and already processed"""
    pass


class Khipu(BasePaymentProcessor):
    """
    Khipu RESST API 1.3 (JUN 2019)

    For reference, see https://khipu.com/page/api-referencia
    """
    NAME = 'khipu'
    DEFAULT_PROFILE_NAME = 'default'
    API_VERSION = '2.0'
    NOTIFICATION_API_VERSION = '1.3'
    KHIPU_URL = 'https://khipu.com/api/{}/'.format(API_VERSION)

    def __init__(self, site):
        """
        Construct a new instance of the khipu processor.
        """
        super(Khipu, self).__init__(site)

    def khipu_request(self, url_posfix, method, parameters):
        """
        Send a request to the given url, calculating the hash of the parameters

        The hash process is as follow:
            - Set parameters as an ordered dict, to prevent them from changing order
            - url encode the parameters
            - Calculate the HMAC using the private key
            - Add the 'hash' parameter to the Authorization header
        """
        method = method.upper()
        url = self.KHIPU_URL + url_posfix
        parameters = OrderedDict(sorted(parameters.items()))
        header = {
            'Authorization': '{}:{}'.format(self.configuration['id'], self.do_hash(method, url, parameters)),
            'Content-Type': 'application/x-www-form-urlencoded',
        }

        if method == 'GET':
            return requests.get(url, parameters, headers=header)
        elif method == 'POST':
            return requests.post(url, parameters, headers=header)
        else:
            raise ValueError('Invalid method value {}'.format(method))

    def do_hash(self, method, url, data):
        """
        Calculathe the Khipu hash using the method, url and data.

        The hash algorithm is HMAC(SECRET, METHOD&URL&PARAMETERS), with parameters sorted.

        Arguments:
            method: http method of the request
            url: Url, including domain of the request. Must not include any GET parameters
            data: Ordered and sorted dictionary with the request data

        returns:
            Hex Digest of the calculated hash
        """
        method_name = method.upper()
        to_sign = method_name + '&' + urllib.quote_plus(url) + (('&' + urllib.urlencode(data)) if len(data) > 0 else '')
        return hmac.new(self.configuration['secret'], to_sign, hashlib.sha256).hexdigest()

    def get_transaction_parameters(self, basket, request=None, use_client_side_checkout=False, **kwargs):
        """
        Create a new Khipu payment.

        Arguments:
            basket (Basket): The basket of products being purchased.
            request (Request, optional): A Request object which is used to construct Khipu's `return_url`.
            use_client_side_checkout (bool, optional): This value is not used.
            **kwargs: Additional parameters; not used by this method.

        Returns:
            dict: Khipu-specific parameters required to complete a transaction. Must contain a URL
                to which users can be directed in order to approve a newly created payment.

        Raises:
            GatewayError: Indicates a general error or unexpected behavior on the part of Khipu which prevented
                a payment from being created.
            TransactionDeclined: Indicates that Khipo declined to create the transaction.
        """
        return_url = receipt_url = get_receipt_page_url(
            order_number=basket.order_number,
            site_configuration=basket.site.siteconfiguration
        )
        # return_url = urljoin(get_ecommerce_url(), reverse('checkout:receipt'))  #urljoin(get_ecommerce_url(), reverse('khipu:execute'))
        cancel_url = urljoin(get_ecommerce_url(), reverse('checkout:cancel-checkout'))  # urljoin(get_ecommerce_url(), reverse('khipu:cancel'))
        notify_url = urljoin(get_ecommerce_url(), reverse('khipu:execute'))
        data = {
            'transaction_id': basket.order_number,
            'subject': basket.order_number,
            'currency': 'CLP',
            'amount': unicode(basket.total_incl_tax),
            # 'custom': None,
            # 'body': None,
            # 'bank_id': None,
            'return_url': return_url,
            'cancel_url': cancel_url,
            # 'picture_url': None,
            'notify_url': notify_url,  # TODO: Add basket id
            # 'contract_url': None,
            'notify_api_version': self.NOTIFICATION_API_VERSION,
            # 'expires_date': None,
            'responsible_user_email': self.configuration['responsible_user_email'],
            # 'collect_account_uuid': None,
            # 'confirm_timeout_date': None
        }

        result = self.khipu_request('payments', 'POST', data)

        if result.status_code != 201:
            msg = 'Khipu payment for basket [%d] declined with HTTP status [%d]'

            logger.exception(msg + ': %s', basket.id, result.status_code, result.text)
            self.record_processor_response(result.text, basket=basket)
            raise TransactionDeclined(msg, basket.id, result.status_code)

        result = result.json()
        self.record_processor_response(result, transaction_id=result['payment_id'], basket=basket)

        parameters = {
            'payment_page_url': result['payment_url'],
        }

        return parameters

    def handle_processor_response(self, responce, basket):
        """
        Handle Khipu notification, completing the transaction if the parameters are correct.

        Arguments:
            responce: Dictionary with the transaction data fetched from self.get_transaction_data
            basket: Basket assigned to the transaction

        Returns:
            HandledProcessorResponse with the transaction information
        Raises:
            GatewayError: Indicates the transaction is not ready, or the amount isn't the same as recorded
        """
        # Fetch transfaction data
        result = responce
        self.record_processor_response(result, basket=basket)

        if result['status'] == 'done':
            if Decimal(result['amount']) == Decimal(basket.total_incl_tax):
                # Check if order is already processed
                if Order.objects.filter(number=basket.order_number).exists():
                    raise KhipuAlreadyProcessed()
                return HandledProcessorResponse(
                    transaction_id=result['payment_id'],
                    total=result['amount'],
                    currency=result['currency'],
                    card_number='Khipu_{}'.format(basket.id),
                    card_type=None
                )
            else:
                logger.error("Transaction [{}] have different transaction ammount [{}], expected [{}]".format(result['payment_id'], result['amount'], basket.total_incl_tax))

        logger.error("Transaction [{}] for basket [{}] not done or with invalid amount.\n {}".format(result['payment_id'], basket.id, result))
        raise GatewayError("Transaction not ready")

    def issue_credit(self, order_number, basket, reference_number, amount, currency):
        return reference_number
        raise NotImplementedError

    def get_transaction_data(self, request_data):
        api_version = request_data.get('api_version', '')
        if api_version != self.NOTIFICATION_API_VERSION:
            self.record_processor_response(request_data, transaction_id=None, basket=None)
            logger.error(u'api_version %s different from expected value %s', api_version, self.NOTIFICATION_API_VERSION)
            raise GatewayError("invalid api_version {}".format(api_version))

        notification_token = request_data.get('notification_token', '')
        data = {
            'notification_token': notification_token,
        }
        result = self.khipu_request('payments', 'GET', data)

        if result.status_code != 200:
            msg = 'Khipu notification_token [%s] invalid with HTTP status [%d]'

            logger.exception(msg + ': %s', notification_token, result.status_code, result.text)
            self.record_processor_response(result.text, basket=None)
            raise GatewayError(msg.format(msg, notification_token, result.status_code))
        return result.json()

    def get_payment_id(self, data):
        return data['payment_id']

    @property
    def error_url(self):
        return "/todo"
