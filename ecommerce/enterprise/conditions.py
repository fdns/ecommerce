from __future__ import unicode_literals

import logging
from uuid import UUID

import waffle
from oscar.core.loading import get_model
from requests.exceptions import ConnectionError, Timeout
from slumber.exceptions import SlumberHttpBaseException

from ecommerce.enterprise.api import catalog_contains_course_runs, fetch_enterprise_learner_data
from ecommerce.enterprise.constants import ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, ENTERPRISE_OFFERS_SWITCH
from ecommerce.extensions.basket.utils import ENTERPRISE_CATALOG_ATTRIBUTE_TYPE
from ecommerce.extensions.offer.constants import OFFER_ASSIGNMENT_REVOKED, OFFER_REDEEMED
from ecommerce.extensions.offer.decorators import check_condition_applicability
from ecommerce.extensions.offer.mixins import ConditionWithoutRangeMixin, SingleItemConsumptionConditionMixin

BasketAttribute = get_model('basket', 'BasketAttribute')
BasketAttributeType = get_model('basket', 'BasketAttributeType')
Condition = get_model('offer', 'Condition')
ConditionalOffer = get_model('offer', 'ConditionalOffer')
OfferAssignment = get_model('offer', 'OfferAssignment')
Voucher = get_model('voucher', 'Voucher')
logger = logging.getLogger(__name__)


class EnterpriseCustomerCondition(ConditionWithoutRangeMixin, SingleItemConsumptionConditionMixin, Condition):
    class Meta(object):
        app_label = 'enterprise'
        proxy = True

    @property
    def name(self):
        return "Basket contains a seat from {}'s catalog".format(self.enterprise_customer_name)

    @check_condition_applicability([ENTERPRISE_OFFERS_SWITCH])
    def is_satisfied(self, offer, basket):  # pylint: disable=unused-argument
        """
        Determines if a user is eligible for an enterprise customer offer
        based on their association with the enterprise customer.

        It also filter out the offer if the `enterprise_customer_catalog_uuid`
        value set on the offer condition does not match with the basket catalog
        value when explicitly provided by the enterprise learner.

        Note: Currently there is no mechanism to prioritize or apply multiple
        offers that may apply as opposed to disqualifying offers if the
        catalog doesn't explicitly match.

        Arguments:
            basket (Basket): Contains information about order line items, the current site,
                             and the user attempting to make the purchase.
        Returns:
            bool
        """
        if not basket.owner:
            # An anonymous user is never linked to any EnterpriseCustomer.
            return False

        if (offer.offer_type == ConditionalOffer.VOUCHER and
                not waffle.switch_is_active(ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH)):
            logger.info('Skipping Voucher type enterprise conditional offer until we are ready to support it.')
            return False

        enterprise_customer = str(self.enterprise_customer_uuid)
        enterprise_catalog = str(self.enterprise_customer_catalog_uuid)
        username = basket.owner.username
        course_run_ids = []
        for line in basket.all_lines():
            course = line.product.course
            if not course:
                # Basket contains products not related to a course_run.
                logger.warning('Unable to apply enterprise offer because '
                               'the Basket contains a product not related to a course_run. '
                               'Offer: %s, Enterprise: %s, Catalog: %s, User: %s, Product ID: %s',
                               offer.id,
                               enterprise_customer,
                               enterprise_catalog,
                               username,
                               line.product.id)
                return False

            course_run_ids.append(course.id)

        courses_in_basket = ','.join(course_run_ids)
        learner_data = {}
        try:
            learner_data = fetch_enterprise_learner_data(basket.site, basket.owner)['results'][0]
        except (ConnectionError, KeyError, SlumberHttpBaseException, Timeout) as exc:
            logger.exception('Unable to apply enterprise offer because '
                             'we failed to retrieve enterprise learner data for the user. '
                             'Offer: %s, Enterprise: %s, Catalog: %s, User: %s, Courses: %s, Exception: %s',
                             offer.id,
                             enterprise_customer,
                             enterprise_catalog,
                             username,
                             courses_in_basket,
                             exc)
            return False
        except IndexError:
            if offer.offer_type == ConditionalOffer.SITE:
                logger.debug(
                    'Unable to apply enterprise site offer %s because no learner data was returned for user %s',
                    offer.id,
                    basket.owner)
                return False

        if (learner_data and 'enterprise_customer' in learner_data and
                enterprise_customer != learner_data['enterprise_customer']['uuid']):
            # Learner is not linked to the EnterpriseCustomer associated with this condition.
            if offer.offer_type == ConditionalOffer.VOUCHER:
                logger.warning('Unable to apply enterprise offer because Learner\'s enterprise (%s)'
                               'does not match this conditions\'s enterprise (%s). '
                               'Offer: %s, Enterprise: %s, Catalog: %s, User: %s, Courses: %s',
                               learner_data['enterprise_customer']['uuid'],
                               enterprise_customer,
                               offer.id,
                               enterprise_customer,
                               enterprise_catalog,
                               username,
                               courses_in_basket)
            return False

        # Verify that the current conditional offer is related to the provided
        # enterprise catalog, this will also filter out offers which don't
        # have `enterprise_customer_catalog_uuid` value set on the condition.
        catalog = self._get_enterprise_catalog_uuid_from_basket(basket)
        if catalog:
            if offer.condition.enterprise_customer_catalog_uuid != catalog:
                logger.warning('Unable to apply enterprise offer %s because '
                               'Enterprise catalog id on the basket (%s) '
                               'does not match the catalog for this condition (%s).',
                               offer.id, catalog, offer.condition.enterprise_customer_catalog_uuid)
                return False

        try:
            catalog_contains_course = catalog_contains_course_runs(
                basket.site, course_run_ids, enterprise_customer, enterprise_customer_catalog_uuid=enterprise_catalog
            )
        except (ConnectionError, KeyError, SlumberHttpBaseException, Timeout) as exc:
            logger.exception('Unable to apply enterprise offer because '
                             'we failed to check if course_runs exist in the catalog. '
                             'Offer: %s, Enterprise: %s, Catalog: %s, User: %s, Courses: %s, Exception: %s',
                             offer.id,
                             enterprise_customer,
                             enterprise_catalog,
                             username,
                             courses_in_basket,
                             exc)
            return False

        if not catalog_contains_course:
            # Basket contains course runs that do not exist in the EnterpriseCustomerCatalogs
            # associated with the EnterpriseCustomer.
            logger.warning('Unable to apply enterprise offer because '
                           'Enterprise catalog does not contain the course(s) in this basket. '
                           'Offer: %s, Enterprise: %s, Catalog: %s, User: %s, Courses: %s',
                           offer.id,
                           enterprise_customer,
                           enterprise_catalog,
                           username,
                           courses_in_basket)
            return False

        return True

    @staticmethod
    def _get_enterprise_catalog_uuid_from_basket(basket):
        """
        Helper method for fetching valid enterprise catalog UUID from basket.

        Arguments:
             basket (Basket): The provided basket can be either temporary (just
             for calculating discounts) or an actual one to buy a product.
        """
        # For temporary basket try to get `catalog` from request
        catalog = basket.strategy.request.GET.get(
            'catalog'
        ) if basket.strategy.request else None

        if not catalog:
            # For actual baskets get `catalog` from basket attribute
            enterprise_catalog_attribute, __ = BasketAttributeType.objects.get_or_create(
                name=ENTERPRISE_CATALOG_ATTRIBUTE_TYPE
            )
            enterprise_customer_catalog = BasketAttribute.objects.filter(
                basket=basket,
                attribute_type=enterprise_catalog_attribute,
            ).first()
            if enterprise_customer_catalog:
                catalog = enterprise_customer_catalog.value_text

        # Return only valid UUID
        try:
            catalog = UUID(catalog) if catalog else None
        except ValueError:
            catalog = None

        return catalog


