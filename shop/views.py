import logging
import subprocess
import sys
from decimal import Decimal
from io import BytesIO
from urllib.parse import quote, unquote, urlencode

from django.contrib import admin
from django.contrib.auth import login, logout
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.conf import settings as django_settings
from django.contrib.auth.forms import AuthenticationForm
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Min, OuterRef, Q, Subquery, Sum
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone   
from django.views.decorators.http import require_POST
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .facebook import FacebookPurchaseEventError, send_order_purchase_event
from .forms import (
    CheckoutForm,
    StaffCallLogForm,
    StaffOrderFilterForm,
    StaffOrderUpdateForm,
    StaffProductVariantForm,
)
from .invoices import build_order_invoice
from .inventory import sync_product_stock
from .pathao import (
    PathaoApiError,
    create_order as create_pathao_order,
    get_order_info as get_pathao_order_info,
    is_pathao_configured,
)
from .models import (
    Banner,
    Category,
    Order,
    OrderItem,
    PathaoShipment,
    PathaoStatusSyncRun,
    Product,
    ProductHighlight,
    ProductImage,
    ProductVariant,
    Size,
    Color,
    SiteSettings,
    Stock,
    StaffCallLog,
)
from .seo import get_site_origin

STOCK_RETURN_STATUSES = {'returned', 'cancelled'}
logger = logging.getLogger(__name__)


def get_staff_landing_url(user):
    if user.is_superuser:
        return reverse('super_admin_dashboard')
    if user.groups.filter(name='Owner').exists():
        return reverse('admin_dashboard')
    if user.is_staff:
        return reverse('staff_dashboard')
    return reverse('home')


def home(request):
    categories = (
        Category.objects.filter(is_active=True)
        .annotate(product_count=Count('products', filter=Q(products__is_active=True)))[:8]
    )
    featured_products = Product.objects.filter(
        is_active=True,
        is_featured=True,
    ).prefetch_related('images', 'variants')[:4]
    newest_products = Product.objects.filter(
        is_active=True,
    ).prefetch_related('images', 'variants').order_by('-created_at')[:4]
    banners = Banner.objects.filter(is_active=True).order_by('ordering', 'created_at')
    settings = SiteSettings.objects.first()

    context = {
        'categories': categories,
        'featured_products': featured_products,
        'newest_products': newest_products,
        'banners': banners,
        'site_settings': settings,
    }
    return render(request, 'home.html', context)


def categories(request):
    category_list = (
        Category.objects.filter(is_active=True)
        .annotate(product_count=Count('products', filter=Q(products__is_active=True)))
        .order_by('name')
    )
    return render(request, 'categories.html', {'categories': category_list})


def search_suggestions(request):
    search_term = request.GET.get('search', '').strip()
    suggestions = []

    if len(search_term) >= 2:
        products = (
            Product.objects.filter(is_active=True)
            .filter(
                Q(name__icontains=search_term) |
                Q(short_description__icontains=search_term) |
                Q(long_description__icontains=search_term)
            )
            .select_related('category')
            .prefetch_related('images', 'variants')[:6]
        )
        for product in products:
            images = list(product.images.all())
            image = next((item for item in images if item.is_primary and item.image), None)
            image = image or next((item for item in images if item.image), None)
            suggestions.append({
                'name': product.name,
                'category': product.category.name,
                'price': str(product.starting_price),
                'url': reverse('product_detail', args=[product.slug]),
                'image': image.image.url if image else '',
            })

    return JsonResponse({'results': suggestions})


def shop(request):
    categories = Category.objects.filter(is_active=True)
    queryset = (
        Product.objects.filter(is_active=True)
        .select_related('category')
        .prefetch_related('images', 'variants')
        .annotate(_starting_price=Min('variants__price'))
    )

    selected_category = request.GET.get('category', '').strip()
    price_min = request.GET.get('min_price', '').strip()
    price_max = request.GET.get('max_price', '').strip()
    search_term = request.GET.get('search', '').strip()
    selected_size = request.GET.get('size', '').strip()
    selected_color = request.GET.get('color', '').strip()
    in_stock_only = request.GET.get('in_stock') == 'on'
    sort_option = request.GET.get('sort', 'newest')

    if selected_category:
        queryset = queryset.filter(category__slug=selected_category)

    if price_min:
        queryset = queryset.filter(_starting_price__gte=price_min)
    if price_max:
        queryset = queryset.filter(_starting_price__lte=price_max)

    if search_term:
        queryset = queryset.filter(
            Q(name__icontains=search_term) |
            Q(short_description__icontains=search_term) |
            Q(long_description__icontains=search_term)
        )

    if selected_size or selected_color:
        matching_variants = ProductVariant.objects.filter(product__is_active=True)
        if selected_size:
            matching_variants = matching_variants.filter(size__iexact=selected_size)
        if selected_color:
            matching_variants = matching_variants.filter(color__iexact=selected_color)
        queryset = queryset.filter(variants__in=matching_variants).distinct()

    if in_stock_only:
        queryset = queryset.filter(stock__quantity__gt=0)

    sort_map = {
        'newest': '-created_at',
        'price_asc': '_starting_price',
        'price_desc': '-_starting_price',
        'most_selling': '-stock__quantity',
    }
    queryset = queryset.order_by(sort_map.get(sort_option, '-created_at'))

    paginator = Paginator(queryset, 12)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    context = {
        'categories': categories,
        'available_sizes': (
            ProductVariant.objects.filter(product__is_active=True)
            .exclude(size='')
            .order_by('size')
            .values_list('size', flat=True)
            .distinct()
        ),
        'available_colors': (
            ProductVariant.objects.filter(product__is_active=True)
            .exclude(color='')
            .order_by('color')
            .values_list('color', flat=True)
            .distinct()
        ),
        'products': page_obj,
        'selected_category': selected_category,
        'selected_size': selected_size,
        'selected_color': selected_color,
        'search_term': search_term,
        'price_min': price_min,
        'price_max': price_max,
        'in_stock_only': in_stock_only,
        'sort_option': sort_option,
    }

    if request.headers.get('HX-Request') == 'true':
        return render(request, 'shop/partials/product_grid.html', context)

    return render(request, 'shop.html', context)


def product_detail(request, slug):
    product = get_object_or_404(
        Product.objects.select_related('category', 'stock').prefetch_related(
            'images',
            'highlights',
            'variants',
        ),
        slug=slug,
        is_active=True,
    )
    request._seo_product = product
    variant_sizes = product.variant_sizes
    variant_colors = product.variant_colors
    product_sizes = list(Size.objects.filter(name__in=variant_sizes).order_by('name'))
    product_colors = list(Color.objects.filter(name__in=variant_colors).order_by('name'))
    related_products = list(
        Product.objects.filter(category=product.category, is_active=True)
        .prefetch_related('images', 'variants')
        .exclude(pk=product.pk)
        .order_by('-created_at')[:6]
    )
    if len(related_products) < 6:
        related_products.extend(
            Product.objects.filter(is_active=True)
            .exclude(pk__in=[product.pk, *(item.pk for item in related_products)])
            .prefetch_related('images', 'variants')
            .order_by('-created_at')[:6 - len(related_products)]
        )
    gallery_records = list(product.images.all().order_by('ordering', 'created_at'))
    primary_image = next((image for image in gallery_records if image.is_primary), None)
    image_records = [image for image in gallery_records if image.image]
    gallery_images = [
        {
            'url': image.image.url,
            'alt': image.caption or product.name,
        }
        for image in image_records
    ]
    if primary_image:
        primary_image_url = primary_image.image.url
    elif gallery_images:
        primary_image_url = gallery_images[0]['url']
    else:
        primary_image_url = ''

    context = {
        'product': product,
        'product_sizes': product_sizes,
        'product_colors': product_colors,
        'product_highlights': product.highlights.all(),
        'product_variants': [
            {
                'size': variant.size,
                'color': variant.color,
                'price': str(variant.price),
                'compare_at_price': (
                    str(variant.compare_at_price)
                    if variant.compare_at_price is not None
                    else ''
                ),
                'quantity': variant.quantity,
            }
            for variant in get_active_product_variants(product)
        ],
        'gallery_images': gallery_images,
        'primary_image_url': primary_image_url,
        'related_products': related_products,
    }
    return render(request, 'product_detail.html', context)


