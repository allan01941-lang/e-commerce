from decimal import Decimal, InvalidOperation
import hashlib
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

import certifi
from django.core.cache import cache


class PathaoApiError(Exception):
    def __init__(self, message, *, stage=None, status_code=None, login_status_code=None):
        super().__init__(message)
        self.stage = stage
        self.status_code = status_code
        self.login_status_code = login_status_code


# Configuration and common response handling
def _environment_prefix(settings):
    if settings.pathao_environment not in ('sandbox', 'production'):
        raise PathaoApiError('Select Sandbox or Production in Pathao Site Settings.')
    return f'pathao_{settings.pathao_environment}_'


def is_pathao_configured(settings):
    try:
        prefix = _environment_prefix(settings)
    except PathaoApiError:
        return False
    return all((
        getattr(settings, f'{prefix}client_id'),
        getattr(settings, f'{prefix}client_secret'),
        getattr(settings, f'{prefix}username'),
        getattr(settings, f'{prefix}password'),
        settings.pathao_grant_type,
        getattr(settings, f'{prefix}base_url'),
    ))


def _base_url(settings):
    prefix = _environment_prefix(settings)
    base_url = getattr(settings, f'{prefix}base_url')
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        raise PathaoApiError('The Pathao API URL must be a valid HTTPS URL.')
    return base_url.rstrip('/')


def _response_message(payload, fallback):
    message = payload.get('message') if isinstance(payload, dict) else None
    if isinstance(message, str) and message.strip():
        return message.strip()[:400]
    return fallback


def _connection_error_message(error):
    reason = getattr(error, 'reason', error)
    if isinstance(reason, TimeoutError):
        return 'Pathao connection timed out after 10 seconds.'

    if isinstance(reason, OSError):
        detail = reason.strerror or type(reason).__name__
    elif isinstance(reason, str):
        detail = reason.strip()[:200]
    else:
        detail = type(reason).__name__
    return f'Could not connect to Pathao: {detail or "network request failed"}.'


