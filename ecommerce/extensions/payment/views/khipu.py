""" Views for interacting with the payment processor. """
from __future__ import unicode_literals

import logging
import os
from cStringIO import StringIO

from django.core.exceptions import MultipleObjectsReturned
from django.core.management import call_command
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View
from django.views.generic import RedirectView, TemplateView
from oscar.apps.partner import strategy
from oscar.apps.payment.exceptions import PaymentError
from oscar.core.loading import get_class, get_model

from ecommerce.extensions.basket.utils import basket_add_organization_attribute
from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.checkout.utils import get_receipt_page_url
from ecommerce.extensions.payment.processors.khipu import Khipu, KhipuWebpay, KhipuAlreadyProcessed

logger = logging.getLogger(__name__)

Applicator = get_class('offer.applicator', 'Applicator')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')


class KiphiPaymentPendingView(TemplateView):
    template_name = 'payment/khipu.html'

    def get_context_data(self, **kwargs):
        context = super(KiphiPaymentPendingView, self).get_context_data(**kwargs)
        #context.update({})
        return context

class KhipuPaymentCheckView(EdxOrderPlacementMixin, View):
    @property
    def payment_processor(self):
        return Khipu(self.request.site)

    def _get_basket(self, payment_id):
        """
        Retrieve a basket using a payment ID.

        Arguments:
            payment_id: payment_id received from Khipu.

        Returns:
            It will return related basket or log exception and return None if
            duplicate payment_id received or any other exception occurred.

        """
        try:
            basket = PaymentProcessorResponse.objects.get(
                processor_name=self.payment_processor.NAME,
                transaction_id=payment_id
            ).basket
            basket.strategy = strategy.Default()
            Applicator().apply(basket, basket.owner, self.request)

            basket_add_organization_attribute(basket, self.request.GET)
            return basket
        except MultipleObjectsReturned:
            logger.warning(u"Duplicate payment ID [%s] received from Khipu.", payment_id)
            return None
        except Exception:  # pylint: disable=broad-except
            logger.exception(u"Unexpected error during basket retrieval while executing Khipu payment.")
            return None

    def get(self, request):
        transaction_id = request.GET.get('transaction_id')
        basket = self._get_basket(transaction_id)
        if not basket:
            print("CHECK EROR")
        r = self.payment_processor.khipu_request('payments/{}'.format(transaction_id), 'GET', {})
        print(r)
        print(r.json())
        return ""


class KhipuPaymentNotificationView(EdxOrderPlacementMixin, View):
    """Process the Kiphu notification of a completed transaction"""
    @property
    def payment_processor(self):
        return Khipu(self.request.site)

    # Disable atomicity for the view. Otherwise, we'd be unable to commit to the database
    # until the request had concluded; Django will refuse to commit when an atomic() block
    # is active, since that would break atomicity. Without an order present in the database
    # at the time fulfillment is attempted, asynchronous order fulfillment tasks will fail.
    @method_decorator(transaction.non_atomic_requests)
    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        return super(KhipuPaymentNotificationView, self).dispatch(request, *args, **kwargs)

    def _get_basket(self, payment_id):
        """
        Retrieve a basket using a payment ID.

        Arguments:
            payment_id: payment_id received from Khipu.

        Returns:
            It will return related basket or log exception and return None if
            duplicate payment_id received or any other exception occurred.

        """
        try:
            basket = PaymentProcessorResponse.objects.get(
                processor_name=self.payment_processor.NAME,
                transaction_id=payment_id
            ).basket
            basket.strategy = strategy.Default()
            Applicator().apply(basket, basket.owner, self.request)

            basket_add_organization_attribute(basket, self.request.GET)
            return basket
        except MultipleObjectsReturned:
            logger.warning(u"Duplicate payment ID [%s] received from Khipu.", payment_id)
            return None
        except Exception:  # pylint: disable=broad-except
            logger.exception(u"Unexpected error during basket retrieval while executing Khipu payment.")
            return None

    def post(self, request):
        """Handle a notification received by Khipu with status update of a transaction"""
        try:
            khipu_data = self.payment_processor.get_transaction_data(request.POST)
            payment_id = self.payment_processor.get_payment_id(khipu_data)
            logger.info(u"Payment [%s] update received by Khipu", payment_id)

            basket = self._get_basket(payment_id)
            if not basket:
                logger.error("Basket not found for payment [%s]", payment_id)
                raise Http404()
        except Exception as e:  # pylint: disable=broad-except
            logger.exception("Error receiving payment {} {}".format(request.POST, e))
            raise Http404()

        try:
            with transaction.atomic():
                try:
                    self.handle_payment(khipu_data, basket)
                except PaymentError:
                    return redirect(self.payment_processor.error_url)
                except KhipuAlreadyProcessed:
                    # Return 400, telling khipu that the transaction was already processed
                    raise HttpResponseBadRequest()
        except HttpResponseBadRequest:
            raise
        except Exception:
            logger.exception('Attempts to handle payment for basket [%d] failed.', basket.id)
            raise Http404()

        try:
            # Generate and handle the order
            shipping_method = NoShippingRequired()
            shipping_charge = shipping_method.calculate(basket)
            order_total = OrderTotalCalculator().calculate(basket, shipping_charge)

            user = basket.owner
            # Given a basket, order number generation is idempotent. Although we've already
            # generated this order number once before, it's faster to generate it again
            # than to retrieve an invoice number from PayPal.
            order_number = basket.order_number

            order = self.handle_order_placement(
                order_number=order_number,
                user=user,
                basket=basket,
                shipping_address=None,
                shipping_method=shipping_method,
                shipping_charge=shipping_charge,
                billing_address=None,
                order_total=order_total,
                request=request
            )
            self.handle_post_order(order)

            return HttpResponse('')
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(self.order_placement_failure_msg, payment_id, basket.id)
            raise Http404()

class KhipuWebpayPaymentNotificationView(KhipuPaymentNotificationView):
    """Process the Kiphu notification of a completed transaction"""
    @property
    def payment_processor(self):
        return KhipuWebpay(self.request.site)
