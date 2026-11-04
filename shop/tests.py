import json
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
import ssl
from tempfile import TemporaryDirectory
from unittest.mock import patch
import urllib.error
from urllib.parse import parse_qs, urlsplit

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
from PIL import Image
from reportlab.lib.styles import getSampleStyleSheet

from .admin import ProductVariantInline, SiteSettingsAdminForm
from .forms import CheckoutForm
from .invoices import _paragraph, _paragraph_markup
from .inventory import sync_product_stock
from .models import (
    Category,
    Color,
    Order,
    OrderItem,
    PathaoShipment,
    PathaoStatusSyncRun,
    Product,
    ProductHighlight,
    ProductImage,
    ProductVariant,
    Size,
    SiteSettings,
    Stock,
    StaffCallLog,
)
from .pathao import (
    PathaoApiError,
    _base_url as get_pathao_base_url,
    _get_token as get_pathao_token,
    _request as pathao_request,
    create_order as create_pathao_order,
    get_order_info as get_pathao_order_info,
    is_pathao_configured,
)
from .views import calculate_shipping


def create_test_product(category, name, slug, price, **fields):
    product = Product.objects.create(
        category=category,
        name=name,
        slug=slug,
        **fields,
    )
    ProductVariant.objects.create(product=product, price=price)
    return product


def configure_test_product_variants(product, variants):
    base_variant = product.starting_variant
    product.variants.all().delete()
    for size, color, quantity in variants:
        if size:
            Size.objects.get_or_create(name=size)
        if color:
            Color.objects.get_or_create(name=color)
        ProductVariant.objects.create(
            product=product,
            size=size,
            color=color,
            price=base_variant.price,
            compare_at_price=base_variant.compare_at_price,
            quantity=quantity,
            opening_quantity=quantity,
        )
    sync_product_stock(product.pk)


