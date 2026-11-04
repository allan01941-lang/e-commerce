import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import Order, SiteSettings


class FacebookPurchaseEventError(Exception):
    pass


def _hash_value(value):
    normalized = value.strip().lower()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest() if normalized else None


def _hashed_phone(phone):
    digits = re.sub(r'\D', '', phone)
    if digits.startswith('00'):
        digits = digits[2:]
    elif digits.startswith('0'):
        digits = f'88{digits}'
    return hashlib.sha256(digits.encode('utf-8')).hexdigest() if digits else None


def _send_event(pixel_id, payload):
    site_settings = SiteSettings.objects.first()
    access_token = (
        site_settings.facebook_conversions_api_access_token
        if site_settings and site_settings.facebook_conversions_api_access_token
        else getattr(settings, 'FACEBOOK_CONVERSIONS_API_ACCESS_TOKEN', '')
    )
    if not pixel_id or not access_token:
        raise FacebookPurchaseEventError(
            'Configure the Facebook Pixel ID in Site Settings and '
            'a Conversions API access token in Site Settings or the server environment to send Purchase events.'
        )

    api_version = getattr(settings, 'FACEBOOK_GRAPH_API_VERSION', 'v26.0')
    query = urllib.parse.urlencode({'access_token': access_token})
    url = f'https://graph.facebook.com/{api_version}/{pixel_id}/events?{query}'
    api_request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    try:
        with urllib.request.urlopen(api_request, timeout=5) as response:
            result = json.loads(response.read().decode('utf-8'))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise FacebookPurchaseEventError(
            'Meta did not accept the Purchase event. Check the server logs and Meta Events Manager, then retry.'
        ) from error

    if result.get('events_received') != 1 or result.get('error'):
        raise FacebookPurchaseEventError(
            'Meta did not confirm receipt of the Purchase event. Check Meta Events Manager, then retry.'
        )


def send_facebook_test_purchase_event(test_event_code, request):
    site_settings = SiteSettings.objects.first()
    pixel_id = site_settings.facebook_pixel_id if site_settings else ''
    event_time = timezone.now()
    user_data = {}
    payload = {
        'data': [{
            'event_name': 'Purchase',
            'event_time': int(event_time.timestamp()),
            'event_id': f'test-purchase-{uuid4()}',
            'action_source': 'website',
            'event_source_url': request.build_absolute_uri(reverse('home')),
            'user_data': user_data,
            'custom_data': {
                'currency': 'BDT',
                'value': 1.0,
                'content_type': 'product',
                'content_ids': ['ADMIN_TEST'],
                'contents': [{'id': 'ADMIN_TEST', 'quantity': 1, 'item_price': 1.0}],
            },
        }],
        'test_event_code': test_event_code.strip(),
    }
    _send_event(pixel_id, payload)


def send_order_purchase_event(order, request):
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status not in {'confirmed', 'delivered'} or order.facebook_purchase_event_sent_at:
            return False

        if order.status == 'delivered' and not order.delivered_at:
            order.delivered_at = timezone.now()
            order.save(update_fields=['delivered_at'])

        site_settings = SiteSettings.objects.first()
        pixel_id = site_settings.facebook_pixel_id if site_settings else ''
        user_data = {}
        hashed_email = _hash_value(order.email or '')
        hashed_phone = _hashed_phone(order.phone)
        if hashed_email:
            user_data['em'] = [hashed_email]
        if hashed_phone:
            user_data['ph'] = [hashed_phone]

        contents = [
            {
                'id': (
                    item.product.sku
                    if item.product_id and item.product.sku
                    else str(item.product_id or item.product_name)
                ),
                'quantity': item.quantity,
                'item_price': float(item.unit_price),
            }
            for item in order.items.select_related('product')
        ]
        payload = {
            'data': [{
                'event_name': 'Purchase',
                'event_time': int(timezone.now().timestamp()),
                'event_id': f'order-{order.pk}',
                'action_source': 'website',
                'event_source_url': request.build_absolute_uri(reverse('order_success', args=[order.pk])),
                'user_data': user_data,
                'custom_data': {
                    'currency': 'BDT',
                    'value': float(order.total or Decimal('0')),
                    'content_type': 'product',
                    'content_ids': [content['id'] for content in contents],
                    'contents': contents,
                },
            }],
        }
        _send_event(pixel_id, payload)
        order.facebook_purchase_event_sent_at = timezone.now()
        order.save(update_fields=['facebook_purchase_event_sent_at'])
        return True