def get_cart_items(request):
    cart = request.session.get('cart', {})
    items = []
    total = Decimal('0')

    for cart_key, quantity in cart.items():
        parsed_key = parse_cart_key(cart_key)
        if parsed_key is None:
            continue
        product_id, selected_size, selected_color = parsed_key
        product = (
            Product.objects.filter(pk=product_id, is_active=True)
            .select_related('stock')
            .prefetch_related('variants')
            .first()
        )
        if product is None:
            continue

        product_quantity = int(quantity)
        if product_quantity <= 0:
            continue

        variant = next(
            (
                option
                for option in product.variants.all()
                if option.size == selected_size and option.color == selected_color
            ),
            None,
        )
        if variant is None:
            continue
        unit_price = variant.price
        line_total = unit_price * product_quantity
        items.append({
            'product': product,
            'variant': variant,
            'quantity': product_quantity,
            'unit_price': unit_price,
            'available_quantity': variant.quantity,
            'line_total': line_total,
            'selected_size': selected_size,
            'selected_color': selected_color,
            'cart_key': cart_key,
        })
        total += line_total

    return items, total


def parse_cart_key(cart_key):
    if str(cart_key).isdigit():
        return int(cart_key), '', ''
    parts = str(cart_key).split(':', 3)
    if len(parts) != 4 or parts[0] != 'variant' or not parts[1].isdigit():
        return None
    return int(parts[1]), unquote(parts[2]), unquote(parts[3])


def build_cart_key(product, selected_size, selected_color):
    has_variants = any(
        variant.size or variant.color for variant in product.variants.all()
    )
    if not has_variants:
        return str(product.pk)
    return (
        f'variant:{product.pk}:'
        f'{quote(selected_size or "", safe="")}:'
        f'{quote(selected_color or "", safe="")}'
    )


def get_selected_variant(request, product):
    sizes = product.variant_sizes
    colors = product.variant_colors
    selected_size = ''
    selected_color = ''
    errors = []

    if sizes:
        size_id = request.POST.get('size_id', '')
        size_option = (
            Size.objects.filter(pk=size_id, name__in=sizes).first()
            if size_id
            else None
        )
        size = size_option.name if size_option else None
        if size is None and len(sizes) == 1:
            size = sizes[0]
        if size is None:
            errors.append('Choose an available size.')
        else:
            selected_size = size
    if colors:
        color_id = request.POST.get('color_id', '')
        color_option = (
            Color.objects.filter(pk=color_id, name__in=colors).first()
            if color_id
            else None
        )
        color = color_option.name if color_option else None
        if color is None and len(colors) == 1:
            color = colors[0]
        if color is None:
            errors.append('Choose an available color.')
        else:
            selected_color = color
    return selected_size, selected_color, errors


def get_cart_key_for_request(request, product):
    posted_key = request.POST.get('cart_key', '')
    parsed_key = parse_cart_key(posted_key)
    if parsed_key and parsed_key[0] == product.pk:
        return posted_key
    return build_cart_key(
        product,
        request.POST.get('selected_size', ''),
        request.POST.get('selected_color', ''),
    )


def cart_quantity_for_variant(cart, product_id, selected_size, selected_color, exclude_key=None):
    total = 0
    for key, quantity in cart.items():
        if key == exclude_key:
            continue
        parsed_key = parse_cart_key(key)
        if parsed_key and parsed_key == (product_id, selected_size, selected_color):
            try:
                total += int(quantity)
            except (TypeError, ValueError):
                continue
    return total


def get_product_variant(product, selected_size, selected_color):
    return next(
        (
            variant
            for variant in product.variants.all()
            if variant.size == selected_size and variant.color == selected_color
        ),
        None,
    )


def get_active_product_variants(product):
    return list(product.variants.all())


def get_variant_stock_quantity(product, variant):
    return variant.quantity


def get_inventory_key(product, variant):
    if variant is not None:
        return ('variant', variant.pk)
    return ('product', product.pk)


def calculate_shipping(items, delivery_area):
    settings = SiteSettings.objects.first() or SiteSettings()
    if any(item['product'].free_shipping for item in items):
        return Decimal('0')

    if delivery_area == 'inside_dhaka':
        return settings.inside_dhaka_charge
    return settings.outside_dhaka_charge


def order_status_holds_stock(status):
    return status not in STOCK_RETURN_STATUSES


def cart(request):
    return redirect('checkout')


@require_POST
def add_to_cart(request, slug):
    product = get_object_or_404(
        Product.objects.select_related('stock').prefetch_related('variants'),
        slug=slug,
        is_active=True,
    )
    selected_size, selected_color, errors = get_selected_variant(request, product)
    if errors:
        for error in errors:
            messages.error(request, error)
        return redirect('product_detail', slug=product.slug)

    cart = request.session.get('cart', {})
    cart_key = build_cart_key(product, selected_size, selected_color)
    variant = get_product_variant(product, selected_size, selected_color)
    if variant is None or (
        cart_quantity_for_variant(cart, product.pk, selected_size, selected_color) + 1
        > get_variant_stock_quantity(product, variant)
    ):
        messages.error(request, 'The requested quantity is not available in stock.')
        return redirect('product_detail', slug=product.slug)
    cart[cart_key] = int(cart.get(cart_key, 0)) + 1
    request.session['cart'] = cart
    return redirect(request.META.get('HTTP_REFERER', 'shop'))


@require_POST
def buy_now(request, slug):
    product = get_object_or_404(
        Product.objects.select_related('stock').prefetch_related('variants'),
        slug=slug,
        is_active=True,
    )
    selected_size, selected_color, errors = get_selected_variant(request, product)
    if errors:
        for error in errors:
            messages.error(request, error)
        return redirect('product_detail', slug=product.slug)

    cart = request.session.get('cart', {})
    cart_key = build_cart_key(product, selected_size, selected_color)
    variant = get_product_variant(product, selected_size, selected_color)
    if variant is None or (
        cart_quantity_for_variant(cart, product.pk, selected_size, selected_color) + 1
        > get_variant_stock_quantity(product, variant)
    ):
        messages.error(request, 'The requested quantity is not available in stock.')
        return redirect('product_detail', slug=product.slug)
    cart[cart_key] = int(cart.get(cart_key, 0)) + 1
    request.session['cart'] = cart
    return redirect('checkout')


@require_POST
def update_cart(request, slug):
    product = get_object_or_404(
        Product.objects.select_related('stock').prefetch_related('variants'),
        slug=slug,
        is_active=True,
    )
    try:
        quantity = int(request.POST.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 0

    cart_key = get_cart_key_for_request(request, product)
    parsed_key = parse_cart_key(cart_key)
    variant = (
        get_product_variant(product, parsed_key[1], parsed_key[2])
        if parsed_key and parsed_key[0] == product.pk
        else None
    )
    if (
        quantity < 1
        or variant is None
        or quantity > get_variant_stock_quantity(product, variant)
    ):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Enter a quantity available in stock.'}, status=400)
        return redirect('cart')

    cart = request.session.get('cart', {})
    selected_size, selected_color = parsed_key[1], parsed_key[2]
    if (
        cart_quantity_for_variant(
            cart,
            product.pk,
            selected_size,
            selected_color,
            exclude_key=cart_key,
        )
        + quantity
        > get_variant_stock_quantity(product, variant)
    ):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'The requested total quantity is not available in stock.'}, status=400)
        messages.error(request, 'The requested total quantity is not available in stock.')
        return redirect('checkout')
    cart[cart_key] = quantity
    request.session['cart'] = cart

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        items, subtotal = get_cart_items(request)
        delivery_area = request.POST.get('delivery_area', 'inside_dhaka')
        if delivery_area not in dict(Order.DELIVERY_AREA_CHOICES):
            delivery_area = 'inside_dhaka'
        shipping_charge = calculate_shipping(items, delivery_area)
        free_shipping_applies = any(item['product'].free_shipping for item in items)
        updated_item = next(
            (item for item in items if item['cart_key'] == cart_key),
            None,
        )
        if updated_item is None:
            return JsonResponse({'error': 'The selected product variant is no longer available.'}, status=400)
        return JsonResponse({
            'quantity': quantity,
            'line_total': str(updated_item['line_total']),
            'subtotal': str(subtotal),
            'shipping_charge': str(shipping_charge),
            'total': str(subtotal + shipping_charge),
            'shipping_rates': {
                area: str(calculate_shipping(items, area))
                for area, _ in Order.DELIVERY_AREA_CHOICES
            },
            'free_shipping_applies': bool(free_shipping_applies),
        })

    return redirect('cart')


