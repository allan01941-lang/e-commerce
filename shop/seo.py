from urllib.parse import urlsplit, urlunsplit

from django.urls import reverse
from django.utils.html import strip_tags

from .models import Product


INDEXABLE_VIEWS = {'home', 'shop', 'categories', 'product_detail'}


def get_site_origin(request, site_settings):
    configured_origin = (site_settings.canonical_url if site_settings else '').strip()
    if configured_origin:
        parsed_origin = urlsplit(configured_origin)
        return urlunsplit((parsed_origin.scheme, parsed_origin.netloc, '', '', '')).rstrip('/')
    return f'{request.scheme}://{request.get_host()}'


def _clean_description(value, max_length):
    text = ' '.join(strip_tags(value or '').split())
    if len(text) > max_length:
        return text[:max_length - 1].rsplit(' ', 1)[0] + '…'
    return text


def _setting(site_settings, field, fallback=''):
    return getattr(site_settings, field, '') if site_settings else fallback


def build_seo_context(request, site_settings):
    view_name = getattr(getattr(request, 'resolver_match', None), 'url_name', '')
    brand_name = _setting(site_settings, 'brand_name', 'BDBazaar') or 'BDBazaar'
    origin = get_site_origin(request, site_settings)
    canonical_url = f'{origin}{request.path}'
    indexing_enabled = bool(_setting(site_settings, 'search_engine_indexing', True))
    indexable_view = view_name in INDEXABLE_VIEWS
    is_filtered_shop = view_name == 'shop' and bool(request.GET)
    is_indexable = indexing_enabled and indexable_view and not is_filtered_shop

    page_title = _setting(site_settings, 'meta_title', f'{brand_name} | Online Shopping in Bangladesh')
    page_description = _setting(
        site_settings,
        'meta_description',
        'Shop quality electronics, home essentials, fashion, and beauty products online in Bangladesh.',
    ) or 'Shop quality electronics, home essentials, fashion, and beauty products online in Bangladesh.'

    if view_name == 'home':
        page_title = _setting(site_settings, 'home_meta_title') or page_title
        page_description = _setting(site_settings, 'home_meta_description') or page_description
    elif view_name == 'categories':
        page_title = _setting(site_settings, 'categories_meta_title') or f'Shop by category | {brand_name}'
        page_description = _setting(site_settings, 'categories_meta_description') or page_description
    elif view_name == 'shop':
        page_title = _setting(site_settings, 'shop_meta_title') or f'Shop online in Bangladesh | {brand_name}'
        page_description = _setting(site_settings, 'shop_meta_description') or page_description
    elif view_name == 'product_detail':
        slug = request.resolver_match.kwargs.get('slug')
        product = getattr(request, '_seo_product', None)
        if product is None:
            product = Product.objects.filter(slug=slug, is_active=True).select_related('category').prefetch_related('images').first()
        if product:
            page_title = f'{product.name} | {brand_name}'
            page_description = product.short_description or product.name

    page_title = _clean_description(page_title, 120)
    page_description = _clean_description(page_description, 160)
    og_title = page_title if view_name == 'product_detail' else (
        _setting(site_settings, 'og_title') or page_title
    )
    og_description = page_description if view_name == 'product_detail' else (
        _setting(site_settings, 'og_description') or page_description
    )
    og_image = ''
    product_schema = None

    if view_name == 'product_detail' and product:
        images = [image for image in product.images.all() if image.image]
        image = next((item for item in images if item.is_primary), images[0] if images else None)
        if image:
            og_image = f'{origin}{image.image.url}'
        product_schema = {
            '@type': 'Product',
            'name': product.name,
            'description': _clean_description(product.short_description or product.name, 5000),
            'category': product.category.name,
            'image': [f'{origin}{item.image.url}' for item in images],
            'offers': {
                '@type': 'Offer',
                'url': canonical_url,
                'priceCurrency': 'BDT',
                'price': str(product.starting_price),
                'availability': (
                    'https://schema.org/InStock'
                    if product.stock_quantity > 0
                    else 'https://schema.org/OutOfStock'
                ),
                'itemCondition': 'https://schema.org/NewCondition',
            },
        }
        if product.sku:
            product_schema['sku'] = product.sku
    if not og_image and site_settings and site_settings.og_image:
        og_image = f'{origin}{site_settings.og_image.url}'

    organization = {
        '@type': 'Organization',
        'name': brand_name,
        'url': f'{origin}/',
        'description': _clean_description(_setting(site_settings, 'about_text'), 500),
        'areaServed': _setting(site_settings, 'brand_area', 'Bangladesh'),
        'sameAs': [
            value for value in (
                _setting(site_settings, 'facebook_url'),
                _setting(site_settings, 'instagram_url'),
                _setting(site_settings, 'twitter_url'),
                _setting(site_settings, 'youtube_url'),
            ) if value
        ],
    }
    if site_settings and site_settings.logo:
        organization['logo'] = f'{origin}{site_settings.logo.url}'
    if site_settings and site_settings.email:
        organization['email'] = site_settings.email
    if site_settings and site_settings.phone:
        organization['telephone'] = site_settings.phone

    website_schema = {
        '@type': 'WebSite',
        'name': brand_name,
        'url': f'{origin}/',
        'potentialAction': {
            '@type': 'SearchAction',
            'target': f'{origin}{reverse("shop")}?search={{search_term_string}}',
            'query-input': 'required name=search_term_string',
        },
    }
    graph = [organization, website_schema]
    if product_schema:
        graph.append(product_schema)

    return {
        'seo': {
            'title': page_title,
            'description': page_description,
            'robots': 'index,follow' if is_indexable else 'noindex,follow',
            'canonical_url': canonical_url,
            'og_title': _clean_description(og_title, 200),
            'og_description': _clean_description(og_description, 200),
            'site_name': brand_name,
            'og_type': 'product' if product_schema else 'website',
            'og_image': og_image,
            'twitter_card': 'summary_large_image' if og_image else 'summary',
            'twitter_title': _clean_description(og_title, 200),
            'twitter_description': _clean_description(og_description, 200),
            'twitter_handle': _setting(site_settings, 'twitter_handle'),
            'google_site_verification': _setting(site_settings, 'google_site_verification'),
            'json_ld': {'@context': 'https://schema.org', '@graph': graph},
        },
    }