def _request(
    settings,
    endpoint,
    method='GET',
    payload=None,
    raw_body=None,
    token=None,
    include_status=False,
    stage='Pathao request',
):
    body = raw_body
    headers = {'Accept': 'application/json'}
    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    elif raw_body is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    if token:
        headers['Authorization'] = f'Bearer {token}'

    request = urllib.request.Request(
        f'{_base_url(settings)}/aladdin/api/v1/{endpoint.lstrip("/")}',
        data=body,
        headers=headers,
        method=method,
    )
    try:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(
            request,
            timeout=10,
            context=ssl_context,
        ) as response:
            raw_response = response.read()
            status_code = getattr(response, 'status', None)
            if status_code is None:
                get_status_code = getattr(response, 'getcode', None)
                status_code = get_status_code() if callable(get_status_code) else None
    except urllib.error.HTTPError as error:
        try:
            error_payload = json.loads(error.read().decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            error_payload = {}
        raise PathaoApiError(
            _response_message(error_payload, f'Pathao returned HTTP {error.code}.'),
            stage=stage,
            status_code=error.code,
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise PathaoApiError(_connection_error_message(error), stage=stage) from error

    try:
        response_payload = json.loads(raw_response.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PathaoApiError(
            'Pathao returned an unreadable response.',
            stage=stage,
            status_code=status_code,
        ) from error
    if not isinstance(response_payload, dict):
        raise PathaoApiError(
            'Pathao returned an unexpected response.',
            stage=stage,
            status_code=status_code,
        )
    if include_status:
        return response_payload, status_code
    return response_payload


# Access-token caching and authentication
def _token_cache_key(settings):
    prefix = _environment_prefix(settings)
    credentials = '\0'.join((
        settings.pathao_environment,
        getattr(settings, f'{prefix}base_url'),
        getattr(settings, f'{prefix}client_id'),
        getattr(settings, f'{prefix}client_secret'),
        getattr(settings, f'{prefix}username'),
        getattr(settings, f'{prefix}password'),
        settings.pathao_grant_type,
    ))
    return 'pathao-token-' + hashlib.sha256(credentials.encode('utf-8')).hexdigest()


def _get_token(settings, include_status=False):
    if not is_pathao_configured(settings):
        raise PathaoApiError(
            'Configure Pathao credentials in Site Settings first.',
            stage='Login',
        )

    prefix = _environment_prefix(settings)
    cache_key = _token_cache_key(settings)
    cached_token = cache.get(cache_key)
    if cached_token:
        if isinstance(cached_token, dict):
            token = cached_token.get('access_token')
            status_code = cached_token.get('status_code')
        else:
            token = cached_token
            status_code = None
        if token:
            return (token, status_code) if include_status else token

    response, status_code = _request(
        settings,
        'issue-token',
        method='POST',
        include_status=True,
        stage='Login',
        payload={
            'client_id': getattr(settings, f'{prefix}client_id'),
            'client_secret': getattr(settings, f'{prefix}client_secret'),
            'username': getattr(settings, f'{prefix}username'),
            'password': getattr(settings, f'{prefix}password'),
            'grant_type': settings.pathao_grant_type,
        },
    )
    token_data = response.get('data', response)
    if not isinstance(token_data, dict) or not token_data.get('access_token'):
        raise PathaoApiError(
            _response_message(response, 'Pathao did not return an access token.'),
            stage='Login',
            status_code=status_code,
        )
    expires_in = token_data.get('expires_in', 3600)
    try:
        cache_timeout = max(60, int(expires_in) - 60)
    except (TypeError, ValueError):
        cache_timeout = 3540
    token = token_data['access_token']
    cache.set(
        cache_key,
        {'access_token': token, 'status_code': status_code},
        timeout=cache_timeout,
    )
    return (token, status_code) if include_status else token


# Shipment status lookup and delivery-fee parsing
def get_order_info(settings, consignment_id):
    token = _get_token(settings)
    response = _request(
        settings,
        f'orders/{urllib.parse.quote(str(consignment_id), safe="")}/info',
        method='POST',
        raw_body=b'',
        token=token,
    )
    result = response.get('data', response)
    if not isinstance(result, dict):
        raise PathaoApiError('Pathao returned unexpected shipment information.')

    shipment_info = {}
    status = result.get('order_status') or result.get('status')
    if status is not None:
        shipment_info['order_status'] = str(status)

    if 'delivery_fee' in result:
        shipment_info['delivery_fee'] = _parse_delivery_fee(result['delivery_fee'])

    return shipment_info


def _parse_delivery_fee(value):
    try:
        delivery_fee = Decimal(str(value)) if value is not None else None
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PathaoApiError('Pathao returned an invalid delivery fee.') from error
    if delivery_fee is not None and (
        not delivery_fee.is_finite() or delivery_fee < 0
    ):
        raise PathaoApiError('Pathao returned an invalid delivery fee.')
    return delivery_fee


# Store selection for order creation
def _get_store_id(settings, token):
    prefix = _environment_prefix(settings)
    store_id = getattr(settings, f'{prefix}store_id')
    if store_id:
        return store_id

    response = _request(
        settings,
        'stores',
        token=token,
        stage='Order creation',
    )
    store_data = response.get('data', response)
    if isinstance(store_data, dict):
        stores = store_data.get('data')
    else:
        stores = store_data
    if not isinstance(stores, list):
        raise PathaoApiError('Pathao returned an unexpected store list.')

    active_stores = [
        store for store in stores
        if isinstance(store, dict)
        and store.get('is_active', 1) in (1, True, '1', 'true', 'True')
        and store.get('store_id')
    ]
    default_stores = [
        store for store in active_stores
        if store.get('is_default_store') is True
        or str(store.get('is_default_store')).lower() in ('1', 'true')
    ]
    if len(default_stores) == 1:
        selected_store = default_stores[0]
    elif len(active_stores) == 1:
        selected_store = active_stores[0]
    elif not active_stores:
        raise PathaoApiError(
            'No active Pathao store was found for these credentials. Create or activate a store in the matching Pathao environment.'
        )
    else:
        raise PathaoApiError(
            'Multiple active Pathao stores were found. Select the required Store ID in Site Settings.'
        )

    try:
        return int(selected_store['store_id'])
    except (TypeError, ValueError) as error:
        raise PathaoApiError('Pathao returned an invalid Store ID.') from error


# Validate order data, submit it to Pathao, and normalize the response
def create_order(settings, order):
    items = list(order.items.all())
    item_quantity = sum(item.quantity for item in items)
    if item_quantity < 1:
        raise PathaoApiError('Add at least one product before creating a Pathao shipment.')

    item_weight = settings.pathao_item_weight
    if not Decimal('0.5') <= item_weight <= Decimal('10'):
        raise PathaoApiError('Pathao parcel weight must be between 0.5 and 10 kg.')

    address_parts = (order.address.strip(), order.thana.strip(), order.zilla.strip())
    if not all(address_parts):
        raise PathaoApiError(
            'Add the recipient street address, Thana / Upazila, and Zilla before creating a shipment.'
        )
    recipient_address = ', '.join(address_parts)
    item_description = '; '.join(
        f'{item.product_name} x{item.quantity} (৳{item.line_total:.2f})'
        for item in items
    )[:500]
    amount_to_collect = order.total
    if amount_to_collect != amount_to_collect.to_integral_value():
        raise PathaoApiError('Pathao COD collection amount must be a whole amount in BDT.')

    token, login_status_code = _get_token(settings, include_status=True)
    try:
        store_id = _get_store_id(settings, token)
    except PathaoApiError as error:
        error.login_status_code = login_status_code
        raise
    merchant_order_id = str(order.display_order_number)
    try:
        response, order_status_code = _request(
            settings,
            'orders',
            method='POST',
            token=token,
            include_status=True,
            stage='Order creation',
            payload={
                'store_id': store_id,
                'merchant_order_id': merchant_order_id,
                'recipient_name': order.full_name,
                'recipient_phone': order.phone,
                'recipient_address': recipient_address,
                'delivery_type': 48,
                'item_type': 2,
                'special_instruction': (order.fulfillment_note or order.note)[:500],
                'item_quantity': item_quantity,
                'item_weight': str(item_weight),
                'item_description': item_description,
                'amount_to_collect': int(amount_to_collect),
            },
        )
    except PathaoApiError as error:
        error.login_status_code = login_status_code
        raise
    result = response.get('data', response)
    if not isinstance(result, dict) or not result.get('consignment_id'):
        raise PathaoApiError(
            _response_message(response, 'Pathao did not return a consignment ID.'),
            stage='Order creation',
            status_code=order_status_code,
            login_status_code=login_status_code,
        )
    response_code = response.get('code')
    try:
        response_code = int(response_code) if response_code is not None else None
    except (TypeError, ValueError) as error:
        raise PathaoApiError(
            'Pathao returned an invalid response code.',
            stage='Order creation',
            status_code=order_status_code,
            login_status_code=login_status_code,
        ) from error

    return {
        'consignment_id': str(result['consignment_id']),
        'status': str(result.get('order_status') or result.get('status') or ''),
        'merchant_order_id': str(result.get('merchant_order_id') or merchant_order_id),
        'delivery_fee': _parse_delivery_fee(result.get('delivery_fee')),
        'message': _response_message(response, ''),
        'type': str(response.get('type') or ''),
        'code': response_code,
        'login_status_code': login_status_code,
        'order_status_code': order_status_code,
    }