@require_POST
def remove_from_cart(request, slug):
    product = get_object_or_404(Product, slug=slug, is_active=True)
    cart = request.session.get('cart', {})
    cart_key = get_cart_key_for_request(request, product)
    cart.pop(cart_key, None)
    request.session['cart'] = cart
    return redirect('cart')


def checkout(request):
    items, subtotal = get_cart_items(request)
    if not items:
        return redirect('shop')

    form = CheckoutForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        delivery_area = form.cleaned_data['delivery_area']
        shipping_charge = calculate_shipping(items, delivery_area)
        order_total = subtotal + shipping_charge
        requested_variant_quantities = {}
        for item in items:
            variant_id = item['variant'].pk
            requested_variant_quantities[variant_id] = (
                requested_variant_quantities.get(variant_id, 0)
                + item['quantity']
            )

        with transaction.atomic():
            locked_variants = {
                variant.pk: variant
                for variant in ProductVariant.objects.select_for_update().filter(
                    pk__in=requested_variant_quantities
                )
            }
            unavailable_item = next(
                (
                    item for item in items
                    if (
                        item['variant'].pk not in locked_variants
                        or requested_variant_quantities[item['variant'].pk]
                        > locked_variants[item['variant'].pk].quantity
                    )
                ),
                None,
            )
            if unavailable_item is not None:
                form.add_error(
                    None,
                    f'{unavailable_item["product"].name} is no longer available in the requested quantity.',
                )
            else:
                order = form.save(commit=False)
                order.payment_method = 'cod'
                order.payment_number = ''
                order.transaction_reference = ''
                order.payment_amount = None
                order.subtotal = subtotal
                order.shipping_charge = shipping_charge
                order.total = order_total
                order.save()

                variant_products = set()
                for item in items:
                    product = item['product']
                    line_total = item['unit_price'] * item['quantity']
                    OrderItem.objects.create(
                        order=order,
                        product=product,
                        variant=item['variant'],
                        product_name=product.name,
                        quantity=item['quantity'],
                        unit_price=item['unit_price'],
                        line_total=line_total,
                        selected_size=item['selected_size'],
                        selected_color=item['selected_color'],
                    )
                    variant = locked_variants[item['variant'].pk]
                    variant.quantity -= item['quantity']
                    variant.save(update_fields=['quantity'])
                    variant_products.add(product.pk)

                for product_id in variant_products:
                    sync_product_stock(product_id)
                request.session['cart'] = {}
                return redirect('order_success', order_id=order.pk)

    shipping_rate = calculate_shipping(items, request.POST.get('delivery_area', 'inside_dhaka')) if request.method == 'POST' else calculate_shipping(items, 'inside_dhaka')
    shipping_rates = {
        'inside_dhaka': calculate_shipping(items, 'inside_dhaka'),
        'outside_dhaka': calculate_shipping(items, 'outside_dhaka'),
    }
    free_shipping_applies = any(item['product'].free_shipping for item in items)
    context = {
        'form': form,
        'items': items,
        'subtotal': subtotal,
        'shipping_charge': shipping_rate,
        'shipping_rates': shipping_rates,
        'total': subtotal + shipping_rate,
        'free_shipping_applies': free_shipping_applies,
        'site_settings': SiteSettings.objects.first(),
    }
    return render(request, 'checkout.html', context)