class AssignableEnterpriseCustomerCondition(EnterpriseCustomerCondition):
    """An enterprise condition that can be redeemed by one or more assigned users."""
    class Meta(object):
        app_label = 'enterprise'
        proxy = True

    def is_satisfied(self, offer, basket):  # pylint: disable=unused-argument
        """
        Determines that if user has assigned a voucher and is eligible for redeem it.

        Arguments:
            offer (ConditionalOffer): The offer to be redeemed.
            basket (Basket): The basket of products being purchased.

        Returns:
            bool
        """
        condition_satisfied = super(AssignableEnterpriseCustomerCondition, self).is_satisfied(offer, basket)
        if condition_satisfied is False:
            return False

        voucher = basket.vouchers.first()

        # get assignments for the basket owner and basket voucher
        user_with_code_assignments = OfferAssignment.objects.filter(
            code=voucher.code, user_email=basket.owner.email
        ).exclude(
            status__in=[OFFER_REDEEMED, OFFER_ASSIGNMENT_REVOKED]
        )

        # user has assignments available
        if user_with_code_assignments.exists():
            return True

        # basket owner can redeem the voucher if free slots are avialable
        if voucher.slots_available_for_assignment:
            return True

        logger.warning('Unable to apply enterprise offer because '
                       'the voucher has not been assigned to this user and their are no remaining available uses. '
                       'Offer: %s, Enterprise: %s, Catalog: %s, User: %s',
                       offer.id,
                       self.enterprise_customer_uuid,
                       self.enterprise_customer_catalog_uuid,
                       basket.owner.username)

        return False