class FacebookPurchaseEventClientSideTests(TestCase):
    def test_order_success_page_does_not_track_pending_order_as_purchase(self):
        order = Order.objects.create(
            full_name='Sample Buyer',
            phone='01700000000',
            address='Dhaka',
            delivery_area='inside_dhaka',
            payment_method='cod',
            total=Decimal('2560.00'),
            status='pending',
        )
        SiteSettings.objects.create(facebook_pixel_id='TEST_PIXEL')

        response = self.client.get(reverse('order_success', args=[order.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Order placed successfully!')
        self.assertNotContains(response, "fbq('track', 'Purchase'")


class InvoiceParagraphTests(TestCase):
    def test_invoice_markup_renders_formatting_and_plain_text_is_escaped(self):
        style = getSampleStyleSheet()['BodyText']

        markup_paragraph = _paragraph_markup('<b>Order</b><br/>#5', style)
        plain_paragraph = _paragraph('<b>Customer</b>', style)

        self.assertEqual(markup_paragraph.getPlainText(), 'Order#5')
        self.assertEqual(plain_paragraph.getPlainText(), '<b>Customer</b>')


class ProductGalleryAndShippingTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='Audio', slug='audio')
        self.product = create_test_product(
            self.category,
            'SoundBar Pro',
            'soundbar-pro',
            Decimal('2500.00'),
        )
        self.product.stock.quantity = 7
        self.product.stock.opening_quantity = 7
        self.product.stock.save(update_fields=['quantity', 'opening_quantity'])
        self.product.variants.update(quantity=7, opening_quantity=7)

    def test_checkout_phone_requires_exactly_eleven_ascii_digits(self):
        valid_form = CheckoutForm(data={
            'full_name': 'Test Buyer',
            'phone': '01700000000',
            'address': 'Dhaka',
            'delivery_area': 'inside_dhaka',
            'payment_method': 'cod',
        })
        self.assertTrue(valid_form.is_valid(), valid_form.errors)

        for phone in ('0170000000', '017000000000', '01700000ab1', '০১৭০০০০০০০০'):
            with self.subTest(phone=phone):
                form = CheckoutForm(data={
                    'full_name': 'Test Buyer',
                    'phone': phone,
                    'address': 'Dhaka',
                    'delivery_area': 'inside_dhaka',
                    'payment_method': 'cod',
                })
                self.assertFalse(form.is_valid())
                self.assertIn('phone', form.errors)

    def test_new_order_numbers_start_from_site_setting_and_increment(self):
        settings = SiteSettings.objects.create(next_order_number=1001)
        order_data = {
            'full_name': 'Numbered Buyer',
            'phone': '01700000000',
            'address': 'Dhaka',
        }

        first_order = Order.objects.create(**order_data)
        second_order = Order.objects.create(**order_data)

        settings.refresh_from_db()
        self.assertEqual(first_order.order_number, 1001)
        self.assertEqual(second_order.order_number, 1002)
        self.assertEqual(settings.next_order_number, 1003)
        self.assertEqual(first_order.display_order_number, 1001)

    def test_product_detail_renders_gallery_images(self):
        ProductImage.objects.create(product=self.product, image='products/gallery/shot-1.jpg', is_primary=True)
        ProductImage.objects.create(product=self.product, image='products/gallery/shot-2.jpg', ordering=1)

        response = self.client.get(reverse('product_detail', args=[self.product.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.product.name)
        self.assertContains(response, '/media/products/gallery/shot-1.jpg')
        self.assertContains(response, '/media/products/gallery/shot-2.jpg')
        self.assertContains(response, 'data-product-slider')
        self.assertContains(response, 'gallery-next')
        self.assertContains(response, 'data-product-thumbnail="1"')

    def test_product_detail_uses_default_placeholder_without_images(self):
        response = self.client.get(reverse('product_detail', args=[self.product.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'images/product-placeholder.svg')
        self.assertContains(response, f'alt="No image available for {self.product.name}"')
        self.assertContains(response, 'form="product-buy-form"')

    def test_checkout_order_summary_shows_product_thumbnail(self):
        ProductImage.objects.create(
            product=self.product,
            image='products/checkout/summary.jpg',
            is_primary=True,
        )
        session = self.client.session
        session['cart'] = {str(self.product.pk): 1}
        session.save()

        response = self.client.get(reverse('checkout'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="checkout-summary-thumbnail"')
        self.assertContains(response, '/media/products/checkout/summary.jpg')
        self.assertContains(response, f'alt="{self.product.name}"')

    def test_order_number_setting_is_editable_in_site_settings_admin(self):
        site_settings = SiteSettings.objects.create()
        staff = get_user_model().objects.create_superuser(
            username='order-number-admin',
            email='order-number-admin@example.com',
            password='secure-test-password',
        )
        self.client.force_login(staff)

        response = self.client.get(
            reverse('admin:shop_sitesettings_change', args=[site_settings.pk]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id_next_order_number')
        self.assertContains(response, '1001')

    def test_mobile_sticky_checkout_action_is_available_on_homepage(self):
        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="mobile-order-action"')
        self.assertContains(response, 'Go to checkout')
        self.assertContains(response, 'href="/checkout/"')

    def test_mobile_sticky_checkout_action_submits_checkout_form(self):
        session = self.client.session
        session['cart'] = {str(self.product.pk): 1}
        session.save()

        response = self.client.get(reverse('checkout'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="checkout-form"')
        self.assertContains(response, 'form="checkout-form"')
        self.assertContains(response, 'Place order')

    def test_product_detail_shows_up_to_six_related_products(self):
        for index in range(7):
            create_test_product(
                self.category,
                f'Related item {index}',
                f'related-item-{index}',
                Decimal('1200.00'),
            )

        response = self.client.get(reverse('product_detail', args=[self.product.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['related_products']), 6)

    def test_product_detail_fills_related_row_from_other_categories(self):
        for index in range(2):
            create_test_product(
                self.category,
                f'Same category item {index}',
                f'same-category-item-{index}',
                Decimal('1200.00'),
            )
        other_category = Category.objects.create(name='Home', slug='related-home')
        for index in range(4):
            create_test_product(
                other_category,
                f'Other category item {index}',
                f'other-category-item-{index}',
                Decimal('900.00'),
            )

        response = self.client.get(reverse('product_detail', args=[self.product.slug]))

        self.assertEqual(response.status_code, 200)
        related_products = response.context['related_products']
        self.assertEqual(len(related_products), 6)
        self.assertEqual(
            sum(item.category_id == self.category.pk for item in related_products),
            2,
        )

    def test_whatsapp_float_uses_site_settings_number(self):
        SiteSettings.objects.create(whatsapp_number='8801712345678')

        response = self.client.get(reverse('product_detail', args=[self.product.slug]))

        self.assertContains(response, 'class="whatsapp-float"')
        self.assertContains(response, 'href="https://wa.me/8801712345678"')
        self.assertContains(response, 'aria-label="Chat with us on WhatsApp"')

    def test_whatsapp_float_is_hidden_when_number_is_not_configured(self):
        response = self.client.get(reverse('product_detail', args=[self.product.slug]))

        self.assertNotContains(response, 'class="whatsapp-float"')

    def test_product_detail_shows_highlights_below_order_button_in_order(self):
        ProductHighlight.objects.create(
            product=self.product,
            icon='truck',
            text='Free delivery',
            ordering=2,
        )
        ProductHighlight.objects.create(
            product=self.product,
            icon='check-mark',
            text='Quality tested',
            ordering=1,
        )

        response = self.client.get(reverse('product_detail', args=[self.product.slug]))
        content = response.content.decode()

        self.assertContains(response, 'class="product-highlights"')
        self.assertContains(response, '<svg viewBox="0 0 24 24" focusable="false">', html=False)
        self.assertContains(response, 'product-highlights-card')
        self.assertContains(response, 'id="product-highlights-title" class="product-highlights-title"')
        self.assertContains(response, 'Free delivery')
        self.assertContains(response, 'Quality tested')
        self.assertLess(content.index('>Quality tested</span>'), content.index('>Free delivery</span>'))
        self.assertLess(content.index('class="detail-actions"'), content.index('class="product-highlights"'))

    def test_customer_selects_product_size_and_color_before_ordering(self):
        size = Size.objects.create(name='Small')
        color = Color.objects.create(name='Green')
        configure_test_product_variants(
            self.product,
            [(size.name, color.name, 7)],
        )

        detail_response = self.client.get(reverse('product_detail', args=[self.product.slug]))

        self.assertContains(detail_response, 'name="size_id"')
        self.assertContains(detail_response, 'name="color_id"')
        self.assertContains(
            detail_response,
            '<option value="%s" selected>Small</option>' % size.pk,
            html=True,
        )
        self.assertContains(
            detail_response,
            '<option value="%s" selected>Green</option>' % color.pk,
            html=True,
        )

        buy_response = self.client.post(
            reverse('buy_now', args=[self.product.slug]),
            {'size_id': size.pk, 'color_id': color.pk},
        )
        self.assertRedirects(buy_response, reverse('checkout'))

        checkout_response = self.client.get(reverse('checkout'))
        self.assertContains(checkout_response, 'Size: Small')
        self.assertContains(checkout_response, 'Color: Green')
        self.assertContains(checkout_response, 'name="cart_key"')
        self.assertContains(checkout_response, 'Email (optional)')
        self.assertNotContains(checkout_response, 'id="id_email" required')

        order_response = self.client.post(reverse('checkout'), {
            'full_name': 'Variant Buyer',
            'phone': '01700000000',
            'address': 'Dhaka',
            'delivery_area': 'inside_dhaka',
            'payment_method': 'cod',
        })
        order = Order.objects.get(full_name='Variant Buyer')
        order_item = order.items.get()
        self.assertRedirects(
            order_response,
            reverse('order_success', args=[order.pk]),
        )
        self.assertEqual(order_item.selected_size, 'Small')
        self.assertEqual(order_item.selected_color, 'Green')
        self.assertIsNone(order.email)

    def test_selected_size_and_color_use_their_variant_price_at_checkout(self):
        size = Size.objects.create(name='Large')
        color = Color.objects.create(name='Red')
        configure_test_product_variants(
            self.product,
            [(size.name, color.name, 7)],
        )
        variant = ProductVariant.objects.get(
            product=self.product,
            size='Large',
            color='Red',
        )
        variant.price = Decimal('3200.00')
        variant.compare_at_price = Decimal('3500.00')
        variant.save(update_fields=['price', 'compare_at_price'])

        detail_response = self.client.get(reverse('product_detail', args=[self.product.slug]))
        self.assertContains(detail_response, '"price": "3200.00"')
        self.assertContains(detail_response, '"compare_at_price": "3500.00"')

        self.client.post(
            reverse('buy_now', args=[self.product.slug]),
            {'size_id': size.pk, 'color_id': color.pk},
        )
        checkout_response = self.client.get(reverse('checkout'))
        self.assertContains(checkout_response, '1 x ৳3200.00')

        order_response = self.client.post(reverse('checkout'), {
            'full_name': 'Variant Price Buyer',
            'phone': '01700000000',
            'address': 'Dhaka',
            'delivery_area': 'inside_dhaka',
            'payment_method': 'cod',
        })

        order = Order.objects.get(full_name='Variant Price Buyer')
        item = order.items.get()
        self.assertRedirects(order_response, reverse('order_success', args=[order.pk]))
        self.assertEqual(item.unit_price, Decimal('3200.00'))
        self.assertEqual(item.line_total, Decimal('3200.00'))
        self.assertEqual(item.variant, variant)

    def test_single_configured_size_is_selected_and_applied_by_default(self):
        only_size = Size.objects.create(name='One Size')
        configure_test_product_variants(
            self.product,
            [(only_size.name, '', 7)],
        )

        detail_response = self.client.get(reverse('product_detail', args=[self.product.slug]))

        self.assertContains(
            detail_response,
            f'<option value="{only_size.pk}" selected>One Size</option>',
            html=True,
        )
        self.assertContains(detail_response, 'class="product-variant-options"')

        buy_response = self.client.post(reverse('buy_now', args=[self.product.slug]))

        self.assertRedirects(buy_response, reverse('checkout'))
        self.assertContains(self.client.get(reverse('checkout')), 'Size: One Size')

    def test_customer_must_choose_configured_product_variants(self):
        configure_test_product_variants(
            self.product,
            [('Large', '', 0), ('Small', '', 0)],
        )

        response = self.client.post(
            reverse('buy_now', args=[self.product.slug]),
            follow=True,
        )

        self.assertEqual(
            response.redirect_chain[0][0],
            reverse('product_detail', args=[self.product.slug]),
        )
        self.assertContains(response, 'Choose an available size.')

    def test_product_description_renders_allowed_rich_text_without_scripts(self):
        self.product.short_description = 'Compact speaker for a full sound.'
        self.product.long_description = (
            '<p style="text-align: center; color: rgb(255, 0, 0)">Feature <strong>bold</strong></p>'
            '<figure class="image"><img src="/media/editor/product.png" alt="Product"></figure>'
            '<figure class="media"><div><iframe src="https://www.youtube.com/embed/demo" title="Video"></iframe></div></figure>'
            '<iframe src="https://evil.example/embed/demo" title="Unsafe"></iframe>'
            '<script>alert(1)</script>'
            '<a href="javascript:alert(1)">link</a>'
            '<img src="x" onerror="alert(1)">'
        )
        self.product.save(update_fields=['short_description', 'long_description'])

        response = self.client.get(reverse('product_detail', args=[self.product.slug]))
        content = response.content.decode()

        self.assertContains(response, '<p style="text-align: center; color: rgb(255, 0, 0);">Feature <strong>bold</strong></p>', html=True)
        self.assertContains(response, '<figure class="image"><img src="/media/editor/product.png" alt="Product"></figure>', html=True)
        self.assertContains(response, 'src="https://www.youtube.com/embed/demo"')
        self.assertNotContains(response, 'src="https://evil.example/embed/demo"')
        self.assertContains(response, self.product.short_description)
        content = response.content.decode()
        description_position = content.index('id="product-description-heading"')
        description_order_position = content.index(
            '<button type="submit" form="product-buy-form" class="btn btn-primary">Order now</button>'
        )
        related_position = content.index('Related products')
        self.assertLess(description_position, description_order_position)
        self.assertLess(description_order_position, related_position)
        self.assertContains(response, 'class="mobile-order-action"')
        self.assertContains(response, 'id="product-buy-form"')
        self.assertLess(description_position, related_position)
        self.assertNotContains(response, '<script>alert(1)</script>', html=True)
        self.assertNotIn('href="javascript:', content)
        self.assertNotIn('onerror=', content)

    def test_product_admin_uses_ckeditor_for_description(self):
        staff = get_user_model().objects.create_superuser(
            username='product-admin',
            email='admin@example.com',
            password='secure-test-password',
        )
        self.client.force_login(staff)

        response = self.client.get(reverse('admin:shop_product_add'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="long_description"')
        self.assertContains(response, 'django_ckeditor_5/dist/styles.css')
        self.assertContains(response, 'django_ckeditor_5/dist/bundle.js')
        self.assertContains(response, 'ckeditor5/image_upload/')
        self.assertContains(response, 'findAndReplace')
        self.assertContains(response, 'fontColor')
        self.assertContains(response, 'imageUpload')
        self.assertContains(response, 'mediaEmbed')
        self.assertContains(response, 'sourceEditing')
        self.assertContains(response, 'tableProperties')
        self.assertContains(response, 'strikethrough')
        self.assertContains(response, 'horizontalLine')
        self.assertContains(response, 'removeFormat')
        self.assertContains(response, 'images-0-image')
        for field in (
            'variants-0-size',
            'variants-0-color',
            'variants-0-price',
            'variants-0-compare_at_price',
            'variants-0-opening_quantity',
        ):
            self.assertContains(response, f'name="{field}"')
        self.assertContains(response, '<select name="variants-0-size"')
        self.assertContains(response, '<select name="variants-0-color"')
        self.assertContains(response, '1200 × 1200 px')
        self.assertContains(response, 'under 100 KB')
        self.assertContains(response, 'WebP')
        self.assertContains(response, 'contained in square frames')
        self.assertContains(response, 'Product images')
        self.assertContains(response, 'name="images-0-caption"')
        self.assertContains(response, 'name="images-0-is_primary"')
        self.assertContains(response, 'name="images-0-ordering"')
        self.assertContains(response, 'Product highlights')
        self.assertContains(response, 'name="highlights-0-icon"')
        self.assertContains(response, 'Money / payment')
        self.assertContains(response, 'Check mark')
        self.assertContains(response, 'Customer support')
        self.assertContains(response, 'Eco-friendly')
        self.assertContains(response, 'name="highlights-0-text"')
        self.assertContains(response, 'name="highlights-0-ordering"')
        admin_index = self.client.get(reverse('admin:index'))
        self.assertNotContains(admin_index, 'Product images')
        self.assertContains(admin_index, 'Sizes')
        self.assertContains(admin_index, 'Colors')

    def test_product_variant_admin_dropdowns_use_selected_product_options(self):
        staff = get_user_model().objects.create_superuser(
            username='variant-admin',
            email='variant-admin@example.com',
            password='secure-test-password',
        )
        self.client.force_login(staff)
        size = Size.objects.create(name='Large')
        unused_size = Size.objects.create(name='Unused size')
        color = Color.objects.create(name='Navy')
        unused_color = Color.objects.create(name='Unused color')
        configure_test_product_variants(
            self.product,
            [(size.name, color.name, 7)],
        )

        response = self.client.get(
            reverse('admin:shop_product_change', args=[self.product.pk]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="Large">Large</option>', html=True)
        self.assertContains(response, '<option value="Navy">Navy</option>', html=True)
        self.assertContains(
            response,
            f'<option value="{unused_size.name}">{unused_size.name}</option>',
            html=True,
        )
        self.assertContains(
            response,
            f'<option value="{unused_color.name}">{unused_color.name}</option>',
            html=True,
        )

    def test_product_variant_admin_dropdowns_follow_selected_options_during_add(self):
        size = Size.objects.create(name='Large')
        unused_size = Size.objects.create(name='Unused size')
        color = Color.objects.create(name='Navy')
        unused_color = Color.objects.create(name='Unused color')
        request = RequestFactory().post(
            '/admin/shop/product/add/',
            data={
                'sizes': [str(size.pk)],
                'colors': [str(color.pk)],
            },
        )
        request.user = get_user_model().objects.create_superuser(
            username='variant-add-admin',
            email='variant-add-admin@example.com',
            password='secure-test-password',
        )
        inline = ProductVariantInline(Product, admin.site)

        self.client.force_login(request.user)
        add_response = self.client.get(reverse('admin:shop_product_add'))
        self.assertContains(add_response, '<option value="Large">Large</option>', html=True)
        self.assertContains(add_response, '<option value="Navy">Navy</option>', html=True)

        formset_class = inline.get_formset(request)
        form = formset_class(instance=Product()).forms[0]

        self.assertIn(('Large', 'Large'), form.fields['size'].choices)
        self.assertIn(('Navy', 'Navy'), form.fields['color'].choices)
        self.assertIn(
            (unused_size.name, unused_size.name),
            form.fields['size'].choices,
        )
        self.assertIn(
            (unused_color.name, unused_color.name),
            form.fields['color'].choices,
        )

    def test_product_variant_admin_accepts_selected_size_and_color_values(self):
        size = Size.objects.create(name='Large')
        color = Color.objects.create(name='Navy')
        request = RequestFactory().post(
            '/admin/shop/product/add/',
            data={
                'sizes': [str(size.pk)],
                'colors': [str(color.pk)],
            },
        )
        request.user = get_user_model().objects.create_superuser(
            username='variant-form-admin',
            email='variant-form-admin@example.com',
            password='secure-test-password',
        )
        inline = ProductVariantInline(Product, admin.site)
        formset_class = inline.get_formset(request)
        formset = formset_class(
            data={
                'variants-TOTAL_FORMS': '1',
                'variants-INITIAL_FORMS': '0',
                'variants-MIN_NUM_FORMS': '0',
                'variants-MAX_NUM_FORMS': '1000',
                'variants-0-size': size.name,
                'variants-0-color': color.name,
                'variants-0-price': '125.00',
                'variants-0-compare_at_price': '',
                'variants-0-opening_quantity': '4',
            },
            instance=Product(),
        )

        self.assertTrue(formset.is_valid(), formset.errors)

    def test_banner_admin_shows_image_size_guidance(self):
        staff = get_user_model().objects.create_superuser(
            username='banner-admin',
            email='banner-admin@example.com',
            password='secure-test-password',
        )
        self.client.force_login(staff)

        response = self.client.get(reverse('admin:shop_banner_add'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '1920 × 600 px')
        self.assertContains(response, 'under 100 KB')
        self.assertContains(response, 'WebP')

    def test_categories_page_lists_categories_linking_to_filtered_shop(self):
        response = self.client.get(reverse('categories'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.category.name)
        self.assertContains(response, f'{reverse("shop")}?category={self.category.slug}')

    def test_shipping_is_free_for_product_marked_free_shipping(self):
        SiteSettings.objects.create(
            brand_name='Test Store',
            inside_dhaka_charge=Decimal('70.00'),
            outside_dhaka_charge=Decimal('140.00'),
        )

        self.product.free_shipping = True
        self.product.save(update_fields=['free_shipping'])
        items = [{
            'product': self.product,
            'quantity': 1,
            'line_total': Decimal('600.00'),
        }]

        self.assertEqual(calculate_shipping(items, 'inside_dhaka'), Decimal('0.00'))

    def test_shipping_is_charged_when_product_is_not_marked_free_shipping(self):
        SiteSettings.objects.create(
            brand_name='Test Store',
            inside_dhaka_charge=Decimal('70.00'),
            outside_dhaka_charge=Decimal('140.00'),
        )
        items = [{
            'product': self.product,
            'quantity': 1,
            'line_total': Decimal('2500.00'),
        }]

        self.assertEqual(calculate_shipping(items, 'inside_dhaka'), Decimal('70.00'))
        self.assertEqual(calculate_shipping(items, 'outside_dhaka'), Decimal('140.00'))

    def test_shipping_rates_can_be_updated_in_site_settings_and_used_at_checkout(self):
        SiteSettings.objects.create(
            brand_name='Test Store',
            inside_dhaka_charge=Decimal('85.00'),
            outside_dhaka_charge=Decimal('165.00'),
        )

        self.assertEqual(calculate_shipping([{
            'product': self.product,
            'quantity': 1,
            'line_total': Decimal('2500.00'),
        }], 'inside_dhaka'), Decimal('85.00'))
        self.assertEqual(calculate_shipping([{
            'product': self.product,
            'quantity': 1,
            'line_total': Decimal('2500.00'),
        }], 'outside_dhaka'), Decimal('165.00'))

    def test_site_settings_admin_exposes_shipping_fields_without_mobile_payment_settings(self):
        staff = get_user_model().objects.create_superuser(
            username='settings-admin',
            email='settings@example.com',
            password='secure-test-password',
        )
        self.client.force_login(staff)

        response = self.client.get(reverse('admin:shop_sitesettings_add'))

        self.assertEqual(response.status_code, 200)
        for field_name in (
            'inside_dhaka_charge',
            'outside_dhaka_charge',
            'whatsapp_number',
        ):
            self.assertContains(response, f'id_{field_name}')
        self.assertNotContains(response, 'bkash_payment_number')
        self.assertNotContains(response, 'nagad_payment_number')
        self.assertContains(response, '8801712345678')
        self.assertNotContains(response, 'free_shipping_minimum_order')

    def test_whatsapp_number_requires_international_digits_without_punctuation(self):
        settings = SiteSettings(whatsapp_number='+880 1712-345678')

        with self.assertRaisesMessage(ValidationError, 'Enter 8–15 digits'):
            settings.full_clean()


class ShopCategoryFilterTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='Audio', slug='audio')
        self.product = create_test_product(
            self.category,
            'SoundBar Pro',
            'soundbar-pro',
            Decimal('2500.00'),
        )

    def test_category_filter_is_a_dropdown_and_preserves_selection(self):
        response = self.client.get(reverse('shop'), {'category': self.category.slug})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<select id="category" name="category">')
        self.assertContains(
            response,
            f'<option value="{self.category.slug}" selected>{self.category.name}</option>',
            html=True,
        )
        self.assertContains(response, self.product.name)

    def test_discount_badge_uses_compare_at_price_on_product_images(self):
        variant = self.product.variants.first()
        variant.compare_at_price = Decimal('3500.00')
        variant.save(update_fields=['compare_at_price'])

        self.assertEqual(self.product.discount_percentage, 29)
        for page in ('shop', 'home', 'product_detail'):
            with self.subTest(page=page):
                url = (
                    reverse('product_detail', args=[self.product.slug])
                    if page == 'product_detail'
                    else reverse(page)
                )
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, '29% OFF')

    def test_discount_badge_requires_compare_at_price_above_actual_price(self):
        variant = self.product.variants.first()
        for compare_at_price in (None, Decimal('2500.00'), Decimal('2400.00')):
            with self.subTest(compare_at_price=compare_at_price):
                variant.compare_at_price = compare_at_price
                variant.save(update_fields=['compare_at_price'])
                self.assertIsNone(self.product.discount_percentage)

    def test_shop_size_and_color_filters_use_configured_variants(self):
        ProductVariant.objects.create(
            product=self.product,
            size='M',
            color='Green',
            price=Decimal('2500.00'),
        )
        ProductVariant.objects.create(
            product=self.product,
            size='L',
            color='Navy Blue',
            price=Decimal('2600.00'),
        )
        matching_product = create_test_product(
            self.category,
            'Navy Blue Headphones',
            'navy-blue-headphones',
            Decimal('1800.00'),
        )
        ProductVariant.objects.create(
            product=matching_product,
            size='M',
            color='Navy Blue',
            price=Decimal('1800.00'),
        )

        response = self.client.get(
            reverse('shop'),
            {'size': 'M', 'color': 'Navy Blue'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<option value="M" selected>M</option>',
            html=True,
        )
        self.assertContains(
            response,
            '<option value="Navy Blue" selected>Navy Blue</option>',
            html=True,
        )
        self.assertContains(response, matching_product.name)
        self.assertNotContains(response, self.product.name)
        self.assertIn('M', response.context['available_sizes'])
        self.assertIn('Navy Blue', response.context['available_colors'])
        self.assertFalse(Size.objects.filter(name='M').exists())
        self.assertFalse(Color.objects.filter(name='Navy Blue').exists())

    def test_shared_navigation_and_footer_use_accessible_working_links(self):
        response = self.client.get(reverse('home'))

        self.assertContains(response, 'class="skip-to-content" href="#main-content"')
        self.assertContains(response, f'href="{reverse("categories")}">Categories</a>')
        self.assertContains(response, f'href="{reverse("checkout")}">Checkout</a>')
        self.assertContains(response, 'href="tel:')
        self.assertContains(response, 'href="mailto:support@bdbazaar.com"')
        self.assertNotContains(response, 'href="#"')

    def test_home_and_categories_show_active_item_counts(self):
        create_test_product(
            self.category,
            'Audio product 2',
            'audio-product-2',
            Decimal('500.00'),
        )
        create_test_product(
            self.category,
            'Inactive audio product',
            'inactive-audio-product',
            Decimal('500.00'),
            is_active=False,
        )

        for page in ('home', 'categories'):
            with self.subTest(page=page):
                response = self.client.get(reverse(page))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, '<small>2 items</small>')

        self.product.is_active = False
        self.product.save(update_fields=['is_active'])
        response = self.client.get(reverse('home'))
        self.assertContains(response, '<small>1 item</small>')

    def test_shop_pagination_works_without_external_htmx_script(self):
        for index in range(13):
            create_test_product(
                self.category,
                f'Audio product {index}',
                f'audio-product-{index}',
                Decimal('500.00'),
            )

        response = self.client.get(reverse('shop'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-shop-page')
        self.assertNotContains(response, 'unpkg.com/htmx')
        self.assertNotContains(response, 'fonts.googleapis.com')

        partial_response = self.client.get(
            reverse('shop'),
            {'page': 2},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(partial_response.status_code, 200)
        self.assertContains(partial_response, 'data-shop-page')
        self.assertContains(partial_response, 'Audio product 0')


    def test_shop_shows_order_now_when_variant_choices_are_unambiguous(self):
        variant = self.product.variants.first()
        variant.size = 'One Size'
        variant.color = 'Black'
        variant.save(update_fields=['size', 'color'])
        self.product.variants.update(quantity=5)
        sync_product_stock(self.product.pk)

        response = self.client.get(reverse('shop'))

        self.assertContains(response, '<span class="product-attribute-label">Size</span>', html=True)
        self.assertContains(response, '<span>One Size</span>', html=True)
        self.assertContains(response, '<span class="product-attribute-label">Color</span>', html=True)
        self.assertContains(response, '<span>Black</span>', html=True)
        self.assertContains(response, 'Order now')
        self.assertNotContains(response, 'Choose option')
        order_response = self.client.post(reverse('buy_now', args=[self.product.slug]))
        self.assertRedirects(order_response, reverse('checkout'))
        self.assertContains(self.client.get(reverse('checkout')), 'Size: One Size')
        self.assertContains(self.client.get(reverse('checkout')), 'Color: Black')

    def test_shop_shows_choose_option_when_product_has_multiple_choices(self):
        self.product.variants.all().delete()
        ProductVariant.objects.create(
            product=self.product,
            size='Small',
            color='Black',
            price=Decimal('2400.00'),
        )
        ProductVariant.objects.create(
            product=self.product,
            size='Large',
            color='Black',
            price=Decimal('2500.00'),
        )

        response = self.client.get(reverse('shop'))

        self.assertContains(response, 'Choose option')
        self.assertNotContains(response, 'Order now')
        self.assertContains(response, '<span>Small</span>', html=True)
        self.assertContains(response, '<span>Large</span>', html=True)

    def test_shop_product_card_shows_available_size_and_color_labels(self):
        self.product.variants.all().delete()
        ProductVariant.objects.create(
            product=self.product,
            size='M',
            color='Navy Blue',
            price=Decimal('2400.00'),
        )
        ProductVariant.objects.create(
            product=self.product,
            size='L',
            color='Navy Blue',
            price=Decimal('2600.00'),
        )

        response = self.client.get(reverse('shop'))

        self.assertContains(response, '<span class="product-attribute-label">Size</span>', html=True)
        self.assertContains(response, '<span>M</span>', html=True)
        self.assertContains(response, '<span>L</span>', html=True)
        self.assertContains(response, '<span class="product-attribute-label">Color</span>', html=True)
        self.assertContains(response, '<span>Navy Blue</span>', html=True)

    def test_shop_product_card_shows_free_shipping_badge_on_image(self):
        self.product.free_shipping = True
        self.product.save(update_fields=['free_shipping'])

        response = self.client.get(reverse('shop'))

        self.assertContains(
            response,
            '<span class="product-image-badge">Free shipping</span>',
            html=True,
        )
        self.assertContains(response, '<div class="product-card-image">', html=False)

    def test_shop_search_matches_short_and_long_descriptions(self):
        self.product.short_description = 'Compact speaker for a small room.'
        self.product.long_description = '<p>Bluetooth pairing and deep bass response.</p>'
        self.product.save(update_fields=['short_description', 'long_description'])

        short_response = self.client.get(reverse('shop'), {'search': 'Compact speaker'})
        long_response = self.client.get(reverse('shop'), {'search': 'deep bass'})

        self.assertContains(short_response, self.product.name)
        self.assertContains(long_response, self.product.name)

    def test_search_suggestions_return_matching_active_products(self):
        inactive = create_test_product(
            self.category,
            'SoundBar Pro inactive',
            'soundbar-pro-inactive',
            Decimal('1800.00'),
            is_active=False,
        )

        response = self.client.get(reverse('search_suggestions'), {'search': 'soundbar'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'], [{
            'name': self.product.name,
            'category': self.category.name,
            'price': '2500.00',
            'url': reverse('product_detail', args=[self.product.slug]),
            'image': '',
        }])
        self.assertNotIn(inactive.name, str(response.json()))

    def test_search_suggestions_wait_for_two_characters(self):
        response = self.client.get(reverse('search_suggestions'), {'search': 's'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'results': []})


class DemoProductSeedTests(TestCase):
    def test_product_refresh_generates_square_images_and_long_descriptions(self):
        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                call_command('seed_dummy_data', '--no-clear', '--refresh-demo-products', verbosity=0)

                for product in Product.objects.all():
                    self.assertIn('<h2>', product.long_description)
                    self.assertIn('<ul>', product.long_description)
                    self.assertTrue(product.images.exists())
                    for product_image in product.images.all():
                        with Image.open(product_image.image.path) as image:
                            self.assertEqual(image.width, image.height)


class SampleOrderCommandTests(TestCase):
    def test_add_sample_orders_creates_varied_orders_without_changing_stock(self):
        category = Category.objects.create(name='Electronics', slug='sample-electronics')
        product = create_test_product(
            category,
            'Sample product',
            'sample-product',
            Decimal('100.00'),
        )
        product.stock.quantity = product.stock.opening_quantity = 12
        product.stock.save(update_fields=['quantity', 'opening_quantity'])
        product_two = create_test_product(
            category,
            'Sample product two',
            'sample-product-two',
            Decimal('150.00'),
        )
        product_two.stock.quantity = product_two.stock.opening_quantity = 8
        product_two.stock.save(update_fields=['quantity', 'opening_quantity'])

        call_command('add_sample_orders', '--count=14', verbosity=0)

        self.assertEqual(Order.objects.count(), 14)
        self.assertEqual(OrderItem.objects.count(), 28)
        self.assertEqual(
            set(Order.objects.values_list('payment_method', flat=True)),
            {'cod'},
        )
        self.assertEqual(
            set(Order.objects.values_list('status', flat=True)),
            {status for status, _ in Order.STATUS_CHOICES},
        )
        self.assertEqual(Order.objects.filter(status='delivered', delivered_at__isnull=False).count(), 2)
        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 12)

    def test_add_sample_orders_can_create_one_unit_per_order(self):
        category = Category.objects.create(name='Electronics', slug='single-item-electronics')
        product = create_test_product(
            category,
            'Single item product',
            'single-item-product',
            Decimal('100.00'),
        )

        call_command('add_sample_orders', '--count=3', '--single-item', verbosity=0)

        self.assertEqual(Order.objects.count(), 3)
        self.assertEqual(OrderItem.objects.count(), 3)
        self.assertEqual(
            list(OrderItem.objects.values_list('product_id', 'quantity', 'unit_price', 'line_total')),
            [(product.pk, 1, Decimal('100.00'), Decimal('100.00'))] * 3,
        )


class OrderManagementTests(TestCase):
    def setUp(self):
        self.staff_user = get_user_model().objects.create_user(
            username='staff',
            password='secretpass',
            is_staff=True,
            is_superuser=True,
        )
        self.category = Category.objects.create(name='Mobiles', slug='mobiles')
        self.product = create_test_product(
            self.category,
            'Phone X',
            'phone-x',
            Decimal('1200.00'),
        )
        self.product.stock.quantity = self.product.stock.opening_quantity = 10
        self.product.stock.save(update_fields=['quantity', 'opening_quantity'])
        self.product.variants.update(quantity=10, opening_quantity=10)
        self.order = Order.objects.create(
            full_name='Test Buyer',
            phone='01700000000',
            email='buyer@example.com',
            address='Dhaka',
            city='Dhaka',
            subtotal=Decimal('1200.00'),
            shipping_charge=Decimal('60.00'),
            total=Decimal('1260.00'),
            status='pending',
        )

    def test_staff_can_remove_order_item_and_restore_stock_with_totals_recalculated(self):
        self.order.status = 'processing'
        self.order.save(update_fields=['status'])
        self.product.variants.update(quantity=8)
        sync_product_stock(self.product.pk)
        removed_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=2,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('2400.00'),
        )
        retained_product = create_test_product(
            self.category,
            'Phone Y',
            'phone-y',
            Decimal('800.00'),
        )
        retained_product.variants.update(quantity=4)
        sync_product_stock(retained_product.pk)
        retained_item = OrderItem.objects.create(
            order=self.order,
            product=retained_product,
            product_name=retained_product.name,
            quantity=1,
            unit_price=Decimal('800.00'),
            line_total=Decimal('800.00'),
        )
        self.client.force_login(self.staff_user)
        detail = self.client.get(reverse('staff_order_detail', args=[self.order.pk]))
        self.assertContains(detail, f'name="delete_item_{removed_item.pk}"')

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'return_to': 'detail',
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'address': self.order.address,
                'thana': self.order.thana,
                'zilla': self.order.zilla,
                'delivery_area': self.order.delivery_area,
                'status': 'processing',
                f'delete_item_{removed_item.pk}': 'true',
                f'item_quantity_{retained_item.pk}': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assertFalse(OrderItem.objects.filter(pk=removed_item.pk).exists())
        retained_item.refresh_from_db()
        self.order.refresh_from_db()
        self.product.refresh_from_db()
        retained_product.refresh_from_db()
        self.assertEqual(retained_item.quantity, 1)
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(retained_product.stock_quantity, 4)
        self.assertEqual(self.order.subtotal, Decimal('800.00'))
        self.assertEqual(self.order.shipping_charge, Decimal('60.00'))
        self.assertEqual(self.order.total, Decimal('860.00'))

    def test_staff_cannot_remove_the_last_order_item_without_adding_another(self):
        order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=1,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('1200.00'),
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'address': self.order.address,
                'thana': self.order.thana,
                'zilla': self.order.zilla,
                'delivery_area': self.order.delivery_area,
                'status': self.order.status,
                f'delete_item_{order_item.pk}': 'true',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])
        self.assertIn('at least one product', response.json()['error'])
        self.assertTrue(OrderItem.objects.filter(pk=order_item.pk).exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.total, Decimal('1260.00'))

    def test_setting_opening_stock_also_sets_current_quantity(self):
        stock = self.product.stock

        stock.opening_quantity = 15
        stock.save(update_fields=['opening_quantity'])

        stock.refresh_from_db()
        self.assertEqual(stock.opening_quantity, 15)
        self.assertEqual(stock.quantity, 15)

    def test_changing_opening_stock_does_not_overwrite_live_quantity(self):
        stock = self.product.stock
        stock.quantity = 7
        stock.save(update_fields=['quantity'])

        stock.opening_quantity = 12
        stock.save(update_fields=['opening_quantity'])

        stock.refresh_from_db()
        self.assertEqual(stock.opening_quantity, 12)
        self.assertEqual(stock.quantity, 7)

    def test_staff_inventory_updates_stock_without_changing_variant_prices(self):
        configure_test_product_variants(
            self.product,
            [('Large', 'Blue', 0), ('Small', 'Blue', 0)],
        )
        variants = list(
            ProductVariant.objects.filter(product=self.product, color='Blue').order_by('size')
        )
        self.client.force_login(self.staff_user)
        large = next(variant for variant in variants if variant.size == 'Large')
        small = next(variant for variant in variants if variant.size == 'Small')
        large_response = self.client.post(
            reverse('staff_update_product_variants', args=[self.product.pk]),
            {
                'variant_id': large.pk,
                'size': 'Large',
                'color': 'Blue',
                'price': '3000.00',
                'compare_at_price': '3500.00',
                'opening_quantity': '10',
            },
        )
        self.assertEqual(large_response.status_code, 302)
        small_response = self.client.post(
            reverse('staff_update_product_variants', args=[self.product.pk]),
            {
                'variant_id': small.pk,
                'size': 'Small',
                'color': 'Blue',
                'price': '1500.00',
                'opening_quantity': '0',
                'restock_quantity': '3',
            },
        )
        self.assertEqual(small_response.status_code, 302)
        self.product.refresh_from_db()
        large = ProductVariant.objects.get(product=self.product, size='Large', color='Blue')
        small = ProductVariant.objects.get(product=self.product, size='Small', color='Blue')
        self.assertEqual(large.price, Decimal('1200.00'))
        self.assertIsNone(large.compare_at_price)
        self.assertEqual(large.quantity, 10)
        self.assertEqual(small.price, Decimal('1200.00'))
        self.assertEqual(small.quantity, 3)
        self.assertEqual(small.restocked_quantity, 3)
        self.assertEqual(self.product.stock_quantity, 13)
        self.assertEqual(self.product.initial_stock_quantity, 10)

        size = Size.objects.get(name='Small')
        color = Color.objects.get(name='Blue')
        self.client.post(
            reverse('buy_now', args=[self.product.slug]),
            {'size_id': size.pk, 'color_id': color.pk},
        )
        order_response = self.client.post(reverse('checkout'), {
            'full_name': 'Variant Inventory Buyer',
            'phone': '01700000000',
            'address': 'Dhaka',
            'delivery_area': 'inside_dhaka',
            'payment_method': 'cod',
        })
        order = Order.objects.get(full_name='Variant Inventory Buyer')
        item = order.items.get()
        self.assertRedirects(order_response, reverse('order_success', args=[order.pk]))
        self.assertEqual(item.unit_price, small.price)
        self.assertEqual(item.variant, small)
        small.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(small.quantity, 2)
        self.assertEqual(self.product.stock_quantity, 12)

    def test_public_navigation_shows_login(self):
        response = self.client.get(reverse('home'))

        self.assertContains(
            response,
            f'href="{reverse("staff_login")}"',
        )
        self.assertContains(response, '>Login</a>', html=False)
        dashboard_response = self.client.get(reverse('staff_dashboard'))
        self.assertEqual(dashboard_response.status_code, 302)
        self.assertIn(reverse('staff_login'), dashboard_response.url)

    def test_staff_login_redirects_staff_to_staff_dashboard(self):
        operator = get_user_model().objects.create_user(
            username='order-operator',
            password='operator-password',
            is_staff=True,
        )
        login_url = reverse('staff_login')
        login_response = self.client.post(
            login_url,
            {
                'username': operator.username,
                'password': 'operator-password',
            },
        )

        self.assertRedirects(login_response, reverse('staff_dashboard'))
        dashboard_response = self.client.get(reverse('staff_dashboard'))
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertContains(dashboard_response, 'Staff dashboard')
        self.assertContains(dashboard_response, 'Order fulfillment')
        self.assertContains(dashboard_response, self.order.full_name)
        self.assertContains(dashboard_response, self.order.phone)
        self.assertContains(dashboard_response, 'Inventory &amp; restock')
        inventory_response = self.client.get(reverse('staff_inventory'))
        self.assertContains(inventory_response, self.product.name)
        self.assertContains(inventory_response, '10')
        self.assertNotContains(dashboard_response, 'Revenue')
        self.assertNotContains(dashboard_response, 'Best selling')
        self.assertNotContains(dashboard_response, 'Django admin')
        self.assertRedirects(
            self.client.get(reverse('admin_dashboard')),
            reverse('staff_dashboard'),
        )

        storefront_response = self.client.get(reverse('home'))
        self.assertContains(storefront_response, 'Staff dashboard')
        self.assertNotContains(storefront_response, 'Django admin')
        self.assertNotContains(storefront_response, 'Admin dashboard')
        self.assertContains(storefront_response, 'Logout')

    def test_staff_login_redirects_owner_group_to_owner_dashboard(self):
        owner_group = Group.objects.create(name='Owner')
        owner = get_user_model().objects.create_user(
            username='store-owner',
            password='owner-password',
            is_staff=True,
        )
        owner.groups.add(owner_group)

        response = self.client.post(
            reverse('staff_login'),
            {'username': owner.username, 'password': 'owner-password'},
        )

        self.assertRedirects(response, reverse('admin_dashboard'))
        self.assertEqual(
            self.client.get(reverse('staff_dashboard')).status_code,
            200,
        )
        admin_dashboard_response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(admin_dashboard_response.status_code, 200)
        self.assertContains(admin_dashboard_response, reverse('staff_dashboard'))
        storefront_response = self.client.get(reverse('home'))
        self.assertContains(storefront_response, 'Owner dashboard')
        self.assertNotContains(storefront_response, 'Staff dashboard')
        self.assertNotContains(storefront_response, 'Django admin')

    def test_staff_login_redirects_superuser_to_super_admin_dashboard(self):
        response = self.client.post(
            reverse('staff_login'),
            {'username': self.staff_user.username, 'password': 'secretpass'},
        )

        self.assertRedirects(response, reverse('super_admin_dashboard'))
        storefront_response = self.client.get(reverse('home'))
        self.assertContains(storefront_response, 'Super admin dashboard')
        self.assertNotContains(storefront_response, 'Staff dashboard')
        self.assertNotContains(storefront_response, 'Owner dashboard')

    def test_super_admin_dashboard_has_admin_and_storefront_shortcuts(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('super_admin_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Super admin dashboard')
        self.assertContains(response, reverse('admin:index'))
        self.assertContains(response, reverse('admin:shop_order_changelist'))
        self.assertContains(response, reverse('admin:shop_product_changelist'))
        self.assertContains(response, reverse('admin:auth_user_changelist'))
        self.assertContains(response, reverse('admin_dashboard'))
        self.assertContains(response, reverse('staff_dashboard'))
        self.assertEqual(self.client.get(reverse('admin_dashboard')).status_code, 200)
        self.assertEqual(self.client.get(reverse('staff_dashboard')).status_code, 200)

    def test_owner_cannot_access_super_admin_dashboard(self):
        owner_group = Group.objects.create(name='Owner')
        owner = get_user_model().objects.create_user(
            username='store-owner',
            password='owner-password',
            is_staff=True,
        )
        owner.groups.add(owner_group)
        self.client.force_login(owner)

        response = self.client.get(reverse('super_admin_dashboard'))

        self.assertRedirects(response, reverse('admin_dashboard'))

    def test_staff_login_rejects_non_staff_account(self):
        customer = get_user_model().objects.create_user(
            username='store-customer',
            password='customer-password',
        )

        response = self.client.post(
            reverse('staff_login'),
            {'username': customer.username, 'password': 'customer-password'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This account does not have staff access.')
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_staff_logout_redirects_to_home(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(reverse('staff_logout'))

        self.assertRedirects(response, reverse('home'))
        storefront_response = self.client.get(reverse('home'))
        self.assertContains(storefront_response, 'Login')
        self.assertNotContains(storefront_response, 'Logout')

    def test_staff_can_update_order_status(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'shipped'},
        )

        self.assertRedirects(response, reverse('admin_dashboard'))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'shipped')

    def test_admin_order_status_update_prefetches_product_variants(self):
        order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=1,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('1200.00'),
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'email': self.order.email,
                'address': self.order.address,
                'thana': self.order.thana,
                'zilla': self.order.zilla,
                'delivery_area': self.order.delivery_area,
                'status': 'processing',
                f'item_quantity_{order_item.pk}': '1',
            },
        )

        self.assertRedirects(response, f"{reverse('staff_dashboard')}?page=1")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'processing')

    def test_staff_order_row_autosave_returns_updated_order_data(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'full_name': 'Autosaved Buyer',
                'phone': '12345678901',
                'dashboard_autosave': '1',
                'email': self.order.email,
                'address': self.order.address,
                'thana': self.order.thana,
                'zilla': self.order.zilla,
                'delivery_area': self.order.delivery_area,
                'status': 'processing',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assertEqual(response.json()['status'], 'processing')
        self.assertIn('total', response.json())
        self.order.refresh_from_db()
        self.assertEqual(self.order.full_name, 'Autosaved Buyer')
        self.assertEqual(self.order.phone, '01700000000')
        self.assertEqual(self.order.status, 'processing')

    def test_admin_dashboard_shows_status_sales_best_sellers_and_low_stock(self):
        order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=2,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('2400.00'),
        )
        low_stock_product = create_test_product(
            self.category,
            'Low Stock Phone',
            'low-stock-phone',
            Decimal('800.00'),
        )
        low_stock_product.stock.quantity = low_stock_product.stock.opening_quantity = 3
        low_stock_product.stock.save(update_fields=['quantity', 'opening_quantity'])
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('admin_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'href="{reverse("staff_dashboard")}" class="btn btn-secondary">Staff dashboard &amp; activity</a>',
        )
        self.assertContains(response, 'Orders by status')
        self.assertContains(response, 'Best-selling items')
        self.assertContains(response, 'Orders details')
        self.assertContains(response, f'#{self.order.display_order_number}')
        self.assertContains(response, low_stock_product.name)
        self.assertContains(response, '3 <small>left</small>', html=True)
        self.assertEqual(response.context['total_orders'], 1)
        self.assertEqual(response.context['status_summary'][0]['order_count'], 1)
        self.assertEqual(response.context['status_summary'][0]['amount'], Decimal('1260.00'))
        self.assertEqual(response.context['best_selling'][0]['total_sold'], 2)
        self.assertEqual(response.context['best_selling'][0]['total_sales'], Decimal('2400.00'))

    def test_admin_dashboard_date_range_filters_order_and_product_metrics(self):
        order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=2,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('2400.00'),
        )
        older_order = Order.objects.create(
            full_name='Older Buyer',
            phone='01711111111',
            address='Chattogram',
            subtotal=Decimal('500.00'),
            total=Decimal('500.00'),
            status='delivered',
        )
        Order.objects.filter(pk=older_order.pk).update(
            created_at=timezone.now() - timedelta(days=10),
        )
        OrderItem.objects.create(
            order=older_order,
            product=self.product,
            product_name=self.product.name,
            quantity=20,
            unit_price=Decimal('25.00'),
            line_total=Decimal('500.00'),
        )
        self.client.force_login(self.staff_user)
        today = timezone.localdate().isoformat()

        response = self.client.get(
            reverse('admin_dashboard'),
            {'start_date': today, 'end_date': today},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_orders'], 1)
        self.assertEqual(response.context['order_details'].paginator.count, 1)
        self.assertEqual(response.context['status_summary'][0]['order_count'], 1)
        self.assertEqual(response.context['best_selling'][0]['total_sold'], 2)

        update_response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'status': 'confirmed',
                'start_date': today,
                'end_date': today,
            },
        )
        self.assertRedirects(
            update_response,
            f'{reverse("admin_dashboard")}?start_date={today}&end_date={today}',
        )

    def test_admin_dashboard_reports_invalid_date_filters_without_showing_unfiltered_metrics(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('admin_dashboard'), {'start_date': 'not-a-date'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Enter a valid date.')
        self.assertEqual(response.context['total_orders'], 0)

    def test_staff_orders_table_shows_order_financials(self):
        self.order.thana = 'Dhanmondi'
        self.order.zilla = 'Dhaka'
        self.order.address = '4 Ambarkhana Road'
        self.order.city = 'Sylhet'
        self.order.delivery_area = 'outside_dhaka'
        self.order.subtotal = Decimal('2400.00')
        self.order.shipping_charge = Decimal('150.00')
        self.order.total = Decimal('2550.00')
        self.order.save()
        SiteSettings.objects.create(
            inside_dhaka_charge=Decimal('75.00'),
            outside_dhaka_charge=Decimal('150.00'),
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name='Phone X',
            quantity=2,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('2400.00'),
        )
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('staff_dashboard'))

        for heading in (
            'Order Number',
            'Date',
            'Customer name',
            'Phone',
            'Delivery address',
            'Order Item',
            'Product Qty',
            'Shipping Cost',
            'Order Total',
            'Order Status',
            'Action',
        ):
            self.assertContains(response, f'<th>{heading}</th>', html=True)
        self.assertNotContains(response, '<th>Product Price</th>', html=True)
        self.assertContains(response, 'Dhanmondi')
        self.assertContains(response, 'name="zilla" value="Dhaka"')
        self.assertContains(response, 'Phone X — ৳1200.00')
        self.assertContains(response, 'data-inside-shipping-rate="75.00"')
        self.assertContains(response, 'data-outside-shipping-rate="150.00"')
        self.assertContains(response, 'class="staff-shipping-cost">150.00</span>')
        self.assertContains(response, 'class="staff-order-total-amount">2550.00</span>')
        self.assertContains(response, 'function updateOrderAmounts(row)')
        self.assertNotContains(response, 'updateShippingCharge')
        self.assertContains(
            response,
            f'class="staff-order-field staff-status-select status-{self.order.status}"',
        )
        self.assertContains(response, "select.classList.add('status-' + select.value)")
        self.assertContains(response, 'staff-order-save-status')
        self.assertNotContains(response, 'staff-order-save-btn')
        self.assertContains(response, 'readonly aria-readonly="true"')
        self.assertContains(
            response,
            f'name="delivery_area" value="{self.order.delivery_area}"',
        )
        self.assertContains(response, '4 Ambarkhana Road')

    def test_staff_dashboard_shows_order_amount_and_details_link(self):
        self.order.shipping_charge = Decimal('60.00')
        self.order.total = Decimal('1260.00')
        self.order.save(update_fields=['shipping_charge', 'total'])
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('staff_dashboard'))

        self.assertContains(response, '<th>Delivery address</th>', html=True)
        self.assertNotContains(response, '<th>Thana</th>', html=True)
        self.assertNotContains(response, '<th>Zilla</th>', html=True)
        self.assertContains(response, '<col class="staff-order-col-action">', html=True)
        self.assertContains(
            response,
            f'name="thana" value="{self.order.thana}"',
        )
        self.assertContains(
            response,
            f'name="zilla" value="{self.order.zilla}"',
        )
        self.assertContains(response, '<th>Shipping Cost</th>', html=True)
        self.assertContains(response, '<th>Order Total</th>', html=True)
        self.assertContains(response, '<th>Pathao Status</th>', html=True)
        self.assertContains(response, '<span class="staff-pathao-status-empty">Not sent</span>', html=True)
        self.assertContains(response, f'#{self.order.display_order_number}')
        self.assertContains(response, 'class="staff-shipping-cost">60.00</span>')
        self.assertContains(response, 'class="staff-order-total-amount">1260.00</span>')
        self.assertContains(
            response,
            f'href="{reverse("staff_order_detail", args=[self.order.pk])}?page=1"',
        )
        self.assertContains(
            response,
            f'href="{reverse("staff_call_log", args=[self.order.pk])}"',
        )
        self.assertContains(
            response,
            f'aria-label="Add or view call notes for order #{self.order.display_order_number}"',
        )
        self.assertContains(
            response,
            '<span class="staff-call-log-count" aria-hidden="true">0</span>',
            html=True,
        )

        StaffCallLog.objects.create(
            order=self.order,
            staff_member=self.staff_user,
            note='Call back after 6 PM.',
        )
        response = self.client.get(reverse('staff_dashboard'))
        self.assertContains(
            response,
            '<span class="staff-call-log-count" aria-hidden="true">1</span>',
            html=True,
        )
        self.assertContains(
            response,
            'title="Last call: Call back after 6 PM. — staff · ',
        )

        PathaoShipment.objects.create(
            order=self.order,
            environment='sandbox',
            consignment_id='C-PENDING-123',
            merchant_order_id=str(self.order.display_order_number),
            order_status='Pending',
        )
        response = self.client.get(reverse('staff_dashboard'))
        self.assertContains(response, '<th>Pathao Status</th>', html=True)
        self.assertContains(
            response,
            f'aria-label="Pathao status Pending for order #{self.order.display_order_number}"',
        )
        self.assertContains(
            response,
            f'href="{reverse("staff_pathao_order", args=[self.order.pk])}"',
        )

    def test_staff_can_add_and_review_order_call_history(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('staff_call_log', args=[self.order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Call history')
        self.assertContains(response, self.order.full_name)
        self.assertContains(response, 'No call notes yet')

        response = self.client.post(
            reverse('staff_call_log', args=[self.order.pk]),
            {'note': 'Customer was busy; requested a call after 6 PM.'},
        )

        self.assertRedirects(response, reverse('staff_call_log', args=[self.order.pk]))
        call_log = StaffCallLog.objects.get(order=self.order)
        self.assertEqual(call_log.note, 'Customer was busy; requested a call after 6 PM.')
        self.assertEqual(call_log.staff_member, self.staff_user)

        history_response = self.client.get(reverse('staff_call_log', args=[self.order.pk]))
        self.assertContains(history_response, 'Customer was busy; requested a call after 6 PM.')
        self.assertContains(history_response, self.staff_user.username)

    def test_staff_can_add_and_view_call_log_on_order_detail(self):
        self.client.force_login(self.staff_user)

        detail_response = self.client.get(
            reverse('staff_order_detail', args=[self.order.pk])
        )
        self.assertContains(detail_response, 'class="dashboard-grid four-column"')
        self.assertContains(
            detail_response,
            f'href="tel:{self.order.phone}"',
        )
        self.assertContains(detail_response, 'name="return_to" value="detail"')

        response = self.client.post(
            reverse('staff_call_log', args=[self.order.pk]),
            {
                'return_to': 'detail',
                'page': '3',
                'note': 'Customer confirmed delivery for tomorrow.',
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('staff_order_detail', args=[self.order.pk])}?page=3",
        )
        call_log = StaffCallLog.objects.get(order=self.order)
        self.assertEqual(call_log.note, 'Customer confirmed delivery for tomorrow.')
        self.assertEqual(call_log.staff_member, self.staff_user)
        detail_response = self.client.get(
            reverse('staff_order_detail', args=[self.order.pk])
        )
        self.assertContains(
            detail_response,
            'Customer confirmed delivery for tomorrow.',
        )
        self.assertContains(detail_response, self.staff_user.username)

    def test_order_detail_shows_all_call_logs_inline_with_staff_and_time(self):
        for note in (
            'Cancel this item',
            'Customer requested a callback',
            'Confirmed delivery address',
            'Customer approved replacement',
            'Follow up tomorrow',
        ):
            StaffCallLog.objects.create(
                order=self.order,
                staff_member=self.staff_user,
                note=note,
            )
        self.client.force_login(self.staff_user)

        response = self.client.get(
            reverse('staff_order_detail', args=[self.order.pk])
        )

        for note in (
            'Cancel this item',
            'Customer requested a callback',
            'Confirmed delivery address',
            'Customer approved replacement',
            'Follow up tomorrow',
            self.staff_user.username,
        ):
            self.assertContains(response, note)
        self.assertContains(response, 'class="staff-detail-call-entry"')
        self.assertContains(response, 'Open call history')

    def test_call_notes_are_registered_in_django_admin(self):
        StaffCallLog.objects.create(
            order=self.order,
            staff_member=self.staff_user,
            note='Customer requested a callback tomorrow.',
        )
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('admin:shop_staffcalllog_changelist'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Customer requested a callback tomorrow.')
        self.assertContains(response, self.order.full_name)

    def test_call_log_requires_staff_login(self):
        response = self.client.get(reverse('staff_call_log', args=[self.order.pk]))

        self.assertRedirects(
            response,
            f'{reverse("staff_login")}?next={reverse("staff_call_log", args=[self.order.pk])}',
        )

    def test_staff_can_filter_orders_and_download_matching_excel(self):
        other_order = Order.objects.create(
            full_name='Shipped Buyer',
            phone='01700000001',
            address='Chattogram',
            subtotal=Decimal('500.00'),
            shipping_charge=Decimal('120.00'),
            total=Decimal('620.00'),
            status='shipped',
        )
        PathaoShipment.objects.create(
            order=self.order,
            merchant_order_id=str(self.order.display_order_number),
            consignment_id='PATHAO123',
            order_status='Pending',
        )
        self.client.force_login(self.staff_user)
        filter_params = {
            'start_date': timezone.localdate().isoformat(),
            'end_date': timezone.localdate().isoformat(),
            'status': 'pending',
        }

        dashboard_response = self.client.get(reverse('staff_dashboard'), filter_params)
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertContains(dashboard_response, self.order.full_name)
        self.assertNotContains(dashboard_response, other_order.full_name)
        self.assertContains(dashboard_response, 'Download Excel')

        export_response = self.client.get(reverse('staff_export_orders'), filter_params)
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        workbook = load_workbook(BytesIO(export_response.content), read_only=True)
        rows = list(workbook.active.iter_rows(values_only=True))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1:3], ('Pathao Status', 'Consignment ID'))
        self.assertEqual(rows[1][0], self.order.display_order_number)
        self.assertEqual(rows[1][1:3], ('Pending', 'PATHAO123'))
        self.assertEqual(rows[1][4], 'Pending')
        self.assertNotIn('Subtotal', rows[0])
        self.assertNotIn('Total', rows[0])
        self.assertNotIn('Amount paid', rows[0])
        self.assertNotIn('Unit price', rows[0])
        self.assertNotIn('Line total', rows[0])

        all_orders_response = self.client.get(reverse('staff_export_orders'))
        all_orders = list(
            load_workbook(BytesIO(all_orders_response.content), read_only=True)
            .active.iter_rows(values_only=True)
        )
        self.assertEqual(len(all_orders), 3)
        all_rows_by_order_number = {row[0]: row for row in all_orders[1:]}
        self.assertEqual(
            set(all_rows_by_order_number),
            {self.order.display_order_number, other_order.display_order_number},
        )
        self.assertEqual(
            all_rows_by_order_number[other_order.display_order_number][1:3],
            (None, None),
        )

    def test_staff_order_filters_reject_invalid_ranges_and_statuses(self):
        self.client.force_login(self.staff_user)

        dashboard_response = self.client.get(
            reverse('staff_dashboard'),
            {'start_date': '2026-10-10', 'end_date': '2026-10-01'},
        )
        self.assertContains(
            dashboard_response,
            'The start date must be on or before the end date.',
        )
        self.assertEqual(
            self.client.get(
                reverse('staff_export_orders'),
                {'status': 'not-a-status'},
            ).status_code,
            400,
        )

    def test_staff_order_detail_shows_complete_order_and_supports_updates(self):
        self.order.payment_method = 'bKash'
        self.order.payment_number = '01711111111'
        self.order.transaction_reference = 'TXN-123'
        self.order.payment_amount = Decimal('1260.00')
        self.order.subtotal = Decimal('1200.00')
        self.order.shipping_charge = Decimal('60.00')
        self.order.total = Decimal('1260.00')
        self.order.note = 'Please call before delivery.'
        self.order.fulfillment_note = 'Pack carefully.'
        self.order.save()
        SiteSettings.objects.create(
            inside_dhaka_charge=Decimal('75.00'),
            outside_dhaka_charge=Decimal('150.00'),
        )
        order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=1,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('1200.00'),
            selected_size='L',
            selected_color='Blue',
        )
        ProductVariant.objects.create(
            product=self.product,
            size='L',
            color='Blue',
            price=Decimal('1200.00'),
            quantity=5,
            opening_quantity=5,
        )
        ProductVariant.objects.create(
            product=self.product,
            size='XL',
            color='Red',
            price=Decimal('1800.00'),
            quantity=2,
            opening_quantity=2,
        )
        alternate_variant = ProductVariant.objects.get(
            product=self.product,
            size='XL',
            color='Red',
        )
        alternate_variant.opening_quantity = 2
        alternate_variant.quantity = 2
        alternate_variant.save(update_fields=['opening_quantity', 'quantity'])
        sync_product_stock(self.product.pk)
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('staff_order_detail', args=[self.order.pk]))
        dashboard_response = self.client.get(reverse('staff_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="panel-box full-panel staff-fulfillment-panel"')
        self.assertContains(response, 'class="staff-fulfillment-card"')
        self.assertContains(response, 'class="staff-fulfillment-card staff-shipping-card"')
        for value in (
            'buyer@example.com',
            'Email',
            'name="email"',
            '01711111111',
            'TXN-123',
            'Please call before delivery.',
            'Pack carefully.',
            'Fulfillment notes',
            'item_product_new',
            'item_quantity_new',
            'Phone X',
            'L',
            'Blue',
            'Save now',
            'Changes save automatically.',
            f'name="item_size_{order_item.pk}"',
            f'name="item_color_{order_item.pk}"',
            f'data-item-id="{order_item.pk}"',
            '<option value="XL">XL</option>',
            '<option value="Red">Red</option>',
            '৳1200.00',
            '৳60.00',
            '৳1260.00',
            'Order summary',
            'Call log',
            'Add call log',
            f'href="tel:{self.order.phone}"',
            'Subtotal:',
            'Amount paid',
        ):
            self.assertContains(response, value)
        self.assertContains(response, 'name="item_quantity_%s"' % order_item.pk)
        self.assertContains(response, 'Unit price')
        self.assertContains(response, 'Line total')
        self.assertContains(response, 'staff-order-subtotal')
        self.assertContains(
            dashboard_response,
            f'name="item_size_{order_item.pk}"',
        )
        self.assertContains(
            dashboard_response,
            f'name="item_color_{order_item.pk}"',
        )
        self.assertContains(dashboard_response, '<option value="XL">XL</option>')
        self.assertContains(dashboard_response, 'Phone X — ৳1200.00')
        self.assertContains(dashboard_response, 'class="staff-shipping-cost">60.00</span>')
        self.assertContains(dashboard_response, 'class="staff-order-total-amount">1260.00</span>')

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'return_to': 'detail',
                'page': '1',
                'full_name': 'Updated Buyer',
                'phone': self.order.phone,
                'email': 'updated@example.com',
                'address': self.order.address,
                'thana': '',
                'zilla': '',
                'delivery_area': 'outside_dhaka',
                'status': 'processing',
                f'item_quantity_{order_item.pk}': '2',
                f'item_size_{order_item.pk}': 'XL',
                f'item_color_{order_item.pk}': 'Red',
                f'item_free_shipping_{order_item.pk}': 'false',
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('staff_order_detail', args=[self.order.pk])}?page=1",
        )
        self.order.refresh_from_db()
        order_item.refresh_from_db()
        self.assertEqual(self.order.full_name, 'Updated Buyer')
        self.assertEqual(self.order.email, 'updated@example.com')
        self.assertEqual(self.order.status, 'processing')
        self.assertEqual(order_item.quantity, 2)
        self.assertEqual(self.order.subtotal, Decimal('3600.00'))
        self.assertEqual(self.order.shipping_charge, Decimal('150.00'))
        self.assertEqual(self.order.total, Decimal('3750.00'))
        self.assertEqual(order_item.selected_size, 'XL')
        self.assertEqual(order_item.selected_color, 'Red')

    def test_staff_order_pages_show_dropdowns_with_one_configured_option(self):
        ProductVariant.objects.create(
            product=self.product,
            size='Standard',
            color='Cream',
            price=Decimal('1200.00'),
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=1,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('1200.00'),
            selected_size='Standard',
            selected_color='Cream',
        )
        self.client.force_login(self.staff_user)

        detail_response = self.client.get(reverse('staff_order_detail', args=[self.order.pk]))
        dashboard_response = self.client.get(reverse('staff_dashboard'))

        self.assertContains(
            detail_response,
            '<label class="staff-color-option">',
            html=False,
        )
        self.assertContains(detail_response, '<option value="Cream" selected>Cream</option>', html=True)
        self.assertContains(
            dashboard_response,
            '<label class="staff-size-option" hidden>',
            html=False,
        )
        self.assertContains(
            dashboard_response,
            '<label class="staff-color-option" hidden>',
            html=False,
        )

    def test_staff_can_add_product_to_order_and_save_fulfillment_notes(self):
        added_product = create_test_product(
            self.category,
            'USB Charger',
            'usb-charger',
            Decimal('500.00'),
        )
        added_product.variants.all().delete()
        variant = ProductVariant.objects.create(
            product=added_product,
            size='Standard',
            color='Black',
            price=Decimal('500.00'),
            quantity=4,
            opening_quantity=4,
        )
        sync_product_stock(added_product.pk)
        self.order.delivery_area = 'outside_dhaka'
        self.order.save(update_fields=['delivery_area'])
        SiteSettings.objects.create(
            inside_dhaka_charge=Decimal('75.00'),
            outside_dhaka_charge=Decimal('150.00'),
        )
        self.client.force_login(self.staff_user)

        detail_response = self.client.get(
            reverse('staff_order_detail', args=[self.order.pk])
        )
        self.assertContains(detail_response, 'name="item_product_new"')
        self.assertContains(detail_response, 'USB Charger — ৳500.00')
        self.assertContains(detail_response, 'name="fulfillment_note"')

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'return_to': 'detail',
                'page': '1',
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'email': self.order.email,
                'address': self.order.address,
                'thana': '',
                'zilla': '',
                'delivery_area': 'outside_dhaka',
                'status': 'pending',
                'item_product_new': str(added_product.pk),
                'item_quantity_new': '2',
                'item_size_new': 'Standard',
                'item_color_new': 'Black',
                'fulfillment_note': 'Wrap the charger separately.',
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('staff_order_detail', args=[self.order.pk])}?page=1",
        )
        self.order.refresh_from_db()
        variant.refresh_from_db()
        added_order_item = OrderItem.objects.get(order=self.order)
        self.assertEqual(added_order_item.product, added_product)
        self.assertEqual(added_order_item.quantity, 2)
        self.assertEqual(added_order_item.unit_price, Decimal('500.00'))
        self.assertEqual(added_order_item.line_total, Decimal('1000.00'))
        self.assertEqual(added_order_item.selected_size, 'Standard')
        self.assertEqual(added_order_item.selected_color, 'Black')
        self.assertEqual(variant.quantity, 2)
        self.assertEqual(self.order.subtotal, Decimal('1000.00'))
        self.assertEqual(self.order.shipping_charge, Decimal('150.00'))
        self.assertEqual(self.order.total, Decimal('1150.00'))
        self.assertEqual(
            self.order.fulfillment_note,
            'Wrap the charger separately.',
        )

    def test_order_detail_autosave_updates_order_and_returns_summary(self):
        variant = self.product.variants.get()
        item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            variant=variant,
            product_name=self.product.name,
            quantity=1,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('1200.00'),
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'return_to': 'detail',
                'page': '1',
                'full_name': 'Autosaved Buyer',
                'phone': self.order.phone,
                'email': self.order.email,
                'address': self.order.address,
                'thana': 'Dhanmondi',
                'zilla': 'Dhaka',
                'delivery_area': 'inside_dhaka',
                'status': 'processing',
                'cash_discount': '10.00',
                'fulfillment_note': 'Call before dispatch.',
                f'item_product_{item.pk}': str(self.product.pk),
                f'item_quantity_{item.pk}': '1',
                f'item_free_shipping_{item.pk}': 'false',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertTrue(response_data['success'])
        self.assertFalse(response_data['added_item'])
        self.assertEqual(response_data['total'], '1250.00')
        self.order.refresh_from_db()
        self.assertEqual(self.order.full_name, 'Autosaved Buyer')
        self.assertEqual(self.order.status, 'processing')
        self.assertEqual(self.order.fulfillment_note, 'Call before dispatch.')
        self.assertEqual(self.order.cash_discount, Decimal('10.00'))
        self.assertEqual(self.order.total, Decimal('1250.00'))

    def test_staff_cannot_add_more_products_than_available_stock(self):
        added_product = create_test_product(
            self.category,
            'USB Charger',
            'usb-charger',
            Decimal('500.00'),
        )
        variant = added_product.variants.get()
        variant.quantity = 1
        variant.opening_quantity = 1
        variant.save(update_fields=['quantity', 'opening_quantity'])
        sync_product_stock(added_product.pk)
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'return_to': 'detail',
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'email': self.order.email,
                'address': self.order.address,
                'thana': '',
                'zilla': '',
                'delivery_area': 'inside_dhaka',
                'status': 'pending',
                'item_product_new': str(added_product.pk),
                'item_quantity_new': '2',
                'item_size_new': '',
                'item_color_new': '',
                'fulfillment_note': '',
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('staff_order_detail', args=[self.order.pk])}?page=1",
        )
        self.assertFalse(OrderItem.objects.filter(order=self.order).exists())
        variant.refresh_from_db()
        self.assertEqual(variant.quantity, 1)

    def test_staff_can_select_another_configured_product_color(self):
        ProductVariant.objects.create(
            product=self.product,
            size='L',
            color='Cream',
            price=Decimal('1200.00'),
            quantity=2,
            opening_quantity=2,
        )
        ProductVariant.objects.create(
            product=self.product,
            size='L',
            color='White',
            price=Decimal('1250.00'),
            quantity=1,
            opening_quantity=1,
        )
        order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=1,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('1200.00'),
            selected_size='L',
            selected_color='Cream',
        )
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('staff_order_detail', args=[self.order.pk]))

        self.assertContains(response, '<option value="Cream" selected>Cream</option>', html=True)
        self.assertContains(response, '<option value="White">White</option>', html=True)
        self.assertNotContains(
            response,
            '<select class="staff-order-field staff-color-select" name="item_color_1" aria-label="Color of Phone X" disabled',
        )

        dashboard_response = self.client.get(reverse('staff_dashboard'))
        self.assertContains(dashboard_response, '<label class="staff-size-option" hidden>', html=False)
        self.assertNotContains(dashboard_response, '<label class="staff-color-option" hidden>', html=False)
        self.assertContains(dashboard_response, '<option value="White">White</option>', html=True)

        update_response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'return_to': 'detail',
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'email': self.order.email or '',
                'address': self.order.address,
                'thana': self.order.thana,
                'zilla': self.order.zilla,
                'delivery_area': self.order.delivery_area,
                'status': self.order.status,
                f'item_product_{order_item.pk}': str(self.product.pk),
                f'item_size_{order_item.pk}': 'L',
                f'item_color_{order_item.pk}': 'White',
                f'item_quantity_{order_item.pk}': '1',
                f'item_free_shipping_{order_item.pk}': 'false',
            },
            follow=True,
        )

        self.assertContains(
            update_response,
            f'Order #{self.order.display_order_number} details and quantities updated.',
        )
        order_item.refresh_from_db()
        self.assertEqual(order_item.selected_color, 'White')
        self.assertEqual(order_item.unit_price, Decimal('1250.00'))

        dashboard_update = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'email': self.order.email or '',
                'address': self.order.address,
                'thana': self.order.thana,
                'zilla': self.order.zilla,
                'delivery_area': self.order.delivery_area,
                'status': self.order.status,
                f'item_product_{order_item.pk}': str(self.product.pk),
                f'item_size_{order_item.pk}': 'L',
                f'item_color_{order_item.pk}': 'Cream',
                f'item_quantity_{order_item.pk}': '1',
                f'item_free_shipping_{order_item.pk}': 'false',
            },
            follow=True,
        )

        self.assertContains(
            dashboard_update,
            f'Order #{self.order.display_order_number} details and quantities updated.',
        )
        order_item.refresh_from_db()
        self.assertEqual(order_item.selected_color, 'Cream')
        self.assertEqual(order_item.unit_price, Decimal('1200.00'))

    def test_staff_cannot_set_size_or_color_not_configured_for_product(self):
        order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=1,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('1200.00'),
            selected_size='M',
            selected_color='Black',
        )
        configure_test_product_variants(
            self.product,
            [('L', '', 10)],
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'return_to': 'detail',
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'address': self.order.address,
                'thana': self.order.thana,
                'zilla': self.order.zilla,
                'delivery_area': self.order.delivery_area,
                'status': self.order.status,
                f'item_quantity_{order_item.pk}': '1',
                f'item_size_{order_item.pk}': 'XXL',
                f'item_color_{order_item.pk}': 'Black',
            },
            follow=True,
        )

        self.assertContains(response, 'each size/color is valid for its product')
        order_item.refresh_from_db()
        self.assertEqual(order_item.selected_size, 'M')

    def test_staff_can_download_a4_order_invoice(self):
        self.order.note = 'Call before delivery.'
        self.order.save(update_fields=['note'])
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=1,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('1200.00'),
            selected_size='L',
            selected_color='Blue',
        )
        SiteSettings.objects.create(
            brand_name='Invoice Store',
            phone='01700000000',
            email='invoice@example.com',
            address='Dhaka, Bangladesh',
        )
        self.client.force_login(self.staff_user)

        detail_response = self.client.get(reverse('staff_order_detail', args=[self.order.pk]))
        self.assertContains(
            detail_response,
            f'href="{reverse("staff_order_invoice", args=[self.order.pk])}"',
        )
        self.assertContains(detail_response, 'Download Invoice')
        response = self.client.get(reverse('staff_order_invoice', args=[self.order.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(
            response['Content-Disposition'],
            f'attachment; filename="invoice-order-{self.order.display_order_number}.pdf"',
        )
        self.assertTrue(response.content.startswith(b'%PDF-'))
        self.assertIn(b'/Title (Invoice - Order #', response.content)
        self.assertIn(b'595.2756', response.content)
        self.assertIn(b'841.8898', response.content)

    def test_staff_cash_discount_reduces_order_and_appears_on_invoice(self):
        variant = self.product.variants.get()
        variant.price = Decimal('6020.00')
        variant.save(update_fields=['price'])
        self.product.free_shipping = True
        self.product.save(update_fields=['free_shipping'])
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            variant=variant,
            product_name=self.product.name,
            quantity=1,
            unit_price=Decimal('6020.00'),
            line_total=Decimal('6020.00'),
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'return_to': 'detail',
                'page': '1',
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'email': self.order.email,
                'address': self.order.address,
                'thana': '',
                'zilla': '',
                'delivery_area': 'inside_dhaka',
                'status': 'pending',
                'cash_discount': '20.00',
                f'item_quantity_{self.order.items.get().pk}': '1',
                f'item_size_{self.order.items.get().pk}': '',
                f'item_color_{self.order.items.get().pk}': '',
                f'item_free_shipping_{self.order.items.get().pk}': 'true',
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('staff_order_detail', args=[self.order.pk])}?page=1",
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.subtotal, Decimal('6020.00'))
        self.assertEqual(self.order.cash_discount, Decimal('20.00'))
        self.assertEqual(self.order.total, Decimal('6000.00'))
        detail_response = self.client.get(
            reverse('staff_order_detail', args=[self.order.pk])
        )
        self.assertContains(detail_response, 'Cash discount')
        self.assertContains(detail_response, 'value="20.00"')
        self.assertContains(detail_response, 'name="cash_discount"')

        invoice_response = self.client.get(
            reverse('staff_order_invoice', args=[self.order.pk])
        )
        self.assertEqual(invoice_response.status_code, 200)
        self.assertEqual(invoice_response['Content-Type'], 'application/pdf')
        self.assertTrue(invoice_response.content.startswith(b'%PDF-'))

        invalid_response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'return_to': 'detail',
                'page': '1',
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'email': self.order.email,
                'address': self.order.address,
                'thana': '',
                'zilla': '',
                'delivery_area': 'inside_dhaka',
                'status': 'pending',
                'cash_discount': '6021.00',
                f'item_quantity_{self.order.items.get().pk}': '1',
                f'item_size_{self.order.items.get().pk}': '',
                f'item_color_{self.order.items.get().pk}': '',
                f'item_free_shipping_{self.order.items.get().pk}': 'true',
            },
            follow=True,
        )
        self.assertContains(
            invalid_response,
            'Cash discount cannot exceed the order total.',
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.cash_discount, Decimal('20.00'))
        self.assertEqual(self.order.total, Decimal('6000.00'))

    def test_regular_staff_can_view_order_detail(self):
        operator = get_user_model().objects.create_user(
            username='order-viewer',
            password='viewer-password',
            is_staff=True,
        )
        self.client.force_login(operator)

        response = self.client.get(reverse('staff_order_detail', args=[self.order.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Order #')
        self.assertContains(response, 'class="dashboard-grid four-column"')
        self.assertEqual(response.content.decode().count('class="dashboard-column'), 4)

    def test_cash_on_delivery_shows_zero_paid_and_keeps_entered_payment_details_visible(self):
        self.order.payment_number = '01711111111'
        self.order.transaction_reference = 'COD-NOTE-123'
        self.order.payment_amount = None
        self.order.save()
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('staff_order_detail', args=[self.order.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Cash on Delivery')
        self.assertContains(response, '৳0.00')
        self.assertContains(response, self.order.payment_number)
        self.assertContains(response, self.order.transaction_reference)

    def test_staff_dashboard_paginates_orders_twenty_at_a_time(self):
        for index in range(20):
            Order.objects.create(
                full_name=f'Buyer {index}',
                phone=f'01700000{index:03d}',
                address='Dhaka',
                total=Decimal('100.00'),
            )
        self.client.force_login(self.staff_user)

        first_page = self.client.get(reverse('staff_dashboard'))
        second_page = self.client.get(reverse('staff_dashboard'), {'page': 2})

        self.assertEqual(first_page.context['orders'].paginator.per_page, 20)
        self.assertEqual(len(first_page.context['orders'].object_list), 20)
        self.assertContains(first_page, 'Page 1 of 2')
        self.assertContains(first_page, '?page=2')
        self.assertEqual(len(second_page.context['orders'].object_list), 1)
        self.assertContains(second_page, 'Page 2 of 2')
        first_page_ids = {order.pk for order in first_page.context['orders'].object_list}
        second_page_ids = {order.pk for order in second_page.context['orders'].object_list}
        self.assertFalse(first_page_ids & second_page_ids)
        self.assertEqual(len(first_page_ids | second_page_ids), 21)

    def test_regular_staff_can_update_order_status_and_returns_to_staff_dashboard(self):
        operator = get_user_model().objects.create_user(
            username='order-operator',
            password='operator-password',
            is_staff=True,
        )
        self.client.force_login(operator)
        dashboard_before_update = self.client.get(reverse('staff_dashboard'))
        self.assertContains(
            dashboard_before_update,
            f'name="delivery_area" form="order-update-{self.order.pk}"',
        )
        self.assertContains(dashboard_before_update, 'Inside Dhaka')
        self.assertContains(dashboard_before_update, 'Outside Dhaka')

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'processing'},
        )

        self.assertRedirects(response, reverse('staff_dashboard'))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'processing')

    def test_delivering_order_preserves_product_free_shipping(self):
        self.product.free_shipping = True
        self.product.save(update_fields=['free_shipping'])
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=1,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('1200.00'),
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'delivered'},
        )

        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.order.status, 'delivered')
        self.assertTrue(self.product.free_shipping)

    def test_staff_can_edit_customer_and_delivery_details_in_order_row(self):
        operator = get_user_model().objects.create_user(
            username='order-editor',
            password='editor-password',
            is_staff=True,
        )
        order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=2,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('2400.00'),
        )
        self.client.force_login(operator)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'full_name': 'Updated Customer',
                'phone': '01712345678',
                'address': '4 Ambarkhana Road',
                'thana': 'Ambarkhana',
                'zilla': 'Sylhet',
                'delivery_area': 'outside_dhaka',
                'status': 'processing',
                'page': '2',
                'dashboard_autosave': '1',
                f'item_quantity_{order_item.pk}': '3',
            },
        )

        self.assertRedirects(response, f'{reverse("staff_dashboard")}?page=2')
        self.order.refresh_from_db()
        self.assertEqual(self.order.full_name, 'Updated Customer')
        self.assertEqual(self.order.phone, '01700000000')
        self.assertEqual(self.order.address, '4 Ambarkhana Road')
        self.assertEqual(self.order.thana, 'Ambarkhana')
        self.assertEqual(self.order.zilla, 'Sylhet')
        self.assertEqual(self.order.delivery_area, 'outside_dhaka')
        self.assertEqual(self.order.status, 'processing')
        self.assertEqual(self.order.subtotal, Decimal('3600.00'))
        self.assertEqual(self.order.shipping_charge, Decimal('120.00'))
        self.assertEqual(self.order.total, Decimal('3720.00'))
        order_item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order_item.quantity, 3)
        self.assertEqual(order_item.line_total, Decimal('3600.00'))
        self.assertEqual(self.product.stock_quantity, 9)

        row = self.client.get(reverse('staff_dashboard')).content.decode()
        self.assertIn('value="4 Ambarkhana Road"', row)
        self.assertNotIn('value="4 Ambarkhana Road, Sylhet"', row)
        self.assertIn(f'name="item_free_shipping_{order_item.pk}"', row)

    def test_staff_can_change_order_item_product_and_recalculate_price_and_stock(self):
        operator = get_user_model().objects.create_user(
            username='product-editor',
            password='editor-password',
            is_staff=True,
        )
        order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=2,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('2400.00'),
        )
        replacement = create_test_product(
            self.category,
            'Phone Y',
            'phone-y',
            Decimal('800.00'),
        )
        replacement.stock.quantity = replacement.stock.opening_quantity = 5
        replacement.stock.save(update_fields=['quantity', 'opening_quantity'])
        replacement.variants.update(quantity=5, opening_quantity=5)
        replacement_size = Size.objects.create(name='Medium')
        replacement_color = Color.objects.create(name='Blue')
        configure_test_product_variants(
            replacement,
            [(replacement_size.name, replacement_color.name, 5)],
        )
        self.client.force_login(operator)

        dashboard = self.client.get(reverse('staff_dashboard'))
        self.assertContains(
            dashboard,
            f'name="item_product_{order_item.pk}"',
        )
        self.assertContains(dashboard, 'class="staff-size-option" hidden')
        self.assertContains(dashboard, 'class="staff-color-option" hidden')
        detail = self.client.get(reverse('staff_order_detail', args=[self.order.pk]))
        self.assertContains(
            detail,
            f'name="item_product_{order_item.pk}"',
        )

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'address': self.order.address,
                'thana': self.order.thana,
                'zilla': self.order.zilla,
                'delivery_area': self.order.delivery_area,
                'status': self.order.status,
                'page': '1',
                f'item_product_{order_item.pk}': str(replacement.pk),
                f'item_quantity_{order_item.pk}': '3',
                f'item_free_shipping_{order_item.pk}': 'false',
            },
        )

        self.assertRedirects(response, f'{reverse("staff_dashboard")}?page=1')
        order_item.refresh_from_db()
        self.product.refresh_from_db()
        replacement.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(order_item.product, replacement)
        self.assertEqual(order_item.product_name, replacement.name)
        self.assertEqual(order_item.unit_price, Decimal('800.00'))
        self.assertEqual(order_item.quantity, 3)
        self.assertEqual(order_item.line_total, Decimal('2400.00'))
        self.assertEqual(order_item.selected_size, replacement_size.name)
        self.assertEqual(order_item.selected_color, replacement_color.name)
        self.assertEqual(self.product.stock_quantity, 12)
        self.assertEqual(replacement.stock_quantity, 2)
        self.assertEqual(self.order.subtotal, Decimal('2400.00'))
        self.assertEqual(self.order.total, Decimal('2460.00'))

    def test_staff_order_updates_use_selected_variant_price_and_inventory(self):
        configure_test_product_variants(
            self.product,
            [
                ('L', 'Blue', 0),
                ('L', 'Red', 0),
                ('XL', 'Blue', 0),
                ('XL', 'Red', 0),
            ],
        )
        variants = {
            (variant.size, variant.color): variant
            for variant in ProductVariant.objects.filter(product=self.product)
        }
        old_variant = variants[('L', 'Blue')]
        new_variant = variants[('XL', 'Red')]
        old_variant.quantity = 1
        old_variant.opening_quantity = 3
        old_variant.save(update_fields=['quantity', 'opening_quantity'])
        new_variant.quantity = 4
        new_variant.opening_quantity = 4
        new_variant.price = Decimal('1800.00')
        new_variant.save(update_fields=['quantity', 'opening_quantity', 'price'])
        variants[('L', 'Red')].quantity = 3
        variants[('L', 'Red')].opening_quantity = 3
        variants[('L', 'Red')].save(update_fields=['quantity', 'opening_quantity'])
        variants[('XL', 'Blue')].quantity = 2
        variants[('XL', 'Blue')].opening_quantity = 2
        variants[('XL', 'Blue')].save(update_fields=['quantity', 'opening_quantity'])
        sync_product_stock(self.product.pk)
        order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            variant=old_variant,
            product_name=self.product.name,
            quantity=2,
            unit_price=old_variant.price,
            line_total=old_variant.price * 2,
            selected_size='L',
            selected_color='Blue',
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'address': self.order.address,
                'thana': self.order.thana,
                'zilla': self.order.zilla,
                'delivery_area': self.order.delivery_area,
                'status': 'processing',
                f'item_quantity_{order_item.pk}': '3',
                f'item_size_{order_item.pk}': 'XL',
                f'item_color_{order_item.pk}': 'Red',
                f'item_free_shipping_{order_item.pk}': 'false',
            },
        )

        self.assertEqual(response.status_code, 302)
        order_item.refresh_from_db()
        old_variant.refresh_from_db()
        new_variant.refresh_from_db()
        self.product.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(order_item.variant, new_variant)
        self.assertEqual(order_item.unit_price, Decimal('1800.00'))
        self.assertEqual(order_item.line_total, Decimal('5400.00'))
        self.assertEqual(old_variant.quantity, 3)
        self.assertEqual(new_variant.quantity, 1)
        self.assertEqual(self.product.stock_quantity, 9)
        self.assertEqual(self.order.subtotal, Decimal('5400.00'))

    def test_staff_can_toggle_product_free_shipping_from_order_row(self):
        operator = get_user_model().objects.create_user(
            username='shipping-editor',
            password='editor-password',
            is_staff=True,
        )
        order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=2,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('2400.00'),
        )
        self.client.force_login(operator)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'address': self.order.address,
                'thana': self.order.thana,
                'zilla': self.order.zilla,
                'delivery_area': 'outside_dhaka',
                'status': self.order.status,
                'page': '1',
                f'item_quantity_{order_item.pk}': '2',
                f'item_free_shipping_{order_item.pk}': 'true',
            },
        )

        self.assertRedirects(response, f'{reverse("staff_dashboard")}?page=1')
        self.product.refresh_from_db()
        self.order.refresh_from_db()
        self.assertTrue(self.product.free_shipping)
        self.assertEqual(self.order.shipping_charge, Decimal('0.00'))
        self.assertEqual(self.order.total, Decimal('2400.00'))

    def test_staff_quantity_edit_rejects_insufficient_stock_without_changes(self):
        operator = get_user_model().objects.create_user(
            username='stock-editor',
            password='editor-password',
            is_staff=True,
        )
        order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=2,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('2400.00'),
        )
        self.client.force_login(operator)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {
                'full_name': self.order.full_name,
                'phone': self.order.phone,
                'address': self.order.address,
                'thana': self.order.thana,
                'zilla': self.order.zilla,
                'delivery_area': self.order.delivery_area,
                'status': self.order.status,
                'page': '1',
                f'item_quantity_{order_item.pk}': '13',
            },
            follow=True,
        )

        self.assertContains(response, 'not enough stock')
        order_item.refresh_from_db()
        self.product.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(order_item.quantity, 2)
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(self.order.total, Decimal('1260.00'))

    def test_returning_order_restores_stock_and_leaving_returned_reduces_it_once(self):
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=2,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('2400.00'),
        )
        self.product.stock.quantity = 8
        self.product.stock.save(update_fields=['quantity'])
        self.product.variants.update(quantity=8)
        sync_product_stock(self.product.pk)
        self.client.force_login(self.staff_user)

        self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'returned'},
        )
        self.product.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(self.order.status, 'returned')

        self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'returned'},
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)

        self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'processing'},
        )
        self.product.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 8)
        self.assertEqual(self.order.status, 'processing')

    def test_status_changes_restore_and_reserve_the_ordered_variant_stock(self):
        variant = ProductVariant.objects.get(
            product=self.product,
            size='',
            color='',
        )
        variant.opening_quantity = 5
        variant.quantity = 3
        variant.save(update_fields=['opening_quantity', 'quantity'])
        sync_product_stock(self.product.pk)
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            variant=variant,
            product_name=self.product.name,
            quantity=2,
            unit_price=variant.price,
            line_total=variant.price * 2,
        )
        self.client.force_login(self.staff_user)

        self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'cancelled'},
        )

        variant.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(variant.quantity, 5)
        self.assertEqual(self.product.stock_quantity, 5)

        self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'processing'},
        )

        variant.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(variant.quantity, 3)
        self.assertEqual(self.product.stock_quantity, 3)

    def test_staff_stock_table_reports_opening_and_status_quantities(self):
        shipped_order = Order.objects.create(
            full_name='Shipped buyer',
            phone='01700000001',
            address='Dhaka',
            status='shipped',
        )
        delivered_order = Order.objects.create(
            full_name='Delivered buyer',
            phone='01700000002',
            address='Dhaka',
            status='delivered',
        )
        returned_order = Order.objects.create(
            full_name='Returned buyer',
            phone='01700000003',
            address='Dhaka',
            status='returned',
        )
        for related_order, quantity in (
            (shipped_order, 2),
            (delivered_order, 3),
            (returned_order, 1),
        ):
            OrderItem.objects.create(
                order=related_order,
                product=self.product,
                product_name=self.product.name,
                quantity=quantity,
                unit_price=Decimal('1200.00'),
                line_total=Decimal('1200.00') * quantity,
            )
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('staff_inventory'))

        self.assertContains(response, 'id="staff-stock-search"')
        self.assertContains(response, 'data-product-name="phone x"')
        self.assertContains(response, "row.dataset.productName.includes(query)")
        for heading in (
            'Product Name',
            'Restocked',
            'Shipped',
            'Delivered',
            'Returned',
            'Available',
            'Receive Stock',
        ):
            self.assertContains(response, f'<th>{heading}</th>', html=True)
        for field in (
            'name="size"',
            'name="color"',
            'name="restock_quantity"',
        ):
            self.assertContains(response, field)
        self.assertNotContains(response, '<th>Opening Stock</th>', html=True)
        self.assertNotContains(response, 'name="opening_quantity"')
        self.assertNotContains(response, 'name="price"')
        self.assertNotContains(response, 'name="compare_at_price"')
        product_row = next(
            product for product in response.context['stock_products']
            if product.pk == self.product.pk
        )
        self.assertEqual(product_row.initial_stock_quantity, 10)
        self.assertEqual(product_row.restocked_quantity, 0)
        self.assertEqual(product_row.shipped_quantity, 2)
        self.assertEqual(product_row.delivered_quantity, 3)
        self.assertEqual(product_row.returned_quantity, 1)
        self.assertEqual(product_row.stock_quantity, 10)

    def test_staff_inventory_restock_preserves_opening_stock_when_not_submitted(self):
        variant = self.product.variants.first()
        variant.quantity = 6
        variant.save(update_fields=['quantity'])
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('staff_update_product_variants', args=[self.product.pk]),
            {
                'variant_id': variant.pk,
                'size': variant.size,
                'color': variant.color,
                'restock_quantity': '2',
            },
        )

        self.assertEqual(response.status_code, 302)
        variant.refresh_from_db()
        self.assertEqual(variant.opening_quantity, 10)
        self.assertEqual(variant.quantity, 8)
        self.assertEqual(variant.restocked_quantity, 2)

    def test_staff_dashboard_links_to_paginated_inventory_page(self):
        self.client.force_login(self.staff_user)

        dashboard = self.client.get(reverse('staff_dashboard'))
        inventory = self.client.get(reverse('staff_inventory'))

        self.assertContains(
            dashboard,
            f'href="{reverse("staff_inventory")}"',
        )
        self.assertNotContains(dashboard, 'Recent stock receipts')
        self.assertEqual(inventory.status_code, 200)
        self.assertContains(inventory, 'Product stock quantities')

    def test_staff_inventory_reports_aggregate_restocked_stock(self):
        self.product.stock.restocked_quantity = 25
        self.product.stock.save(update_fields=['restocked_quantity'])
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('staff_inventory'))
        product_row = next(
            product for product in response.context['stock_products']
            if product.pk == self.product.pk
        )
        self.assertEqual(product_row.restocked_quantity, 25)
        self.assertContains(response, '25')

    def test_staff_restock_increases_available_stock_and_records_receipt(self):
        operator = get_user_model().objects.create_user(
            username='inventory-receiver',
            password='inventory-password',
            is_staff=True,
        )
        self.client.force_login(operator)

        response = self.client.post(
            reverse('staff_restock_product', args=[self.product.pk]),
            {'quantity': '7'},
        )

        self.assertRedirects(
            response,
            f'{reverse("staff_inventory")}#product-stock-{self.product.pk}',
            fetch_redirect_response=False,
        )
        self.product.refresh_from_db()
        stock = self.product.stock
        self.assertEqual(self.product.stock_quantity, 17)
        self.assertEqual(self.product.initial_stock_quantity, 10)
        self.assertEqual(stock.restocked_quantity, 7)

        dashboard = self.client.get(reverse('staff_inventory'))
        product_row = next(
            product for product in dashboard.context['stock_products']
            if product.pk == self.product.pk
        )
        self.assertEqual(product_row.restocked_quantity, 7)

    def test_deleting_product_preserves_order_history(self):
        item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=2,
            unit_price=self.product.starting_price,
            line_total=self.product.starting_price * 2,
        )
        self.order.status = 'processing'
        self.order.save(update_fields=['status'])

        self.product.delete()

        item.refresh_from_db()
        self.assertIsNone(item.product)
        self.assertEqual(item.product_name, 'Phone X')
        self.assertFalse(Stock.objects.filter(product_id=self.product.pk).exists())

        self.client.force_login(self.staff_user)
        detail = self.client.get(reverse('staff_order_detail', args=[self.order.pk]))
        self.assertEqual(detail.status_code, 200)
        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'cancelled'},
        )
        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'cancelled')

    def test_deleting_category_deletes_products_and_preserves_order_history(self):
        item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=1,
            unit_price=self.product.starting_price,
            line_total=self.product.starting_price,
        )
        product_id = self.product.pk

        self.category.delete()

        self.assertFalse(Product.objects.filter(pk=product_id).exists())
        item.refresh_from_db()
        self.assertIsNone(item.product)
        self.assertEqual(item.product_name, 'Phone X')

    def test_staff_restock_rejects_invalid_quantities_without_receipt(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('staff_restock_product', args=[self.product.pk]),
            {'quantity': '0'},
            follow=True,
        )

        self.assertContains(response, 'restock quantity greater than zero')
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(self.product.stock.restocked_quantity, 0)

    def test_cancelling_order_restores_stock_and_reopening_reserves_it_again(self):
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=2,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('2400.00'),
        )
        self.product.stock.quantity = 8
        self.product.stock.save(update_fields=['quantity'])
        self.product.variants.update(quantity=8)
        sync_product_stock(self.product.pk)
        self.client.force_login(self.staff_user)
        update_url = reverse('update_order_status', args=[self.order.pk])

        self.client.post(update_url, {'status': 'cancelled'})
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)

        self.client.post(update_url, {'status': 'cancelled'})
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)

        self.client.post(update_url, {'status': 'pending'})
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 8)

    @override_settings(FACEBOOK_CONVERSIONS_API_ACCESS_TOKEN='test-access-token')
    @patch('shop.facebook.urllib.request.urlopen')
    def test_facebook_purchase_event_is_sent_on_confirmation_only_once(self, mock_urlopen):
        SiteSettings.objects.create(facebook_pixel_id='TEST_PIXEL')
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=2,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('2400.00'),
        )
        mock_response = mock_urlopen.return_value.__enter__.return_value
        mock_response.read.return_value = json.dumps({'events_received': 1}).encode('utf-8')
        self.client.force_login(self.staff_user)

        self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'shipped'},
        )
        mock_urlopen.assert_not_called()

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'confirmed'},
            follow=True,
        )

        self.assertContains(response, 'Facebook Purchase event sent')
        self.assertEqual(mock_urlopen.call_count, 1)
        api_request = mock_urlopen.call_args.args[0]
        payload = json.loads(api_request.data.decode('utf-8'))
        purchase_event = payload['data'][0]
        self.assertEqual(purchase_event['event_name'], 'Purchase')
        self.assertEqual(purchase_event['event_id'], f'order-{self.order.pk}')
        self.assertEqual(purchase_event['custom_data']['currency'], 'BDT')
        self.assertEqual(purchase_event['custom_data']['value'], 1260.0)
        self.assertEqual(purchase_event['custom_data']['contents'][0]['quantity'], 2)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'delivered'},
            follow=True,
        )
        self.assertNotContains(response, 'Facebook Purchase event sent')
        self.assertEqual(mock_urlopen.call_count, 1)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.facebook_purchase_event_sent_at)

    def test_missing_facebook_conversions_token_is_reported_and_confirmation_can_be_retried(self):
        SiteSettings.objects.create(facebook_pixel_id='TEST_PIXEL')
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('update_order_status', args=[self.order.pk]),
            {'status': 'confirmed'},
            follow=True,
        )

        self.assertContains(response, 'Conversions API access token')
        self.order.refresh_from_db()
        self.assertIsNone(self.order.facebook_purchase_event_sent_at)

        with override_settings(FACEBOOK_CONVERSIONS_API_ACCESS_TOKEN='test-access-token'):
            with patch('shop.facebook.urllib.request.urlopen') as mock_urlopen:
                mock_urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
                    {'events_received': 1}
                ).encode('utf-8')
                response = self.client.post(
                    reverse('update_order_status', args=[self.order.pk]),
                    {'status': 'confirmed'},
                    follow=True,
                )

        self.assertContains(response, 'Facebook Purchase event sent')
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.facebook_purchase_event_sent_at)

    @override_settings(FACEBOOK_CONVERSIONS_API_ACCESS_TOKEN='test-access-token')
    @patch('shop.facebook.urllib.request.urlopen')
    def test_site_settings_admin_sends_meta_test_purchase_event(self, mock_urlopen):
        stored_token = 'stored-test-secret-token'
        site_settings = SiteSettings.objects.create(
            facebook_pixel_id='TEST_PIXEL',
            facebook_conversions_api_access_token=stored_token,
        )
        mock_response = mock_urlopen.return_value.__enter__.return_value
        mock_response.read.return_value = json.dumps({'events_received': 1}).encode('utf-8')
        self.client.force_login(self.staff_user)
        change_url = reverse('admin:shop_sitesettings_change', args=[site_settings.pk])
        form_response = self.client.get(change_url)

        self.assertEqual(form_response.status_code, 200)
        self.assertContains(form_response, 'Facebook test event code')
        self.assertContains(form_response, 'type="text"')
        self.assertContains(form_response, stored_token)
        self.assertNotContains(
            form_response,
            'type="password" name="facebook_conversions_api_access_token"',
        )
        self.assertContains(form_response, 'Send Meta test Purchase event')
        form = form_response.context['adminform'].form
        form_data = {
            name: str(form.initial.get(name) or '')
            for name in form.fields
            if name != 'logo'
        }
        form_data['facebook_conversions_api_access_token'] = ''
        form_data['facebook_test_event_code'] = 'TEST123'
        form_data['_send_facebook_test_event'] = 'Send Meta test Purchase event'

        response = self.client.post(change_url, form_data, follow=True)

        self.assertContains(response, 'Meta accepted the test Purchase event')
        self.assertEqual(mock_urlopen.call_count, 1)
        api_request = mock_urlopen.call_args.args[0]
        payload = json.loads(api_request.data.decode('utf-8'))
        self.assertEqual(parse_qs(urlsplit(api_request.full_url).query)['access_token'], [stored_token])
        self.assertEqual(payload['test_event_code'], 'TEST123')
        self.assertEqual(payload['data'][0]['event_name'], 'Purchase')
        self.assertEqual(payload['data'][0]['custom_data']['value'], 1.0)
        site_settings.refresh_from_db()
        self.assertEqual(site_settings.facebook_conversions_api_access_token, stored_token)
        self.assertContains(response, stored_token)
        self.assertContains(response, 'id_facebook_conversions_api_access_token')
        self.assertNotContains(response, 'type="password" name="facebook_conversions_api_access_token"')
        self.assertFalse(hasattr(site_settings, 'facebook_test_event_code'))

    def test_buy_now_redirects_to_checkout(self):
        response = self.client.post(reverse('buy_now', args=[self.product.slug]))

        self.assertRedirects(response, reverse('checkout'))
        self.assertEqual(self.client.session.get('cart', {}).get(str(self.product.pk)), 1)

    def test_cart_url_redirects_to_checkout(self):
        session = self.client.session
        session['cart'] = {str(self.product.pk): 1}
        session.save()

        response = self.client.get(reverse('cart'))

        self.assertRedirects(response, reverse('checkout'))

    def test_checkout_quantity_update_returns_recalculated_totals(self):
        session = self.client.session
        session['cart'] = {str(self.product.pk): 1}
        session.save()

        response = self.client.post(
            reverse('update_cart', args=[self.product.slug]),
            {'quantity': '3', 'delivery_area': 'outside_dhaka'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['line_total'], '3600.00')
        self.assertEqual(response.json()['subtotal'], '3600.00')
        self.assertEqual(response.json()['shipping_charge'], '120')
        self.assertEqual(response.json()['total'], '3720.00')
        self.assertEqual(self.client.session['cart'][str(self.product.pk)], 3)

    def test_checkout_quantity_update_rejects_quantity_above_stock(self):
        response = self.client.post(
            reverse('update_cart', args=[self.product.slug]),
            {'quantity': '11'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.json())

    def test_checkout_only_accepts_cod_and_ignores_legacy_payment_details(self):
        session = self.client.session
        session['cart'] = {str(self.product.pk): 1}
        session.save()

        checkout_response = self.client.get(reverse('checkout'))
        self.assertContains(checkout_response, 'name="payment_method" value="cod"')
        self.assertContains(checkout_response, 'Payment method: <strong>Cash on Delivery (COD)</strong>', html=True)
        self.assertNotContains(checkout_response, 'bKash')
        self.assertNotContains(checkout_response, 'Nagad')
        self.assertContains(checkout_response, 'class="checkout-products"')
        self.assertContains(checkout_response, 'Phone X')
        self.assertContains(checkout_response, 'placeholder="আপনার নাম লিখুন"')
        self.assertContains(checkout_response, 'class="checkout-product-price">৳1200.00</span>')

        rejected_response = self.client.post(reverse('checkout'), {
            'full_name': 'Test Buyer',
            'phone': '01700000000',
            'address': 'Dhaka',
            'delivery_area': 'inside_dhaka',
            'payment_method': 'bKash',
            'payment_number': '01822222222',
            'transaction_reference': 'TXN123456',
            'payment_amount': '1260.00',
        })
        self.assertEqual(rejected_response.status_code, 200)
        self.assertIn('payment_method', rejected_response.context['form'].errors)
        self.assertEqual(Order.objects.count(), 1)

        response = self.client.post(reverse('checkout'), {
            'full_name': 'COD Buyer',
            'phone': '01700000000',
            'address': 'Dhaka',
            'delivery_area': 'inside_dhaka',
            'payment_method': 'cod',
            'payment_number': '01822222222',
            'transaction_reference': 'TXN123456',
            'payment_amount': '1260.00',
        })

        created_order = Order.objects.exclude(pk=self.order.pk).get(full_name='COD Buyer')
        self.assertRedirects(response, reverse('order_success', args=[created_order.pk]))
        self.assertEqual(created_order.payment_method, 'cod')
        self.assertEqual(created_order.payment_number, '')
        self.assertEqual(created_order.transaction_reference, '')
        self.assertIsNone(created_order.payment_amount)

    def test_nagad_checkout_submission_is_rejected(self):
        session = self.client.session
        session['cart'] = {str(self.product.pk): 1}
        session.save()

        response = self.client.post(reverse('checkout'), {
            'full_name': 'Test Buyer',
            'phone': '01700000000',
            'address': 'Dhaka',
            'delivery_area': 'inside_dhaka',
            'payment_method': 'nagad',
            'payment_number': '01822222222',
            'transaction_reference': 'TXN123456',
            'payment_amount': '1000.00',
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn('payment_method', response.context['form'].errors)
        self.assertEqual(Order.objects.count(), 1)


class SeoAndSitemapTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='Electronics', slug='electronics')
        self.product = create_test_product(
            self.category,
            'Smart Watch',
            'smart-watch',
            Decimal('3999.00'),
        )
        self.product.stock.quantity = self.product.stock.opening_quantity = 4
        self.product.stock.save(update_fields=['quantity', 'opening_quantity'])
        self.product.variants.update(quantity=4, opening_quantity=4)
        self.settings = SiteSettings.objects.create(
            brand_name='BDBazaar',
            meta_description='Bangladesh ecommerce with fast delivery.',
            og_title='BDBazaar Bangladesh',
            og_description='Shop gadgets and daily essentials.',
            canonical_url='',
            ga_measurement_id='G-TEST123',
            facebook_pixel_id='TEST_PIXEL',
        )

    def test_homepage_renders_seo_meta(self):
        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="description" content="Bangladesh ecommerce with fast delivery."')
        self.assertContains(response, "gtag('config', ")
        self.assertContains(response, "{send_page_view: false}")
        self.assertContains(response, "gtag('event', 'page_view'")
        self.assertContains(response, "fbq('track', 'PageView')")

    def test_customer_pages_load_analytics_and_shop_ajax_tracks_virtual_page_views(self):
        urls = (
            reverse('home'),
            reverse('shop'),
            reverse('categories'),
            reverse('product_detail', args=[self.product.slug]),
        )
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'googletagmanager.com/gtag/js?id=G-TEST123')
                self.assertContains(response, 'connect.facebook.net/en_US/fbevents.js')

        shop_response = self.client.get(reverse('shop'))
        self.assertContains(shop_response, 'function trackShopPageView()')
        self.assertContains(shop_response, "window.gtag('event', 'page_view'")
        self.assertContains(shop_response, "window.fbq('track', 'PageView')")

    def test_staff_dashboard_is_excluded_from_customer_analytics(self):
        staff_user = get_user_model().objects.create_user(
            username='analytics-staff',
            password='safe-test-password',
            is_staff=True,
        )
        self.client.force_login(staff_user)

        response = self.client.get(reverse('staff_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'googletagmanager.com/gtag/js')
        self.assertNotContains(response, 'connect.facebook.net/en_US/fbevents.js')

    def test_sitemap_includes_products_and_categories(self):
        response = self.client.get(reverse('sitemap'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('product_detail', args=[self.product.slug]))
        self.assertContains(response, reverse('categories'))
        self.assertNotContains(response, f"{reverse('shop')}?category={self.category.slug}")

    def test_product_short_description_is_used_for_search_and_social_metadata(self):
        self.product.short_description = 'Compact Bluetooth speaker with rich sound.'
        self.product.save(update_fields=['short_description'])

        response = self.client.get(reverse('product_detail', args=[self.product.slug]))

        self.assertContains(
            response,
            '<meta name="description" content="Compact Bluetooth speaker with rich sound.">',
        )
        self.assertContains(
            response,
            '<meta property="og:description" content="Compact Bluetooth speaker with rich sound.">',
        )
        self.assertContains(response, '<meta property="og:type" content="product">')
        self.assertContains(response, '<meta name="twitter:title" content="Smart Watch | BDBazaar">')
        self.assertContains(response, '<meta property="og:site_name" content="BDBazaar">')
        self.assertContains(response, '"priceCurrency": "BDT"')
        self.assertContains(response, 'https://schema.org/InStock')

    def test_robots_txt_points_to_sitemap_and_excludes_private_pages(self):
        response = self.client.get(reverse('robots_txt'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'Sitemap: http://testserver{reverse("sitemap")}')
        self.assertContains(response, 'Disallow: /admin/')

    def test_public_pages_get_self_canonical_without_placeholder_domain(self):
        response = self.client.get(reverse('home'))

        self.assertContains(response, '<link rel="canonical" href="http://testserver/">')
        self.assertNotContains(response, 'bdbazaar.example.com')

    def test_seo_settings_drive_homepage_metadata_and_search_visibility(self):
        self.settings.home_meta_title = 'BDBazaar | Shop Bangladesh'
        self.settings.home_meta_description = 'Discover everyday products and deals in Bangladesh.'
        self.settings.google_site_verification = 'verification-token'
        self.settings.twitter_handle = '@bdbazaar'
        self.settings.save()

        response = self.client.get(reverse('home'))

        self.assertContains(response, '<title>BDBazaar | Shop Bangladesh</title>')
        self.assertContains(
            response,
            '<meta name="description" content="Discover everyday products and deals in Bangladesh.">',
        )
        self.assertContains(response, '<meta name="google-site-verification" content="verification-token">')
        self.assertContains(response, '<meta name="twitter:site" content="@bdbazaar">')

        self.settings.search_engine_indexing = False
        self.settings.save(update_fields=['search_engine_indexing'])
        response = self.client.get(reverse('home'))
        self.assertContains(response, '<meta name="robots" content="noindex,follow">')
        self.assertContains(self.client.get(reverse('robots_txt')), 'Disallow: /')

    def test_page_overrides_control_category_and_shop_metadata(self):
        self.settings.categories_meta_title = 'Browse collections'
        self.settings.categories_meta_description = 'Find products by collection.'
        self.settings.shop_meta_title = 'All products'
        self.settings.shop_meta_description = 'Browse every available product.'
        self.settings.save()

        categories_response = self.client.get(reverse('categories'))
        shop_response = self.client.get(reverse('shop'))
        filtered_shop_response = self.client.get(reverse('shop'), {'search': 'watch'})

        self.assertContains(categories_response, '<title>Browse collections</title>')
        self.assertContains(
            categories_response,
            '<meta name="description" content="Find products by collection.">',
        )
        self.assertContains(shop_response, '<title>All products</title>')
        self.assertContains(shop_response, '<meta name="robots" content="index,follow">')
        self.assertContains(filtered_shop_response, '<meta name="robots" content="noindex,follow">')

    def test_canonical_domain_is_used_for_page_metadata_and_sitemap(self):
        self.settings.canonical_url = 'https://store.example.org/'
        self.settings.save(update_fields=['canonical_url'])

        response = self.client.get(reverse('home'))
        sitemap_response = self.client.get(reverse('sitemap'))

        self.assertContains(response, '<link rel="canonical" href="https://store.example.org/">')
        self.assertContains(sitemap_response, 'https://store.example.org/')

    def test_seo_fields_are_editable_in_site_settings_admin(self):
        staff = get_user_model().objects.create_superuser(
            username='seo-admin',
            email='seo-admin@example.com',
            password='secure-test-password',
        )
        self.client.force_login(staff)

        response = self.client.get(reverse('admin:shop_sitesettings_change', args=[self.settings.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Aim for 50–60 characters')
        self.assertContains(response, 'Aim for 150–160 characters')
        self.assertContains(response, 'Social sharing title. Aim for about 50–60 characters.')
        self.assertContains(response, 'Character counts include spaces.')
        for field in (
            'meta_title',
            'meta_description',
            'home_meta_title',
            'home_meta_description',
            'categories_meta_title',
            'categories_meta_description',
            'shop_meta_title',
            'shop_meta_description',
            'og_title',
            'og_description',
            'og_image',
            'canonical_url',
            'google_site_verification',
            'twitter_handle',
            'search_engine_indexing',
        ):
            self.assertContains(response, f'id_{field}')


class PathaoShipmentTests(TestCase):
    def setUp(self):
        self.staff_user = get_user_model().objects.create_user(
            username='pathao-staff',
            password='secure-test-password',
            is_staff=True,
        )
        self.category = Category.objects.create(name='Pathao category', slug='pathao-category')
        self.product = create_test_product(
            self.category,
            'Pathao product',
            'pathao-product',
            Decimal('1200.00'),
        )
        self.order = Order.objects.create(
            full_name='Pathao Buyer',
            phone='01700000000',
            address='Road 1, Sector 4',
            thana='Uttara',
            zilla='Dhaka',
            subtotal=Decimal('1200.00'),
            shipping_charge=Decimal('60.00'),
            total=Decimal('1260.00'),
            note='Please call before delivery.',
            status='confirmed',
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            quantity=2,
            unit_price=Decimal('1200.00'),
            line_total=Decimal('2400.00'),
        )
        self.settings = SiteSettings.objects.create(
            pathao_environment='production',
            pathao_production_client_id='production-client-id',
            pathao_production_client_secret='production-client-secret',
            pathao_production_username='pathao-production@example.com',
            pathao_production_password='production-password',
            pathao_production_store_id=123,
        )

    def test_shipment_payload_uses_order_total_and_combined_item_quantity(self):
        self.order.subtotal = Decimal('2400.00')
        self.order.shipping_charge = Decimal('60.00')
        self.order.cash_discount = Decimal('20.00')
        self.order.total = Decimal('2440.00')
        self.order.payment_amount = Decimal('200.00')
        self.order.save(update_fields=[
            'subtotal',
            'shipping_charge',
            'cash_discount',
            'total',
            'payment_amount',
        ])
        with (
            patch('shop.pathao._get_token', return_value=('test-access-token', 200)),
            patch('shop.pathao._request', return_value=(
                {
                    'message': 'Order Created Successfully',
                    'type': 'success',
                    'code': 200,
                    'data': {
                        'consignment_id': 'C123',
                        'merchant_order_id': str(self.order.display_order_number),
                        'order_status': 'Pending',
                        'delivery_fee': 80,
                    },
                },
                201,
            )) as mock_request,
        ):
            result = create_pathao_order(self.settings, self.order)

        self.assertEqual(result, {
            'consignment_id': 'C123',
            'status': 'Pending',
            'merchant_order_id': str(self.order.display_order_number),
            'delivery_fee': Decimal('80'),
            'message': 'Order Created Successfully',
            'type': 'success',
            'code': 200,
            'login_status_code': 200,
            'order_status_code': 201,
        })
        payload = mock_request.call_args.kwargs['payload']
        self.assertEqual(payload['item_quantity'], 2)
        self.assertEqual(payload['amount_to_collect'], 2440)
        self.assertIsInstance(payload['amount_to_collect'], int)
        self.assertEqual(payload['item_weight'], '0.50')
        self.assertIsInstance(payload['item_weight'], str)
        self.assertEqual(payload['recipient_address'], 'Road 1, Sector 4, Uttara, Dhaka')
        self.assertEqual(
            set(payload),
            {
                'store_id',
                'merchant_order_id',
                'recipient_name',
                'recipient_phone',
                'recipient_address',
                'delivery_type',
                'item_type',
                'special_instruction',
                'item_quantity',
                'item_weight',
                'item_description',
                'amount_to_collect',
            },
        )
        self.assertNotIn('recipient_city', payload)
        self.assertNotIn('recipient_zone', payload)
        self.assertNotIn('recipient_area', payload)
        self.assertEqual(payload['item_description'], 'Pathao product x2 (৳2400.00)')
        self.assertEqual(payload['special_instruction'], 'Please call before delivery.')
        self.assertEqual(
            payload['merchant_order_id'],
            str(self.order.display_order_number),
        )

    def test_shipment_info_uses_consignment_id_and_returns_latest_status_and_fee(self):
        with (
            patch('shop.pathao._get_token', return_value='test-access-token'),
            patch(
                'shop.pathao._request',
                return_value={
                    'data': {
                        'consignment_id': 'C123',
                        'order_status': 'Picked',
                        'delivery_fee': 85,
                    },
                },
            ) as mock_request,
        ):
            info = get_pathao_order_info(self.settings, 'C123')

        self.assertEqual(
            info,
            {
                'order_status': 'Picked',
                'delivery_fee': Decimal('85'),
            },
        )
        self.assertEqual(mock_request.call_args.args[1], 'orders/C123/info')
        self.assertEqual(mock_request.call_args.kwargs['token'], 'test-access-token')

    def test_shipment_info_uses_documented_post_with_empty_form_body(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"data":{"order_status":"Pending"}}'

        with (
            patch('shop.pathao._get_token', return_value='test-access-token'),
            patch(
                'shop.pathao.urllib.request.urlopen',
                return_value=FakeResponse(),
            ) as urlopen,
        ):
            get_pathao_order_info(self.settings, 'C123')

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.data, b'')
        self.assertEqual(
            request.get_header('Content-type'),
            'application/x-www-form-urlencoded',
        )
        self.assertEqual(
            request.full_url,
            'https://api-hermes.pathao.com/aladdin/api/v1/orders/C123/info',
        )
        self.assertEqual(
            request.get_header('Authorization'),
            'Bearer test-access-token',
        )

    def test_manual_sync_updates_pathao_shipment_status_without_changing_order_status(self):
        shipment = PathaoShipment.objects.create(
            order=self.order,
            environment='production',
            consignment_id='C123',
            merchant_order_id=str(self.order.display_order_number),
            order_status='Pending',
        )
        run = PathaoStatusSyncRun.objects.create()

        with patch(
            'shop.management.commands.sync_pathao_shipment_statuses.get_order_info',
            return_value={
                'order_status': 'Delivered',
                'delivery_fee': Decimal('85'),
            },
        ) as get_info:
            call_command('sync_pathao_shipment_statuses', '--run-id', str(run.pk))

        shipment.refresh_from_db()
        run.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(shipment.order_status, 'Delivered')
        self.assertEqual(shipment.delivery_fee, Decimal('85'))
        self.assertIsNotNone(shipment.status_checked_at)
        self.assertEqual(self.order.status, 'confirmed')
        self.assertEqual(self.order.pathao_order_status, 'Delivered')
        self.assertEqual(run.status, 'completed')
        get_info.assert_called_once_with(self.settings, 'C123')

    def test_manual_sync_only_fetches_pending_pathao_shipments(self):
        PathaoShipment.objects.create(
            order=self.order,
            environment='production',
            consignment_id='PENDING123',
            merchant_order_id=str(self.order.display_order_number),
            order_status='Pending',
        )
        delivered_order = Order.objects.create(
            full_name='Delivered Pathao Buyer',
            phone='01700000002',
            address='Road 3',
        )
        PathaoShipment.objects.create(
            order=delivered_order,
            environment='production',
            consignment_id='DELIVERED123',
            merchant_order_id=str(delivered_order.display_order_number),
            order_status='Delivered',
        )
        run = PathaoStatusSyncRun.objects.create()

        with patch(
            'shop.management.commands.sync_pathao_shipment_statuses.get_order_info',
            return_value={'order_status': 'In Transit'},
        ) as get_info:
            call_command('sync_pathao_shipment_statuses', '--run-id', str(run.pk))

        get_info.assert_called_once_with(self.settings, 'PENDING123')

    def test_manual_sync_uses_current_environment_for_legacy_shipments(self):
        shipment = PathaoShipment.objects.create(
            order=self.order,
            consignment_id='LEGACY123',
            merchant_order_id=str(self.order.display_order_number),
            order_status='Pending',
        )
        run = PathaoStatusSyncRun.objects.create()

        with patch(
            'shop.management.commands.sync_pathao_shipment_statuses.get_order_info',
            return_value={'order_status': 'In Transit'},
        ) as get_info:
            call_command('sync_pathao_shipment_statuses', '--run-id', str(run.pk))

        shipment.refresh_from_db()
        self.assertEqual(shipment.order_status, 'In Transit')
        get_info.assert_called_once_with(self.settings, 'LEGACY123')

    def test_status_sync_command_requires_a_manual_run_id(self):
        with self.assertRaisesMessage(
            CommandError,
            'Error: the following arguments are required: --run-id',
        ):
            call_command('sync_pathao_shipment_statuses')

    def test_immediate_background_sync_checks_shipments_sequentially(self):
        first_shipment = PathaoShipment.objects.create(
            order=self.order,
            environment='production',
            consignment_id='FIRST123',
            merchant_order_id=str(self.order.display_order_number),
            order_status='Pending',
        )
        second_order = Order.objects.create(
            full_name='Second Pathao Buyer',
            phone='01700000001',
            address='Road 2',
        )
        second_shipment = PathaoShipment.objects.create(
            order=second_order,
            environment='production',
            consignment_id='SECOND123',
            merchant_order_id=str(second_order.display_order_number),
            order_status='Pending',
            status_checked_at=timezone.now(),
        )
        delivered_order = Order.objects.create(
            full_name='Already Delivered Buyer',
            phone='01700000003',
            address='Road 4',
        )
        delivered_shipment = PathaoShipment.objects.create(
            order=delivered_order,
            environment='production',
            consignment_id='DELIVERED123',
            merchant_order_id=str(delivered_order.display_order_number),
            order_status='Delivered',
        )
        run = PathaoStatusSyncRun.objects.create()
        requested_ids = []

        def fetch_info(settings, consignment_id):
            requested_ids.append(consignment_id)
            return {'order_status': 'In Transit'}

        with (
            patch(
                'shop.management.commands.sync_pathao_shipment_statuses.get_order_info',
                side_effect=fetch_info,
            ),
            patch(
                'shop.management.commands.sync_pathao_shipment_statuses.time_module.sleep',
            ) as pause,
        ):
            call_command('sync_pathao_shipment_statuses', '--run-id', str(run.pk))

        run.refresh_from_db()
        first_shipment.refresh_from_db()
        second_shipment.refresh_from_db()
        self.assertEqual(requested_ids, ['SECOND123', 'FIRST123'])
        pause.assert_called_once_with(5)
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.total_shipments, 2)
        self.assertEqual(run.checked_shipments, 2)
        self.assertEqual(first_shipment.order_status, 'In Transit')
        self.assertEqual(second_shipment.order_status, 'In Transit')
        delivered_shipment.refresh_from_db()
        self.assertEqual(delivered_shipment.order_status, 'Delivered')
        self.assertIsNone(delivered_shipment.status_checked_at)

    def test_staff_dashboard_starts_one_background_status_sync(self):
        PathaoShipment.objects.create(
            order=self.order,
            environment='production',
            consignment_id='C123',
            merchant_order_id=str(self.order.display_order_number),
            order_status='Pending',
        )
        self.client.force_login(self.staff_user)
        dashboard = self.client.get(reverse('staff_dashboard'))
        self.assertContains(dashboard, 'Sync Pathao statuses now')

        with patch('shop.views.subprocess.Popen') as popen:
            response = self.client.post(
                reverse('staff_start_pathao_status_sync'),
                follow=True,
            )

        self.assertRedirects(response, reverse('staff_dashboard'))
        self.assertContains(response, 'started in the background')
        self.assertContains(response, 'Shipments are checked one at a time')
        run = PathaoStatusSyncRun.objects.get()
        self.assertEqual(run.status, 'queued')
        self.assertEqual(run.requested_by, self.staff_user)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command[-3:], [
            'sync_pathao_shipment_statuses',
            '--run-id',
            str(run.pk),
        ])
        self.assertTrue(popen.call_args.kwargs['start_new_session'])

    def test_staff_dashboard_shows_sync_button_when_no_pending_shipments(self):
        self.client.force_login(self.staff_user)

        dashboard = self.client.get(reverse('staff_dashboard'))

        self.assertContains(dashboard, 'Sync Pathao statuses now')
        self.assertContains(dashboard, 'No pending Pathao shipments to sync right now.')
        self.assertRegex(
            dashboard.content.decode(),
            r'<button(?=[^>]*class="btn btn-primary")(?=[^>]*type="submit")(?![^>]*disabled)[^>]*>\s*Sync Pathao statuses now',
        )

        response = self.client.post(
            reverse('staff_start_pathao_status_sync'),
            follow=True,
        )

        self.assertContains(response, 'There are no Pathao shipments to sync.')
        self.assertFalse(PathaoStatusSyncRun.objects.exists())

    def test_staff_dashboard_prevents_duplicate_background_status_sync(self):
        PathaoShipment.objects.create(
            order=self.order,
            environment='production',
            consignment_id='C123',
            merchant_order_id=str(self.order.display_order_number),
            order_status='Pending',
        )
        PathaoStatusSyncRun.objects.create(
            status='queued',
            requested_by=self.staff_user,
        )
        self.client.force_login(self.staff_user)

        with patch('shop.views.subprocess.Popen') as popen:
            response = self.client.post(
                reverse('staff_start_pathao_status_sync'),
                follow=True,
            )

        self.assertContains(response, 'already queued')
        self.assertEqual(PathaoStatusSyncRun.objects.count(), 1)
        popen.assert_not_called()

    def test_non_staff_cannot_start_background_status_sync(self):
        response = self.client.post(reverse('staff_start_pathao_status_sync'))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(PathaoStatusSyncRun.objects.exists())

    def test_shipment_auto_selects_default_store_when_store_id_is_not_configured(self):
        self.settings.pathao_production_store_id = None

        with (
            patch('shop.pathao._get_token', return_value=('production-access-token', 200)),
            patch('shop.pathao._request', side_effect=[
                {
                    'data': {
                        'data': [
                            {'store_id': '456', 'is_active': 1, 'is_default_store': True},
                            {'store_id': '789', 'is_active': 1, 'is_default_store': False},
                        ],
                    },
                },
                (
                    {'data': {'consignment_id': 'C789', 'order_status': 'Pending'}},
                    201,
                ),
            ]) as mock_request,
        ):
            result = create_pathao_order(self.settings, self.order)

        self.assertTrue(is_pathao_configured(self.settings))
        self.assertEqual(result['consignment_id'], 'C789')
        self.assertEqual(mock_request.call_args_list[0].args[1], 'stores')
        self.assertEqual(mock_request.call_args_list[1].args[1], 'orders')
        self.assertEqual(mock_request.call_args_list[1].kwargs['payload']['store_id'], 456)

    def test_shipment_requires_manual_store_selection_when_multiple_stores_have_no_default(self):
        self.settings.pathao_production_store_id = None

        with (
            patch('shop.pathao._get_token', return_value=('production-access-token', 200)),
            patch('shop.pathao._request', return_value={
                'data': {
                    'data': [
                        {'store_id': 456, 'is_active': 1, 'is_default_store': False},
                        {'store_id': 789, 'is_active': 1, 'is_default_store': False},
                    ],
                },
            }) as mock_request,
        ):
            with self.assertRaisesMessage(
                PathaoApiError,
                'Multiple active Pathao stores were found. Select the required Store ID in Site Settings.',
            ):
                create_pathao_order(self.settings, self.order)

        self.assertEqual(mock_request.call_args.args[1], 'stores')

    def test_shipment_rejects_fractional_cod_amounts(self):
        self.order.total = Decimal('1260.50')

        with self.assertRaisesMessage(
            PathaoApiError,
            'Pathao COD collection amount must be a whole amount in BDT.',
        ):
            create_pathao_order(self.settings, self.order)

    def test_shipment_rejects_weight_outside_pathao_limits(self):
        self.settings.pathao_item_weight = Decimal('10.01')

        with self.assertRaisesMessage(
            PathaoApiError,
            'Pathao parcel weight must be between 0.5 and 10 kg.',
        ):
            create_pathao_order(self.settings, self.order)

    def test_shipment_requires_street_thana_and_zilla(self):
        self.order.thana = ''

        with self.assertRaisesMessage(
            PathaoApiError,
            'Add the recipient street address, Thana / Upazila, and Zilla before creating a shipment.',
        ):
            create_pathao_order(self.settings, self.order)

    def test_staff_can_create_pathao_shipment_without_location_ids(self):
        self.settings.pathao_environment = 'sandbox'
        self.settings.save(update_fields=['pathao_environment'])
        self.client.force_login(self.staff_user)
        with (
            patch(
                'shop.views.create_pathao_order',
                return_value={'consignment_id': 'C123', 'status': 'Pending'},
            ) as create_shipment,
            patch('shop.views.get_pathao_order_info', return_value={}) as get_info,
        ):
            response = self.client.post(
                reverse('staff_create_pathao_shipment', args=[self.order.pk]),
                {'page': '4'},
            )

        self.assertRedirects(
            response,
            reverse('staff_pathao_order', args=[self.order.pk]),
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.pathao_consignment_id, 'C123')
        create_shipment.assert_called_once()
        self.assertEqual(
            create_shipment.call_args.args[0].pathao_environment,
            'production',
        )
        get_info.assert_called_once_with(self.settings, 'C123')

    def test_failed_shipment_is_saved_and_shown_on_pathao_page(self):
        self.client.force_login(self.staff_user)

        with patch(
            'shop.views.create_pathao_order',
            side_effect=PathaoApiError('Pathao rejected the recipient address.'),
        ):
            response = self.client.post(
                reverse('staff_create_pathao_shipment', args=[self.order.pk]),
                follow=True,
            )

        self.assertContains(response, 'Pathao shipment was not created')
        self.order.refresh_from_db()
        self.assertEqual(self.order.pathao_error, 'Pathao rejected the recipient address.')
        self.assertContains(response, 'Pathao rejected the recipient address.')
        self.assertGreater(
            response.content.index(b'Pathao shipment was not created'),
            response.content.index(b'staff-pathao-action-panel'),
        )

    def test_login_failure_shows_status_and_skips_order_creation(self):
        self.client.force_login(self.staff_user)

        with patch(
            'shop.views.create_pathao_order',
            side_effect=PathaoApiError(
                'Invalid Pathao client credentials.',
                stage='Login',
                status_code=401,
            ),
        ):
            response = self.client.post(
                reverse('staff_create_pathao_shipment', args=[self.order.pk]),
                follow=True,
            )

        self.assertContains(response, 'Login message: Failed (HTTP 401)')
        self.assertContains(
            response,
            'Order create message: Not attempted because Pathao login failed.',
        )

    def test_successful_shipment_clears_previous_failure(self):
        self.order.pathao_error = 'Previous production failure'
        self.order.save(update_fields=['pathao_error'])
        self.client.force_login(self.staff_user)

        with patch(
            'shop.views.create_pathao_order',
            return_value={
                'consignment_id': 'C123',
                'status': 'Pending',
                'merchant_order_id': str(self.order.display_order_number),
                'delivery_fee': Decimal('80'),
                'message': 'Order Created Successfully',
                'type': 'success',
                'code': 200,
                    'login_status_code': 200,
                    'order_status_code': 201,
            },
        ), patch(
            'shop.views.get_pathao_order_info',
            return_value={'order_status': 'Picked', 'delivery_fee': Decimal('85')},
        ) as get_info:
            response = self.client.post(
                reverse('staff_create_pathao_shipment', args=[self.order.pk]),
                follow=True,
            )

        self.assertContains(response, 'Pathao shipment created')
        self.assertContains(response, 'Login message: Access token accepted (HTTP 200).')
        self.assertContains(
            response,
            'Order create message: Pathao shipment created (HTTP 201).',
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.pathao_consignment_id, 'C123')
        shipment = self.order.pathao_shipment
        self.assertEqual(shipment.merchant_order_id, str(self.order.display_order_number))
        self.assertEqual(shipment.consignment_id, 'C123')
        self.assertEqual(shipment.order_status, 'Picked')
        self.assertEqual(shipment.delivery_fee, Decimal('85'))
        self.assertEqual(shipment.message, 'Order Created Successfully')
        self.assertEqual(shipment.response_type, 'success')
        self.assertEqual(shipment.response_code, 200)
        self.assertEqual(shipment.info_error, '')
        self.assertEqual(self.order.pathao_order_status, 'Picked')
        get_info.assert_called_once_with(self.settings, 'C123')
        self.assertEqual(self.order.pathao_error, '')
        self.assertTrue(
            response.content.index(b'Pathao shipment created')
            > response.content.index(b'staff-pathao-action-panel')
        )
        self.assertContains(
            self.client.get(reverse('staff_pathao_order', args=[self.order.pk])),
            '৳85.00',
        )

    def test_staff_cannot_submit_a_second_shipment_for_the_same_order(self):
        self.order.pathao_consignment_id = 'EXISTING'
        self.order.save(update_fields=['pathao_consignment_id'])
        self.client.force_login(self.staff_user)

        with patch('shop.views.create_pathao_order') as create_shipment:
            response = self.client.post(
                reverse('staff_create_pathao_shipment', args=[self.order.pk]),
                follow=True,
            )

        self.assertContains(response, 'Order create message: Shipment already exists: EXISTING.')
        create_shipment.assert_not_called()

    def test_order_detail_links_confirmed_orders_to_pathao_page(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('staff_order_detail', args=[self.order.pk]))

        self.assertContains(response, 'Review Pathao order details')
        self.assertContains(response, reverse('staff_pathao_order', args=[self.order.pk]))
        self.assertContains(response, 'staff-pathao-review-link')
        self.assertLess(response.content.index(b'Customer and payment'), response.content.index(b'Review Pathao order details'))
        self.assertLess(response.content.index(b'Back to staff dashboard'), response.content.index(b'Customer and payment'))
        self.assertNotContains(response, 'staff-section-label">Courier')
        self.assertNotContains(response, '<h2>Pathao shipment</h2>')
        self.assertNotContains(response, 'id="pathao-city"')

    def test_pathao_preview_shows_details_without_creating_shipment(self):
        self.client.force_login(self.staff_user)
        self.order.subtotal = Decimal('2400.00')
        self.order.shipping_charge = Decimal('60.00')
        self.order.cash_discount = Decimal('20.00')
        self.order.total = Decimal('2440.00')
        self.order.payment_amount = Decimal('200.00')
        self.order.fulfillment_note = 'Call on arrival.'
        self.order.save(update_fields=[
            'subtotal',
            'shipping_charge',
            'cash_discount',
            'total',
            'payment_amount',
            'fulfillment_note',
        ])

        with patch('shop.views.create_pathao_order') as create_shipment:
            response = self.client.get(reverse('staff_pathao_order', args=[self.order.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Review before creating')
        self.assertContains(response, self.order.full_name)
        self.assertContains(response, self.order.phone)
        self.assertContains(response, 'Road 1, Sector 4, Uttara, Dhaka')
        self.assertContains(response, '৳2400.00')
        self.assertContains(response, '৳60.00')
        self.assertContains(response, '− ৳20.00')
        self.assertContains(response, 'COD amount sent to Pathao')
        self.assertContains(response, '৳2440.00')
        self.assertContains(response, str(self.order.display_order_number))
        self.assertContains(response, 'Call on arrival.')
        self.assertContains(response, 'Pathao product x2')
        self.assertNotContains(response, 'Check customer')
        self.assertContains(response, 'Create Pathao order')
        self.assertNotContains(response, 'Pathao delivery history')
        create_shipment.assert_not_called()

    def test_info_lookup_failure_keeps_created_shipment_and_saves_error(self):
        self.client.force_login(self.staff_user)

        with (
            patch(
                'shop.views.create_pathao_order',
                return_value={'consignment_id': 'C123', 'status': 'Pending'},
            ),
            patch(
                'shop.views.get_pathao_order_info',
                side_effect=PathaoApiError('Pathao returned HTTP 503.'),
            ),
        ):
            response = self.client.post(
                reverse('staff_create_pathao_shipment', args=[self.order.pk]),
                follow=True,
            )

        self.order.refresh_from_db()
        shipment = self.order.pathao_shipment
        self.assertEqual(self.order.pathao_consignment_id, 'C123')
        self.assertEqual(shipment.info_error, 'Pathao returned HTTP 503.')
        self.assertContains(response, 'Pathao shipment created')
        self.assertContains(response, 'latest details could not be fetched')
        self.assertContains(response, 'Pathao returned HTTP 503.')
        self.assertContains(
            self.client.get(reverse('staff_pathao_order', args=[self.order.pk])),
            'Could not refresh Pathao shipment details',
        )

    def test_pathao_page_shows_all_successful_shipments(self):
        self.order.pathao_consignment_id = 'C123'
        self.order.pathao_order_status = 'Pending'
        self.order.save(update_fields=['pathao_consignment_id', 'pathao_order_status'])
        another_order = Order.objects.create(
            full_name='Another Sandbox Buyer',
            phone='01700000001',
            address='Test road',
            pathao_consignment_id='C456',
            pathao_order_status='Pending',
        )
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('staff_dashboard'))

        self.assertNotContains(response, 'Pathao shipment attempts')
        self.assertNotContains(response, 'Courier sandbox')

        pathao_page = self.client.get(reverse('staff_pathao_order', args=[self.order.pk]))
        self.assertContains(pathao_page, 'Shipment history')
        self.assertContains(pathao_page, 'Shipment created')
        self.assertContains(pathao_page, 'C123')
        self.assertContains(pathao_page, 'C456')
        self.assertContains(pathao_page, reverse('staff_pathao_order', args=[another_order.pk]))

    def test_pathao_create_button_is_limited_to_confirmed_orders(self):
        self.client.force_login(self.staff_user)
        dashboard = self.client.get(reverse('staff_dashboard'))
        self.assertContains(dashboard, 'Review Pathao')
        self.assertNotRegex(
            dashboard.content.decode(),
            r'class="[^"]*staff-pathao-order-btn[^"]*"[^>]*hidden',
        )

        self.order.status = 'pending'
        self.order.save(update_fields=['status'])
        dashboard = self.client.get(reverse('staff_dashboard'))
        self.assertContains(dashboard, 'staff-pathao-order-btn')
        self.assertRegex(
            dashboard.content.decode(),
            r'class="[^"]*staff-pathao-order-btn[^"]*"[^>]*hidden',
        )

    def test_pathao_shipment_creation_rejects_unconfirmed_order(self):
        self.order.status = 'pending'
        self.order.save(update_fields=['status'])
        self.client.force_login(self.staff_user)

        with patch('shop.views.create_pathao_order') as create_shipment:
            response = self.client.post(
                reverse('staff_create_pathao_shipment', args=[self.order.pk]),
                follow=True,
            )

        self.assertContains(response, 'Confirm this order in the staff dashboard')
        create_shipment.assert_not_called()

    def test_pathao_site_settings_default_to_production(self):
        settings = SiteSettings()

        self.assertEqual(settings.pathao_environment, 'production')
        self.assertEqual(
            settings.pathao_production_base_url,
            'https://api-hermes.pathao.com',
        )
        self.assertEqual(settings.pathao_grant_type, 'password')

    def test_pathao_token_request_uses_configured_grant_type(self):
        with (
            patch('shop.pathao.cache.get', return_value=None),
            patch('shop.pathao.cache.set') as cache_set,
            patch('shop.pathao._request', return_value=(
                {
                    'data': {
                        'access_token': 'production-access-token',
                        'expires_in': 3600,
                    },
                },
                200,
            )) as mock_request,
        ):
            token = get_pathao_token(self.settings)

        self.assertEqual(token, 'production-access-token')
        self.assertEqual(mock_request.call_args.args[1], 'issue-token')
        self.assertEqual(mock_request.call_args.kwargs['payload']['grant_type'], 'password')
        self.assertEqual(
            mock_request.call_args.kwargs['payload']['client_id'],
            'production-client-id',
        )
        cache_set.assert_called_once()
        self.assertEqual(
            cache_set.call_args.args[1],
            {'access_token': 'production-access-token', 'status_code': 200},
        )

    def test_production_environment_uses_its_own_credentials_and_store(self):
        self.settings.pathao_environment = 'production'
        self.settings.pathao_production_client_id = ''
        self.settings.pathao_production_client_secret = ''
        self.settings.pathao_production_username = 'production@example.com'
        self.settings.pathao_production_password = 'production-password'
        self.assertFalse(is_pathao_configured(self.settings))
        self.settings.pathao_production_client_id = 'production-client-id'
        self.settings.pathao_production_client_secret = 'production-client-secret'
        self.settings.pathao_production_store_id = 456

        with (
            patch(
                'shop.pathao._get_token',
                return_value=('production-access-token', 200),
            ),
            patch('shop.pathao._request', return_value=(
                {
                    'data': {
                        'consignment_id': 'PROD123',
                        'order_status': 'Pending',
                    },
                },
                201,
            )) as mock_request,
        ):
            result = create_pathao_order(self.settings, self.order)

        self.assertTrue(is_pathao_configured(self.settings))
        self.assertEqual(result['consignment_id'], 'PROD123')
        self.assertEqual(result['login_status_code'], 200)
        self.assertEqual(result['order_status_code'], 201)
        self.assertEqual(
            mock_request.call_args.kwargs['payload']['store_id'],
            456,
        )
        self.assertEqual(mock_request.call_args.args[0], self.settings)
        self.assertEqual(mock_request.call_args.args[1], 'orders')
        self.assertNotIn('recipient_city', mock_request.call_args.kwargs['payload'])
        self.assertNotIn('recipient_zone', mock_request.call_args.kwargs['payload'])
        self.assertEqual(
            get_pathao_base_url(self.settings),
            'https://api-hermes.pathao.com',
        )

    def test_production_order_request_targets_live_api_with_production_store_and_token(self):
        self.settings.pathao_environment = 'production'
        self.settings.pathao_production_client_id = 'production-client-id'
        self.settings.pathao_production_client_secret = 'production-client-secret'
        self.settings.pathao_production_username = 'production@example.com'
        self.settings.pathao_production_password = 'production-password'
        self.settings.pathao_production_store_id = 456

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"data":{"consignment_id":"LIVE123","order_status":"Pending"}}'

        with (
            patch(
                'shop.pathao._get_token',
                return_value=('production-access-token', 200),
            ),
            patch(
                'shop.pathao.urllib.request.urlopen',
                return_value=FakeResponse(),
            ) as urlopen,
        ):
            result = create_pathao_order(self.settings, self.order)

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode('utf-8'))
        self.assertEqual(result['consignment_id'], 'LIVE123')
        self.assertEqual(
            request.full_url,
            'https://api-hermes.pathao.com/aladdin/api/v1/orders',
        )
        self.assertEqual(
            request.get_header('Authorization'),
            'Bearer production-access-token',
        )
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(payload['store_id'], 456)
        self.assertEqual(payload['merchant_order_id'], str(self.order.display_order_number))

    def test_production_token_request_uses_documented_endpoint_and_json_fields(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"data":{"access_token":"live-token","expires_in":3600}}'

        with (
            patch('shop.pathao.cache.get', return_value=None),
            patch('shop.pathao.cache.set'),
            patch(
                'shop.pathao.urllib.request.urlopen',
                return_value=FakeResponse(),
            ) as urlopen,
        ):
            token = get_pathao_token(self.settings)

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode('utf-8'))
        self.assertEqual(token, 'live-token')
        self.assertEqual(
            request.full_url,
            'https://api-hermes.pathao.com/aladdin/api/v1/issue-token',
        )
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(payload, {
            'client_id': 'production-client-id',
            'client_secret': 'production-client-secret',
            'grant_type': 'password',
            'username': 'pathao-production@example.com',
            'password': 'production-password',
        })

    def test_pathao_order_page_uses_live_environment_even_if_sandbox_was_saved(self):
        self.settings.pathao_environment = 'sandbox'
        self.settings.pathao_production_client_id = 'production-client-id'
        self.settings.pathao_production_client_secret = 'production-client-secret'
        self.settings.pathao_production_username = 'production@example.com'
        self.settings.pathao_production_password = 'production-password'
        self.settings.save(update_fields=[
            'pathao_environment',
            'pathao_production_client_id',
            'pathao_production_client_secret',
            'pathao_production_username',
            'pathao_production_password',
        ])

        self.client.force_login(self.staff_user)
        response = self.client.get(
            reverse('staff_pathao_order', args=[self.order.pk]),
        )

        self.assertContains(response, 'Live environment')
        self.assertNotContains(response, 'Sandbox environment')
        self.assertTrue(response.context['pathao_configured'])

    def test_pathao_api_requests_send_bearer_token(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"data": {}}'

        with patch(
            'shop.pathao.urllib.request.urlopen',
            return_value=FakeResponse(),
        ) as urlopen:
            pathao_request(self.settings, 'orders', token='production-access-token')

        request = urlopen.call_args.args[0]
        ssl_context = urlopen.call_args.kwargs['context']
        self.assertEqual(
            request.get_header('Authorization'),
            'Bearer production-access-token',
        )
        self.assertEqual(
            request.full_url,
            'https://api-hermes.pathao.com/aladdin/api/v1/orders',
        )
        self.assertTrue(ssl_context.check_hostname)
        self.assertEqual(ssl_context.verify_mode, ssl.CERT_REQUIRED)

    def test_pathao_authentication_errors_are_reported(self):
        with self.assertRaisesMessage(
            PathaoApiError,
            'Invalid Pathao client credentials.',
        ):
            with patch(
                'shop.pathao.urllib.request.urlopen',
                side_effect=urllib.error.HTTPError(
                    'https://api-hermes.pathao.com/aladdin/api/v1/issue-token',
                    401,
                    'Unauthorized',
                    {},
                    BytesIO(b'{"message":"Invalid Pathao client credentials."}'),
                ),
            ):
                get_pathao_token(self.settings)

    def test_pathao_http_522_error_is_reported(self):
        with self.assertRaisesMessage(
            PathaoApiError,
            'Pathao returned HTTP 522.',
        ):
            with patch(
                'shop.pathao.urllib.request.urlopen',
                side_effect=urllib.error.HTTPError(
                    'https://api-hermes.pathao.com/aladdin/api/v1/orders',
                    522,
                    'Connection timed out',
                    {},
                    BytesIO(b''),
                ),
            ):
                pathao_request(self.settings, 'orders', method='POST', payload={})

    def test_pathao_connection_error_includes_transport_reason(self):
        with self.assertRaisesMessage(
            PathaoApiError,
            'Could not connect to Pathao: Temporary failure in name resolution.',
        ):
            with patch(
                'shop.pathao.urllib.request.urlopen',
                side_effect=urllib.error.URLError(
                    'Temporary failure in name resolution',
                ),
            ):
                pathao_request(self.settings, 'issue-token', method='POST', payload={})

    def test_pathao_secrets_are_visible_and_preserved_when_blank(self):
        self.settings.pathao_production_client_secret = 'production-client-secret'
        self.settings.pathao_production_password = 'production-password'
        form = SiteSettingsAdminForm(instance=self.settings)

        self.assertIn('production-client-secret', form.as_p())
        self.assertIn('production-password', form.as_p())
        self.assertFalse(any(field.startswith('pathao_sandbox') for field in form.fields))
        self.assertNotIn('pathao_environment', form.fields)
        self.assertEqual(
            form.fields['pathao_production_client_secret'].widget.input_type,
            'text',
        )
        self.assertEqual(
            form.fields['pathao_production_password'].widget.input_type,
            'text',
        )
        self.assertEqual(form.fields['pathao_grant_type'].initial, 'password')

        submitted_form = SiteSettingsAdminForm(
            instance=self.settings,
            data={
                'pathao_production_client_secret': '',
                'pathao_production_password': '',
            },
        )
        submitted_form.is_valid()

        self.assertEqual(
            submitted_form.cleaned_data['pathao_production_client_secret'],
            'production-client-secret',
        )
        self.assertEqual(
            submitted_form.cleaned_data['pathao_production_password'],
            'production-password',
        )

    def test_site_settings_admin_shows_only_production_pathao_setup(self):
        superuser = get_user_model().objects.create_superuser(
            username='pathao-admin',
            email='pathao-admin@example.com',
            password='secure-test-password',
        )
        self.client.force_login(superuser)

        response = self.client.get(
            reverse('admin:shop_sitesettings_change', args=[self.settings.pk]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'pathao-environment-settings.js')
        self.assertNotContains(response, 'id_pathao_environment')
        self.assertNotContains(response, 'id_pathao_sandbox_client_id')
        self.assertContains(response, 'id_pathao_production_client_id')

        self.settings.pathao_environment = 'sandbox'
        admin.site._registry[SiteSettings].save_model(
            None,
            self.settings,
            None,
            True,
        )
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.pathao_environment, 'production')