def order_success(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    return render(request, 'order_success.html', {'order': order})


def staff_login(request):
    if request.user.is_authenticated:
        return redirect(get_staff_landing_url(request.user))

    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.get_user()
        if not user.is_staff:
            form.add_error(None, 'This account does not have staff access.')
        else:
            login(request, user)
            return redirect(get_staff_landing_url(user))

    return render(request, 'staff_login.html', {'form': form})


@require_POST
def staff_logout(request):
    logout(request)
    return redirect('home')


def sitemap_view(request):
    site_settings = SiteSettings.objects.first()
    origin = get_site_origin(request, site_settings)
    static_urls = [
        {'loc': f'{origin}{reverse("home")}'},
        {'loc': f'{origin}{reverse("shop")}'},
        {'loc': f'{origin}{reverse("categories")}'},
    ]
    product_urls = [
        {'loc': f'{origin}{reverse("product_detail", args=[product.slug])}'}
        for product in Product.objects.filter(is_active=True).only('slug')
    ]

    urls = static_urls + product_urls if not site_settings or site_settings.search_engine_indexing else []
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for entry in urls:
        xml.append('  <url>')
        xml.append(f'    <loc>{entry["loc"]}</loc>')
        xml.append('  </url>')
    xml.append('</urlset>')
    return HttpResponse('\n'.join(xml), content_type='application/xml')


def robots_txt(request):
    site_settings = SiteSettings.objects.first()
    sitemap_url = f'{get_site_origin(request, site_settings)}{reverse("sitemap")}'
    if site_settings and not site_settings.search_engine_indexing:
        return HttpResponse(
            f'User-agent: *\nDisallow: /\nSitemap: {sitemap_url}\n',
            content_type='text/plain',
        )
    return HttpResponse(
        f'User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /staff-dashboard/\n'
        f'Disallow: /super-admin-dashboard/\nDisallow: /admin-dashboard/\n'
        f'Sitemap: {sitemap_url}\n',
        content_type='text/plain',
    )


@require_POST
@staff_member_required(login_url='staff_login')
def update_order_status(request, order_id):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    staff_page = request.POST.get('page', '1')
    staff_redirect = (
        f"{reverse('staff_order_detail', args=[order_id])}?page={staff_page if staff_page.isdigit() else '1'}"
        if request.POST.get('return_to') == 'detail'
        else f"{reverse('staff_dashboard')}?page={staff_page if staff_page.isdigit() else '1'}"
    )
    dashboard_filters = {
        name: request.POST.get(name, '').strip()
        for name in ('start_date', 'end_date')
        if request.POST.get(name, '').strip()
    }
    admin_dashboard_redirect = reverse('admin_dashboard')
    if dashboard_filters:
        admin_dashboard_redirect = f'{admin_dashboard_redirect}?{urlencode(dashboard_filters)}'
    staff_row_update = 'full_name' in request.POST

    def update_response(error=None, redirect_url=staff_redirect, data=None):
        if is_ajax:
            response_data = {'success': error is None}
            if error:
                response_data['error'] = error
            if data:
                response_data.update(data)
            return JsonResponse(response_data, status=400 if error else 200)
        if error:
            messages.error(request, error)
        return redirect(redirect_url)

    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), pk=order_id)
        previous_status = order.status
        added_item = None
        if staff_row_update:
            form_data = request.POST
            if request.POST.get('dashboard_autosave') == '1':
                form_data = request.POST.copy()
                form_data['phone'] = order.phone
            form = StaffOrderUpdateForm(form_data, instance=order)
            if not form.is_valid():
                return update_response(
                    'Order was not updated. Check the customer, delivery details, and cash discount.'
                )
            cash_discount = (
                form.cleaned_data['cash_discount'] or Decimal('0')
                if 'cash_discount' in request.POST
                else order.cash_discount
            )

            order_items = list(
                OrderItem.objects.select_for_update()
                .filter(order=order)
                .select_related('product', 'variant')
                .prefetch_related(
                    'product__variants',
                )
                .order_by('pk')
            )
            deleted_item_ids = {
                item.pk for item in order_items
                if request.POST.get(f'delete_item_{item.pk}', '').lower()
                in {'true', 'on', '1'}
            }
            add_product_value = request.POST.get('item_product_new', '').strip()
            added_product_id = None
            if add_product_value:
                try:
                    added_product_id = int(add_product_value)
                except ValueError:
                    return update_response('Choose a valid product to add to the order.')
            if (
                deleted_item_ids
                and len(deleted_item_ids) == len(order_items)
                and added_product_id is None
            ):
                return update_response(
                    'An order must contain at least one product. Add another product before removing the last item.'
                )

            selected_product_ids = {}
            invalid_product_selection = False
            for item in order_items:
                if item.pk in deleted_item_ids:
                    continue
                raw_product_id = request.POST.get(
                    f'item_product_{item.pk}',
                    str(item.product_id),
                ).strip()
                try:
                    selected_product_ids[item.pk] = int(raw_product_id)
                except (TypeError, ValueError):
                    invalid_product_selection = True
                    break
            product_map = {
                product.pk: product
                for product in Product.objects.select_for_update()
                .filter(
                    pk__in=set(selected_product_ids.values())
                    | {item.product_id for item in order_items}
                    | ({added_product_id} if added_product_id is not None else set())
                )
                .prefetch_related('variants')
            }
            if invalid_product_selection or any(
                product_id not in product_map
                for product_id in selected_product_ids.values()
            ):
                return update_response(
                    'Order was not updated. Choose a valid product for every order item.'
                )
            if added_product_id is not None:
                added_product = product_map.get(added_product_id)
                if added_product is None or not added_product.is_active:
                    return update_response('Choose an active product to add to the order.')
                try:
                    added_quantity = int(request.POST.get('item_quantity_new', '').strip())
                except (TypeError, ValueError):
                    added_quantity = 0
                added_size_options = {
                    variant.size for variant in added_product.variants.all() if variant.size
                }
                added_color_options = {
                    variant.color for variant in added_product.variants.all() if variant.color
                }
                added_size = request.POST.get('item_size_new', '').strip()
                added_color = request.POST.get('item_color_new', '').strip()
                if len(added_size_options) == 1 and not added_size:
                    added_size = next(iter(added_size_options))
                if len(added_color_options) == 1 and not added_color:
                    added_color = next(iter(added_color_options))
                added_variant = get_product_variant(added_product, added_size, added_color)
                if (
                    added_quantity < 1
                    or len(added_size) > OrderItem._meta.get_field('selected_size').max_length
                    or len(added_color) > OrderItem._meta.get_field('selected_color').max_length
                    or (len(added_size_options) > 1 and not added_size)
                    or (len(added_color_options) > 1 and not added_color)
                    or (added_size and added_size not in added_size_options)
                    or (added_color and added_color not in added_color_options)
                    or added_variant is None
                ):
                    return update_response(
                        'Order was not updated. Choose a valid size, color, and quantity for the added product.'
                    )
                added_item = {
                    'product': added_product,
                    'variant': added_variant,
                    'quantity': added_quantity,
                    'size': added_size,
                    'color': added_color,
                    'unit_price': added_variant.price,
                }
            else:
                added_item = None

            configured_options = {}
            for item in order_items:
                if item.pk in deleted_item_ids:
                    continue
                selected_product = product_map[selected_product_ids[item.pk]]
                configured_options[(item.pk, 'size')] = {
                    variant.size
                    for variant in selected_product.variants.all()
                    if variant.size
                }
                configured_options[(item.pk, 'color')] = {
                    variant.color
                    for variant in selected_product.variants.all()
                    if variant.color
                }
            new_quantities = {}
            new_sizes = {}
            new_colors = {}
            new_variants = {}
            quantity_errors = False
            for item in order_items:
                if item.pk in deleted_item_ids:
                    continue
                raw_quantity = request.POST.get(f'item_quantity_{item.pk}', '').strip()
                try:
                    quantity = int(raw_quantity)
                except (TypeError, ValueError):
                    quantity = 0
                if quantity < 1:
                    quantity_errors = True
                    break
                new_quantities[item.pk] = quantity
                selected_product = product_map[selected_product_ids[item.pk]]
                size_options = configured_options[(item.pk, 'size')]
                color_options = configured_options[(item.pk, 'color')]
                if f'item_size_{item.pk}' in request.POST:
                    new_size = request.POST[f'item_size_{item.pk}'].strip()
                elif len(size_options) == 1:
                    new_size = next(iter(size_options))
                elif selected_product.pk == item.product_id:
                    new_size = item.selected_size
                else:
                    new_size = ''
                if f'item_color_{item.pk}' in request.POST:
                    new_color = request.POST[f'item_color_{item.pk}'].strip()
                elif len(color_options) == 1:
                    new_color = next(iter(color_options))
                elif selected_product.pk == item.product_id:
                    new_color = item.selected_color
                else:
                    new_color = ''
                if (
                    len(new_size) > OrderItem._meta.get_field('selected_size').max_length
                    or len(new_color) > OrderItem._meta.get_field('selected_color').max_length
                    or (len(size_options) > 1 and not new_size)
                    or (len(color_options) > 1 and not new_color)
                    or (
                        new_size
                        and size_options
                        and new_size not in size_options
                        and not (
                            selected_product.pk == item.product_id
                            and new_size == item.selected_size
                        )
                    )
                    or (
                        new_color
                        and color_options
                        and new_color not in color_options
                        and not (
                            selected_product.pk == item.product_id
                            and new_color == item.selected_color
                        )
                    )
                ):
                    quantity_errors = True
                    break
                new_sizes[item.pk] = new_size
                new_colors[item.pk] = new_color
                variant = get_product_variant(selected_product, new_size, new_color)
                if variant is None:
                    quantity_errors = True
                    break
                new_variants[item.pk] = variant

            if quantity_errors:
                return update_response(
                    'Order was not updated. Check that quantities are whole numbers greater than zero and each size/color is valid for its product.',
                )

            order = form.save(commit=False)
            new_status = order.status
            stock_changes = {}
            for item in order_items:
                old_product = product_map.get(item.product_id)
                if old_product and order_status_holds_stock(previous_status):
                    old_key = get_inventory_key(old_product, item.variant)
                    stock_changes[old_key] = (
                        stock_changes.get(old_key, 0) + item.quantity
                    )
                if item.pk in deleted_item_ids:
                    continue
                new_product = product_map[selected_product_ids[item.pk]]
                if order_status_holds_stock(new_status):
                    new_key = get_inventory_key(new_product, new_variants[item.pk])
                    stock_changes[new_key] = (
                        stock_changes.get(new_key, 0) - new_quantities[item.pk]
                    )
            if added_item and order_status_holds_stock(new_status):
                added_key = get_inventory_key(added_item['product'], added_item['variant'])
                stock_changes[added_key] = (
                    stock_changes.get(added_key, 0) - added_item['quantity']
                )

            variant_ids = [
                key[1] for key in stock_changes if key[0] == 'variant'
            ]
            product_ids = [
                key[1] for key in stock_changes if key[0] == 'product'
            ]
            locked_variants = {
                variant.pk: variant
                for variant in ProductVariant.objects.select_for_update().filter(
                    pk__in=variant_ids
                )
            }
            locked_stocks = {
                stock.product_id: stock
                for stock in Stock.objects.select_for_update().filter(
                    product_id__in=product_ids
                )
            }
            if any(
                (
                    key[1] not in (
                        locked_variants if key[0] == 'variant' else locked_stocks
                    )
                    or -change > (
                        locked_variants[key[1]].quantity
                        if key[0] == 'variant'
                        else locked_stocks[key[1]].quantity
                    )
                )
                for key, change in stock_changes.items()
                if change < 0
            ):
                return update_response(
                    'Order was not updated. There is not enough stock for the requested product quantities.',
                )

            product_free_shipping = {}
            for item in order_items:
                if item.pk in deleted_item_ids:
                    continue
                field_name = f'item_free_shipping_{item.pk}'
                if field_name not in request.POST:
                    continue
                is_free_shipping = request.POST.get(field_name, '').lower() in {'true', 'on', '1'}
                product_id = selected_product_ids[item.pk]
                existing_choice = product_free_shipping.get(product_id)
                if existing_choice is not None and existing_choice != is_free_shipping:
                    return update_response(
                        'Order was not updated. A product can only have one free-shipping setting.',
                    )
                product_free_shipping[product_id] = is_free_shipping

            line_subtotal = Decimal('0')
            shipping_items = []
            variant_products = set()
            updated_items = []
            for item in order_items:
                if item.pk in deleted_item_ids:
                    continue
                selected_product = product_map[selected_product_ids[item.pk]]
                selected_variant = new_variants[item.pk]
                if selected_variant.pk != item.variant_id:
                    item.product = selected_product
                    item.product_name = selected_product.name
                    item.variant = selected_variant
                    item.unit_price = selected_variant.price
                item.quantity = new_quantities[item.pk]
                item.selected_size = new_sizes[item.pk]
                item.selected_color = new_colors[item.pk]
                item.line_total = item.unit_price * item.quantity
                line_subtotal += item.line_total
                updated_items.append(item)
                shipping_items.append({
                    'product': selected_product,
                    'quantity': item.quantity,
                    'line_total': item.line_total,
                })

            if added_item:
                added_line_total = added_item['unit_price'] * added_item['quantity']
                line_subtotal += added_line_total
                shipping_items.append({
                    'product': added_item['product'],
                    'quantity': added_item['quantity'],
                    'line_total': added_line_total,
                })

            for product_id, is_free_shipping in product_free_shipping.items():
                product_map[product_id].free_shipping = is_free_shipping
            shipping_charge = calculate_shipping(shipping_items, order.delivery_area)
            gross_total = line_subtotal + shipping_charge
            if cash_discount > gross_total:
                return update_response(
                    'Order was not updated. Cash discount cannot exceed the order total.'
                )

            for item in updated_items:
                item.save(update_fields=[
                    'product',
                    'variant',
                    'product_name',
                    'unit_price',
                    'quantity',
                    'selected_size',
                    'selected_color',
                    'line_total',
                ])
            if deleted_item_ids:
                OrderItem.objects.filter(
                    order=order,
                    pk__in=deleted_item_ids,
                ).delete()
            if added_item:
                added_line_total = added_item['unit_price'] * added_item['quantity']
                OrderItem.objects.create(
                    order=order,
                    product=added_item['product'],
                    variant=added_item['variant'],
                    product_name=added_item['product'].name,
                    quantity=added_item['quantity'],
                    unit_price=added_item['unit_price'],
                    line_total=added_line_total,
                    selected_size=added_item['size'],
                    selected_color=added_item['color'],
                )

            for (stock_type, stock_id), stock_change in stock_changes.items():
                if stock_type == 'variant':
                    variant = locked_variants[stock_id]
                    variant.quantity += stock_change
                    variant.save(update_fields=['quantity'])
                    variant_products.add(variant.product_id)
                else:
                    stock = locked_stocks[stock_id]
                    stock.quantity += stock_change
                    stock.save(update_fields=['quantity'])
            for product_id in variant_products:
                sync_product_stock(product_id)

            for product_id in product_free_shipping:
                product = product_map[product_id]
                product.free_shipping = product_free_shipping[product_id]
                product.save(update_fields=['free_shipping'])

            order.subtotal = line_subtotal
            order.shipping_charge = shipping_charge
            order.cash_discount = cash_discount
            order.total = gross_total - cash_discount
            if 'fulfillment_note' in request.POST:
                order.fulfillment_note = request.POST['fulfillment_note'].strip()
            order.save()
            if not is_ajax:
                messages.success(
                    request,
                    f'Order #{order.display_order_number} details and quantities updated.',
                )
        else:
            new_status = request.POST.get('status', '').strip()
            valid_statuses = {value for value, _ in Order.STATUS_CHOICES}
            if new_status not in valid_statuses:
                return update_response(
                    'Choose a valid order status.',
                    redirect_url=admin_dashboard_redirect if request.user.is_superuser else 'staff_dashboard',
                )
            if previous_status != new_status and (
                not order_status_holds_stock(previous_status)
                or not order_status_holds_stock(new_status)
            ):
                status_order_items = list(
                    OrderItem.objects.select_for_update()
                    .filter(order=order)
                    .select_related('product', 'variant')
                )
                stock_changes = {}
                for item in status_order_items:
                    if not item.product_id:
                        continue
                    stock_key = get_inventory_key(item.product, item.variant)
                    if order_status_holds_stock(previous_status):
                        stock_changes[stock_key] = (
                            stock_changes.get(stock_key, 0) + item.quantity
                        )
                    if order_status_holds_stock(new_status):
                        stock_changes[stock_key] = (
                            stock_changes.get(stock_key, 0) - item.quantity
                        )
                variant_ids = [
                    key[1] for key in stock_changes if key[0] == 'variant'
                ]
                product_ids = [
                    key[1] for key in stock_changes if key[0] == 'product'
                ]
                status_variants = {
                    variant.pk: variant
                    for variant in ProductVariant.objects.select_for_update().filter(
                        pk__in=variant_ids
                    )
                }
                status_stocks = {
                    stock.product_id: stock
                    for stock in Stock.objects.select_for_update().filter(
                        product_id__in=product_ids
                    )
                }
                if any(
                    (
                        key[1] not in (
                            status_variants if key[0] == 'variant' else status_stocks
                        )
                        or -change > (
                            status_variants[key[1]].quantity
                            if key[0] == 'variant'
                            else status_stocks[key[1]].quantity
                        )
                    )
                    for key, change in stock_changes.items()
                    if change < 0
                ):
                    return update_response(
                        'Order status was not changed. There is not enough stock to reserve these units.',
                        redirect_url=admin_dashboard_redirect if request.user.is_superuser else 'staff_dashboard',
                    )
                variant_products = set()
                for (stock_type, stock_id), stock_change in stock_changes.items():
                    if stock_type == 'variant':
                        variant = status_variants[stock_id]
                        variant.quantity += stock_change
                        variant.save(update_fields=['quantity'])
                        variant_products.add(variant.product_id)
                    else:
                        stock = status_stocks[stock_id]
                        stock.quantity += stock_change
                        stock.save(update_fields=['quantity'])
                for product_id in variant_products:
                    sync_product_stock(product_id)
            order.status = new_status
            order.save(update_fields=['status', 'updated_at'])

    event_warning = None
    if order.status in {'confirmed', 'delivered'}:
        try:
            sent = send_order_purchase_event(order, request)
        except FacebookPurchaseEventError as error:
            event_warning = str(error)
            if not is_ajax:
                messages.error(request, event_warning)
        else:
            if sent and not is_ajax:
                messages.success(request, 'Facebook Purchase event sent for this confirmed order.')

    if is_ajax:
        response_data = {
            'status': order.status,
            'subtotal': str(order.subtotal),
            'shipping_charge': str(order.shipping_charge),
            'cash_discount': str(order.cash_discount),
            'total': str(order.total),
            'added_item': bool(added_item),
        }
        if event_warning:
            response_data['warning'] = event_warning
        return update_response(data=response_data)
    if 'full_name' in request.POST:
        return redirect(staff_redirect)
    return redirect(admin_dashboard_redirect if request.user.is_superuser else 'staff_dashboard')


@staff_member_required(login_url='staff_login')
def admin_dashboard(request):
    if not request.user.is_superuser and not request.user.groups.filter(name='Owner').exists():
        return redirect('staff_dashboard')

    filter_form = StaffOrderFilterForm(request.GET or None)
    orders = Order.objects.all()
    if filter_form.is_valid():
        filters = filter_form.cleaned_data
        if filters.get('start_date'):
            orders = orders.filter(created_at__date__gte=filters['start_date'])
        if filters.get('end_date'):
            orders = orders.filter(created_at__date__lte=filters['end_date'])
    elif request.GET:
        orders = orders.none()

    total_orders = orders.count()
    delivered_statuses = {'delivered', 'completed'}
    total_revenue = orders.filter(status__in=delivered_statuses).aggregate(total=Sum('total'))['total'] or Decimal('0')
    order_totals_by_status = {
        row['status']: row
        for row in orders.values('status').annotate(order_count=Count('pk'), amount=Sum('total'))
    }
    status_summary = [
        {
            'value': status,
            'label': label,
            'order_count': order_totals_by_status.get(status, {}).get('order_count', 0),
            'amount': order_totals_by_status.get(status, {}).get('amount') or Decimal('0'),
        }
        for status, label in Order.STATUS_CHOICES
    ]
    known_statuses = {status for status, _ in Order.STATUS_CHOICES}
    status_summary.extend(
        {
            'value': status,
            'label': status.replace('_', ' ').title(),
            'order_count': row['order_count'],
            'amount': row['amount'] or Decimal('0'),
        }
        for status, row in order_totals_by_status.items()
        if status not in known_statuses
    )
    low_stock_products = (
        Product.objects.filter(stock__quantity__lte=5)
        .select_related('stock')
        .order_by('stock__quantity', 'name')
    )
    low_stock_count = low_stock_products.count()
    order_details = Paginator(
        orders.order_by('-created_at', '-pk'),
        20,
    ).get_page(request.GET.get('orders_page'))
    best_selling = list(
        OrderItem.objects.filter(order__in=orders)
        .exclude(order__status__in=['returned', 'cancelled'])
        .values('product_id', 'product_name', 'product__category__name')
        .annotate(total_sold=Sum('quantity'), total_sales=Sum('line_total'))
        .order_by('-total_sold', 'product_name')[:5]
    )

    context = {
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'low_stock_products': low_stock_products,
        'low_stock_count': low_stock_count,
        'best_selling': best_selling,
        'status_summary': status_summary,
        'order_details': order_details,
        'filter_form': filter_form,
        'selected_start_date': filter_form['start_date'].value(),
        'selected_end_date': filter_form['end_date'].value(),
        'status_choices': Order.STATUS_CHOICES,
    }
    return render(request, 'admin_dashboard.html', context)


@staff_member_required(login_url='staff_login')
def super_admin_dashboard(request):
    if not request.user.is_superuser:
        return redirect('admin_dashboard')

    return render(
        request,
        'super_admin_dashboard.html',
        {
            'admin_apps': admin.site.get_app_list(request),
        },
    )


def get_staff_product_options():
    products = list(
        Product.objects.select_related('stock')
        .prefetch_related('variants')
        .order_by('name')
    )
    catalog = [
        {
            'id': product.pk,
            'free_shipping': product.free_shipping,
            'sizes': product.variant_sizes,
            'colors': product.variant_colors,
            'variants': [
                {
                    'size': variant.size,
                    'color': variant.color,
                    'price': str(variant.price),
                    'quantity': variant.quantity,
                }
                for variant in get_active_product_variants(product)
            ],
        }
        for product in products
    ]
    return products, catalog


@staff_member_required(login_url='staff_login')
def staff_dashboard(request):
    filter_form = StaffOrderFilterForm(request.GET or None)
    latest_call_log = StaffCallLog.objects.filter(
        order_id=OuterRef('pk'),
    ).order_by('-created_at', '-pk')
    orders_queryset = (
        Order.objects.prefetch_related(
            'items__product__variants',
        )
        .select_related('pathao_shipment')
        .annotate(
            call_log_count=Count('call_logs'),
            latest_call_note=Subquery(latest_call_log.values('note')[:1]),
            latest_call_staff=Subquery(
                latest_call_log.values('staff_member__username')[:1]
            ),
            latest_call_at=Subquery(latest_call_log.values('created_at')[:1]),
        )
        .order_by('-created_at')
    )
    if filter_form.is_bound:
        if filter_form.is_valid():
            orders_queryset = filter_staff_orders(orders_queryset, filter_form.cleaned_data)
        else:
            orders_queryset = orders_queryset.none()

    page_number = request.GET.get('page')
    orders = Paginator(orders_queryset, 20).get_page(page_number)
    staff_products, product_catalog = get_staff_product_options()
    site_settings = SiteSettings.objects.first() or SiteSettings()
    pathao_status_sync_enabled = is_pathao_configured(site_settings)
    pending_pathao_shipment_count = PathaoShipment.objects.filter(
        environment__in=(site_settings.pathao_environment, ''),
        consignment_id__gt='',
        order_status__iexact='Pending',
    ).count()
    active_pathao_status_sync = PathaoStatusSyncRun.objects.filter(
        status__in=('queued', 'running'),
    ).first()
    context = {
        'orders': orders,
        'filter_form': filter_form,
        'filter_query': request.GET.copy(),
        'status_choices': Order.STATUS_CHOICES,
        'delivery_area_choices': Order.DELIVERY_AREA_CHOICES,
        'shipping_rates': {
            'inside_dhaka': site_settings.inside_dhaka_charge,
            'outside_dhaka': site_settings.outside_dhaka_charge,
        },
        'staff_products': staff_products,
        'product_catalog': product_catalog,
        'pathao_status_sync_enabled': pathao_status_sync_enabled,
        'pending_pathao_shipment_count': pending_pathao_shipment_count,
        'active_pathao_status_sync': active_pathao_status_sync,
        'latest_pathao_status_sync': PathaoStatusSyncRun.objects.exclude(
            status__in=('queued', 'running'),
        ).first(),
    }
    context['filter_query'].pop('page', None)
    context['filter_query'] = context['filter_query'].urlencode()
    return render(request, 'staff_dashboard.html', context)


@require_POST
@staff_member_required(login_url='staff_login')
def start_pathao_status_sync(request):
    site_settings = SiteSettings.objects.first()
    if site_settings is None or not is_pathao_configured(site_settings):
        messages.error(request, 'Configure Pathao credentials in Site Settings before syncing shipments.')
        return redirect('staff_dashboard')
    pending_shipments = PathaoShipment.objects.filter(
        environment__in=(site_settings.pathao_environment, ''),
        consignment_id__gt='',
        order_status__iexact='Pending',
    )
    if not pending_shipments.exists():
        messages.info(request, 'There are no Pathao shipments to sync.')
        return redirect('staff_dashboard')

    try:
        with transaction.atomic():
            run = PathaoStatusSyncRun.objects.create(
                requested_by=request.user,
            )
    except IntegrityError:
        active_run = PathaoStatusSyncRun.objects.filter(
            status__in=('queued', 'running'),
        ).first()
        messages.info(
            request,
            f'Pathao status sync is already {active_run.get_status_display().lower()}.',
        )
        return redirect('staff_dashboard')

    command = [
        sys.executable,
        str(django_settings.BASE_DIR / 'manage.py'),
        'sync_pathao_shipment_statuses',
        '--run-id',
        str(run.pk),
    ]
    try:
        subprocess.Popen(
            command,
            cwd=django_settings.BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as error:
        logger.exception('Could not start Pathao status sync run %s.', run.pk)
        run.status = 'failed'
        run.error_summary = f'Could not start background sync: {error}'[:2000]
        run.completed_at = timezone.now()
        run.save(update_fields=['status', 'error_summary', 'completed_at'])
        messages.error(request, 'Could not start Pathao status sync. Check the server logs.')
        return redirect('staff_dashboard')

    messages.success(
        request,
        'Pathao status sync started in the background. Shipments are checked one at a time.',
    )
    return redirect('staff_dashboard')


@staff_member_required(login_url='staff_login')
def staff_inventory(request):
    stock_products = (
        Product.objects.filter(is_active=True)
        .select_related('category', 'stock')
        .prefetch_related('variants')
        .only(
            'name',
            'category__name',
            'stock__opening_quantity',
            'stock__restocked_quantity',
            'stock__quantity',
        )
        .annotate(
            shipped_quantity=Sum(
                'order_items__quantity',
                filter=Q(order_items__order__status='shipped'),
                default=0,
            ),
            delivered_quantity=Sum(
                'order_items__quantity',
                filter=Q(order_items__order__status='delivered'),
                default=0,
            ),
            returned_quantity=Sum(
                'order_items__quantity',
                filter=Q(order_items__order__status='returned'),
                default=0,
            ),
        )
        .order_by('stock__quantity', 'name')
    )
    stock_products = list(stock_products)
    for product in stock_products:
        product.restocked_quantity = product.stock.restocked_quantity
        variants = get_active_product_variants(product)
        for variant in variants:
            variant.inventory_opening_quantity = variant.opening_quantity
            variant.inventory_restocked_quantity = variant.restocked_quantity
            variant.inventory_quantity = variant.quantity
        product.inventory_variants = variants
        size_options = product.variant_sizes
        color_options = product.variant_colors
        requested_size = request.GET.get(f'size_{product.pk}', '')
        requested_color = request.GET.get(f'color_{product.pk}', '')
        selected_variant = next(
            (
                variant for variant in variants
                if variant.size == requested_size and variant.color == requested_color
            ),
            variants[0] if variants else None,
        )
        product.variant_editor = {
            'size_options': size_options,
            'color_options': color_options,
            'variants': [
                {
                    'id': variant.pk,
                    'size': variant.size,
                    'color': variant.color,
                    'opening_quantity': variant.inventory_opening_quantity,
                    'quantity': variant.inventory_quantity,
                }
                for variant in variants
            ],
            'selected_variant': selected_variant,
        }
    return render(
        request,
        'staff_inventory.html',
        {
            'stock_products': stock_products,
            'variant_editor_catalog': [
                {
                    'id': product.pk,
                    'variants': product.variant_editor['variants'],
                }
                for product in stock_products
            ],
        },
    )


@require_POST
@staff_member_required(login_url='staff_login')
def update_product_variants(request, product_id):
    with transaction.atomic():
        product = get_object_or_404(
            Product.objects.select_for_update()
            .select_related('stock')
            .prefetch_related('variants'),
            pk=product_id,
            is_active=True,
        )
        variants = list(
            ProductVariant.objects.select_for_update()
            .filter(product=product)
            .order_by('size', 'color', 'pk')
        )
        active_variants = get_active_product_variants(product)
        active_variant_ids = {variant.pk for variant in active_variants}
        variants = [variant for variant in variants if variant.pk in active_variant_ids]
        if not variants:
            messages.error(request, 'No size/color variants are available to update.')
            return redirect(f'{reverse("staff_inventory")}#product-stock-{product_id}')

        selected_size = request.POST.get('size', '').strip()
        selected_color = request.POST.get('color', '').strip()
        raw_variant_id = request.POST.get('variant_id', '').strip()
        try:
            selected_variant_id = int(raw_variant_id)
        except (TypeError, ValueError):
            selected_variant_id = 0
        selected_variant = next(
            (
                variant for variant in variants
                if variant.pk == selected_variant_id
                and variant.size == selected_size
                and variant.color == selected_color
            ),
            None,
        )
        form = StaffProductVariantForm(request.POST)
        if not form.is_valid() or selected_variant is None:
            messages.error(
                request,
                'Stock was not updated. Choose an existing size/color combination and enter valid whole-number stock quantities.',
            )
            return redirect(f'{reverse("staff_inventory")}#product-stock-{product_id}')

        data = form.cleaned_data
        restock_quantity = data.get('restock_quantity') or 0
        variant_states = {}
        for variant in variants:
            variant_states[variant.pk] = {
                'opening_quantity': variant.opening_quantity,
                'quantity': variant.quantity,
                'restocked_quantity': variant.restocked_quantity,
            }

        selected_state = variant_states[selected_variant.pk]
        opening_quantity = data.get('opening_quantity')
        if opening_quantity is None:
            opening_quantity = selected_state['opening_quantity']
        quantity = selected_state['quantity']
        if (
            opening_quantity != selected_state['opening_quantity']
            and quantity == selected_state['opening_quantity']
        ):
            quantity = opening_quantity
        selected_state.update({
            'opening_quantity': opening_quantity,
            'quantity': quantity + restock_quantity,
            'restocked_quantity': selected_state['restocked_quantity'] + restock_quantity,
        })

        projected_totals = {
            'quantity': 0,
            'opening_quantity': 0,
            'restocked_quantity': 0,
        }
        for state in variant_states.values():
            if any(value > 2147483647 for value in state.values()):
                messages.error(
                    request,
                    'Stock was not updated. A variant would exceed the supported stock limit.',
                )
                return redirect(f'{reverse("staff_inventory")}#product-stock-{product_id}')
            for field, value in state.items():
                projected_totals[field] += value
        if any(value > 2147483647 for value in projected_totals.values()):
            messages.error(
                request,
                'Stock was not updated. The combined product stock would exceed the supported limit.',
            )
            return redirect(f'{reverse("staff_inventory")}#product-stock-{product_id}')

        ProductVariant.objects.filter(pk=selected_variant.pk).update(
            **variant_states[selected_variant.pk],
        )
        sync_product_stock(product.pk)

    messages.success(
        request,
        f'Stock updated for {product.name} ({selected_size or "Standard"} / {selected_color or "Standard"}).',
    )
    query = urlencode({
        f'size_{product.pk}': selected_size,
        f'color_{product.pk}': selected_color,
    })
    return redirect(f'{reverse("staff_inventory")}?{query}#product-stock-{product_id}')


def filter_staff_orders(queryset, filters):
    if filters.get('start_date'):
        queryset = queryset.filter(created_at__date__gte=filters['start_date'])
    if filters.get('end_date'):
        queryset = queryset.filter(created_at__date__lte=filters['end_date'])
    if filters.get('status'):
        queryset = queryset.filter(status=filters['status'])
    return queryset


@require_POST
@staff_member_required(login_url='staff_login')
def restock_product(request, product_id):
    inventory_url = f"{reverse('staff_inventory')}#product-stock-{product_id}"
    raw_quantity = request.POST.get('quantity', '').strip()
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError):
        quantity = 0

    if quantity < 1 or quantity > 2147483647:
        messages.error(request, 'Enter a whole-number restock quantity greater than zero.')
        return redirect(inventory_url)

    with transaction.atomic():
        product = get_object_or_404(
            Product.objects.select_for_update().select_related('stock'),
            pk=product_id,
            is_active=True,
        )
        variant = ProductVariant.objects.select_for_update().filter(
            product=product,
            size='',
            color='',
        ).first()
        if variant is None:
            messages.error(request, 'Choose a size/color variant and record its restock in the inventory table.')
            return redirect(inventory_url)
        if (
            variant.quantity + quantity > 2147483647
            or variant.restocked_quantity + quantity > 2147483647
            or product.stock.quantity + quantity > 2147483647
            or product.stock.restocked_quantity + quantity > 2147483647
        ):
            messages.error(request, 'The restock quantity exceeds the supported stock limit.')
            return redirect(inventory_url)

        variant.quantity += quantity
        variant.restocked_quantity += quantity
        variant.save(update_fields=['quantity', 'restocked_quantity'])
        sync_product_stock(product.pk)

    messages.success(
        request,
        f'Added {quantity} units to {product.name}. Available stock is now {product.stock.quantity}.',
    )
    return redirect(inventory_url)


@staff_member_required(login_url='staff_login')
def export_staff_orders(request):
    filter_form = StaffOrderFilterForm(request.GET)
    if not filter_form.is_valid():
        return HttpResponseBadRequest(
            'Invalid order filters. Provide valid dates, a valid status, and a start date no later than the end date.'
        )

    orders = list(
        filter_staff_orders(
            Order.objects.prefetch_related('items').order_by('-created_at'),
            filter_form.cleaned_data,
        )
    )
    merchant_order_ids = {
        str(order.display_order_number)
        for order in orders
    }
    shipments_by_merchant_order_id = {
        shipment.merchant_order_id: shipment
        for shipment in PathaoShipment.objects.filter(
            merchant_order_id__in=merchant_order_ids,
        ).only('merchant_order_id', 'order_status', 'consignment_id')
    }
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = 'Orders'
    headers = [
        'Order number',
        'Pathao Status',
        'Consignment ID',
        'Date',
        'Status',
        'Customer',
        'Phone',
        'Email',
        'Address',
        'Thana',
        'Zilla',
        'Delivery area',
        'Items',
        'Payment method',
        'Payment number',
        'Transaction reference',
        'Customer note',
    ]
    worksheet.append(headers)
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
        cell.data_type = 's'

    for order in orders:
        shipment = shipments_by_merchant_order_id.get(
            str(order.display_order_number),
        )
        item_details = '\n'.join(
            f'{item.product_name} (Qty: {item.quantity}, Size: {item.selected_size or "-"}, '
            f'Color: {item.selected_color or "-"})'
            for item in order.items.all()
        )
        worksheet.append([
            order.display_order_number,
            shipment.order_status if shipment else '',
            shipment.consignment_id if shipment else '',
            timezone.localtime(order.created_at).replace(tzinfo=None),
            order.get_status_display(),
            order.full_name,
            order.phone,
            order.email or '',
            order.address,
            order.thana,
            order.zilla,
            order.get_delivery_area_display(),
            item_details,
            order.get_payment_method_display(),
            order.payment_number,
            order.transaction_reference,
            order.note,
        ])
        for cell in worksheet[worksheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = 's'

    worksheet.freeze_panes = 'A2'
    worksheet.auto_filter.ref = worksheet.dimensions
    for column_cells in worksheet.columns:
        column_letter = get_column_letter(column_cells[0].column)
        worksheet.column_dimensions[column_letter].width = min(
            max(max(len(str(cell.value or '')) for cell in column_cells) + 2, 12),
            45,
        )

    output = BytesIO()
    workbook.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename="orders.xlsx"'
    return response


@staff_member_required(login_url='staff_login')
def staff_order_detail(request, order_id):
    order = get_object_or_404(
        Order.objects.prefetch_related(
            'items__product__variants',
        ),
        pk=order_id,
    )
    page = request.GET.get('page', '1')
    page = page if page.isdigit() else '1'
    staff_products, product_catalog = get_staff_product_options()
    site_settings = SiteSettings.objects.first() or SiteSettings()
    call_logs = order.call_logs.select_related('staff_member').all()
    call_log_count = order.call_logs.count()
    context = {
        'order': order,
        'status_choices': Order.STATUS_CHOICES,
        'delivery_area_choices': Order.DELIVERY_AREA_CHOICES,
        'shipping_rates': {
            'inside_dhaka': site_settings.inside_dhaka_charge,
            'outside_dhaka': site_settings.outside_dhaka_charge,
        },
        'dashboard_url': f"{reverse('staff_dashboard')}?page={page}",
        'page': page,
        'staff_products': staff_products,
        'product_catalog': product_catalog,
        'call_logs': call_logs,
        'call_log_count': call_log_count,
        'pathao_configured': is_pathao_configured(site_settings),
    }
    return render(request, 'staff_order_detail.html', context)


@require_POST
@staff_member_required(login_url='staff_login')
def create_pathao_shipment(request, order_id):
    settings = SiteSettings.objects.first() or SiteSettings()
    settings.pathao_environment = 'production'
    if not is_pathao_configured(settings):
        messages.error(
            request,
            'Login message: Not attempted. Configure Pathao production credentials in Site Settings.',
        )
        messages.error(
            request,
            'Order create message: Not attempted because Pathao login is not configured.',
        )
        return redirect('staff_pathao_order', order_id=order_id)

    try:
        with transaction.atomic():
            order = get_object_or_404(
                Order.objects.select_for_update().prefetch_related('items'),
                pk=order_id,
            )
            if order.pathao_consignment_id:
                messages.error(
                    request,
                    f'Order create message: Shipment already exists: {order.pathao_consignment_id}.',
                )
                return redirect('staff_pathao_order', order_id=order_id)
            if order.status != 'confirmed':
                messages.error(
                    request,
                    'Order create message: Not attempted. Confirm this order first.',
                )
                return redirect('staff_pathao_order', order_id=order_id)
            shipment = create_pathao_order(settings, order)
            PathaoShipment.objects.create(
                order=order,
                environment=settings.pathao_environment,
                consignment_id=shipment['consignment_id'],
                merchant_order_id=shipment.get('merchant_order_id')
                or str(order.display_order_number),
                order_status=shipment['status'],
                delivery_fee=shipment.get('delivery_fee'),
                message=shipment.get('message', ''),
                response_type=shipment.get('type', ''),
                response_code=shipment.get('code'),
            )
            order.pathao_consignment_id = shipment['consignment_id']
            order.pathao_order_status = shipment['status']
            order.pathao_error = ''
            order.save(update_fields=[
                'pathao_consignment_id',
                'pathao_order_status',
                'pathao_error',
                'updated_at',
            ])
    except PathaoApiError as error:
        Order.objects.filter(pk=order_id, pathao_consignment_id='').update(
            pathao_error=str(error)[:2000],
            updated_at=timezone.now(),
        )
        if error.stage == 'Login':
            login_status = (
                f'HTTP {error.status_code}'
                if error.status_code is not None
                else 'no HTTP response received'
            )
            messages.error(
                request,
                f'Login message: Failed ({login_status}): {error}',
            )
            messages.error(
                request,
                'Order create message: Not attempted because Pathao login failed.',
            )
        else:
            if error.login_status_code is not None:
                messages.success(
                    request,
                    f'Login message: Successful (HTTP {error.login_status_code}).',
                )
            elif error.stage is None:
                messages.info(
                    request,
                    'Login message: Not attempted because order validation failed.',
                )
            else:
                messages.info(
                    request,
                    'Login message: Access token reused; previous HTTP status is unavailable.',
                )
            order_status = (
                f'HTTP {error.status_code}'
                if error.status_code is not None
                else 'no HTTP response received'
            )
            messages.error(
                request,
                f'Order create message: Pathao shipment was not created ({order_status}): {error}',
            )
        return redirect('staff_pathao_order', order_id=order_id)

    login_status = shipment.get('login_status_code')
    login_result = (
        f'HTTP {login_status}'
        if login_status is not None
        else 'HTTP status unavailable (cached token or response omitted it)'
    )
    messages.success(request, f'Login message: Access token accepted ({login_result}).')
    order_status = shipment.get('order_status_code')
    order_result = (
        f'HTTP {order_status}'
        if order_status is not None
        else 'HTTP status unavailable'
    )
    messages.success(
        request,
        f"Order create message: Pathao shipment created ({order_result}). "
        f"Consignment ID: {shipment['consignment_id']}.",
    )

    try:
        shipment_info = get_pathao_order_info(settings, shipment['consignment_id'])
    except PathaoApiError as error:
        PathaoShipment.objects.filter(order_id=order_id).update(
            info_error=str(error)[:2000],
            status_checked_at=timezone.now(),
            updated_at=timezone.now(),
        )
        messages.warning(
            request,
            f"Pathao shipment created, but its latest details could not be fetched: {error}",
        )
    else:
        shipment_record = PathaoShipment.objects.get(order_id=order_id)
        updated_fields = ['info_error', 'status_checked_at', 'updated_at']
        shipment_record.info_error = ''
        shipment_record.status_checked_at = timezone.now()
        if 'order_status' in shipment_info:
            shipment_record.order_status = shipment_info['order_status']
            order.pathao_order_status = shipment_info['order_status']
            order.save(update_fields=['pathao_order_status', 'updated_at'])
            updated_fields.append('order_status')
        if 'delivery_fee' in shipment_info:
            shipment_record.delivery_fee = shipment_info['delivery_fee']
            updated_fields.append('delivery_fee')
        shipment_record.save(update_fields=updated_fields)

    return redirect('staff_pathao_order', order_id=order_id)


@staff_member_required(login_url='staff_login')
def staff_pathao_order(request, order_id):
    order = get_object_or_404(
        Order.objects.prefetch_related('items'),
        pk=order_id,
    )
    order_items = list(order.items.all())
    successful_orders = Order.objects.filter(
        pathao_consignment_id__gt='',
    ).order_by('-updated_at')
    site_settings = SiteSettings.objects.first() or SiteSettings()
    site_settings.pathao_environment = 'production'
    amount_to_collect = order.total
    item_description = '; '.join(
        f'{item.product_name} x{item.quantity} (৳{item.line_total:.2f})'
        for item in order_items
    )[:500]
    special_instruction = (order.fulfillment_note or order.note)[:500]
    return render(request, 'staff_pathao_order.html', {
        'order': order,
        'pathao_shipment': PathaoShipment.objects.filter(order=order).first(),
        'order_items': order_items,
        'item_quantity': sum(item.quantity for item in order_items),
        'amount_to_collect': amount_to_collect,
        'item_description': item_description,
        'special_instruction': special_instruction,
        'merchant_order_id': str(order.display_order_number),
        'pathao_store_id': getattr(
            site_settings,
            f'pathao_{site_settings.pathao_environment}_store_id',
            None,
        ),
        'pathao_item_weight': site_settings.pathao_item_weight,
        'successful_orders': successful_orders,
        'pathao_configured': is_pathao_configured(site_settings),
    })


@staff_member_required(login_url='staff_login')
def staff_call_log(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    return_to_detail = request.POST.get('return_to') == 'detail'
    detail_page = request.POST.get('page', '1')
    detail_page = detail_page if detail_page.isdigit() else '1'
    detail_url = (
        f"{reverse('staff_order_detail', args=[order.pk])}?page={detail_page}"
    )
    if request.method == 'POST':
        form = StaffCallLogForm(request.POST)
        if form.is_valid():
            call_log = form.save(commit=False)
            call_log.order = order
            call_log.staff_member = request.user
            call_log.save()
            messages.success(request, 'Call note added to the order history.')
            if return_to_detail:
                return redirect(detail_url)
            return redirect('staff_call_log', order_id=order.pk)
        if return_to_detail:
            messages.error(request, 'Enter a call note before saving it.')
            return redirect(detail_url)
    else:
        form = StaffCallLogForm()

    call_logs = Paginator(
        order.call_logs.select_related('staff_member').all(),
        20,
    ).get_page(request.GET.get('page'))
    return render(
        request,
        'call_log.html',
        {
            'order': order,
            'form': form,
            'call_logs': call_logs,
        },
    )


@staff_member_required(login_url='staff_login')
def download_order_invoice(request, order_id):
    order = get_object_or_404(
        Order.objects.prefetch_related('items'),
        pk=order_id,
    )
    site_settings = SiteSettings.objects.first() or SiteSettings()
    pdf = build_order_invoice(order, site_settings)
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="invoice-order-{order.display_order_number}.pdf"'
    )
    return response
