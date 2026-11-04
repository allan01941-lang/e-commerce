from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models, transaction
from django_ckeditor_5.fields import CKEditor5Field


class Category(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to='categories/', blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Categories'

    def __str__(self):
        return self.name

class Size(models.Model):
    name = models.CharField(max_length=20, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

class Color(models.Model):
    name = models.CharField(max_length=50, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

class Product(models.Model):
    category = models.ForeignKey(Category, related_name='products', on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    sku = models.CharField(max_length=60, blank=True, default='')
    short_description = models.TextField(
        blank=True,
        help_text='Used as the product page search and social description. Aim for about 150–160 characters; write a clear, factual summary.',
    )
    long_description = CKEditor5Field(blank=True, config_name='default')
    free_shipping = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)    

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    @property
    def starting_variant(self):
        return min(
            self.variants.all(),
            key=lambda variant: (variant.price, variant.pk),
            default=None,
        )

    @property
    def starting_price(self):
        variant = self.starting_variant
        return variant.price if variant else None

    @property
    def starting_compare_at_price(self):
        variant = self.starting_variant
        return variant.compare_at_price if variant else None

    @property
    def discount_percentage(self):
        variant = self.starting_variant
        if variant is None:
            return None
        price = variant.price
        compare_at_price = variant.compare_at_price
        if (
            compare_at_price is None
            or compare_at_price <= price
            or compare_at_price <= 0
        ):
            return None
        percentage = (
            (compare_at_price - price) * Decimal('100') / compare_at_price
        ).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        return max(1, int(percentage))

    @property
    def variant_sizes(self):
        return sorted({
            variant.size
            for variant in self.variants.all()
            if variant.size
        })

    @property
    def variant_colors(self):
        return sorted({
            variant.color
            for variant in self.variants.all()
            if variant.color
        })

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new:
            Stock.objects.create(product=self)

    @property
    def stock_quantity(self):
        return self.stock.quantity if hasattr(self, 'stock') else 0

    @property
    def initial_stock_quantity(self):
        return self.stock.opening_quantity if hasattr(self, 'stock') else 0

    @property
    def in_stock(self):
        return self.stock_quantity > 0


class ProductVariant(models.Model):
    product = models.ForeignKey(
        Product,
        related_name='variants',
        on_delete=models.CASCADE,
    )
    size = models.CharField(max_length=20, blank=True, default='')
    color = models.CharField(max_length=50, blank=True, default='')
    price = models.DecimalField(max_digits=10, decimal_places=2)
    compare_at_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    quantity = models.PositiveIntegerField(default=0)
    opening_quantity = models.PositiveIntegerField(default=0)
    restocked_quantity = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['product__name', 'size', 'color']
        constraints = [
            models.UniqueConstraint(
                fields=['product', 'size', 'color'],
                name='unique_product_variant_options',
            ),
        ]

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        previous = None
        if self.pk:
            previous = (
                type(self).objects.filter(pk=self.pk)
                .values('quantity', 'opening_quantity')
                .first()
            )

        sync_current_quantity = False
        if previous is None:
            sync_current_quantity = self.quantity == 0 and self.opening_quantity > 0
        elif (
            self.opening_quantity != previous['opening_quantity']
            and previous['quantity'] == previous['opening_quantity']
            and self.quantity == previous['quantity']
        ):
            sync_current_quantity = True

        if sync_current_quantity:
            self.quantity = self.opening_quantity
            if update_fields is not None:
                kwargs['update_fields'] = set(update_fields) | {'quantity'}

        super().save(*args, **kwargs)

    def __str__(self):
        options = ', '.join(value for value in (self.size, self.color) if value)
        return f'{self.product.name} ({options or "Standard"})'


class Stock(models.Model):
    product = models.OneToOneField(
        Product,
        related_name='stock',
        on_delete=models.CASCADE,
    )
    quantity = models.PositiveIntegerField(
        default=0,
        verbose_name='Current available quantity',
        help_text='Live stock remaining now; this changes with orders, returns, and restocks.',
    )
    opening_quantity = models.PositiveIntegerField(
        default=0,
        verbose_name='Opening quantity',
        help_text='Starting stock count. When first entered, it also sets current available quantity.',
    )
    restocked_quantity = models.PositiveIntegerField(
        default=0,
        verbose_name='Total restocked',
        help_text='Cumulative units received after the opening stock count.',
    )

    class Meta:
        ordering = ['product__name']

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        previous = None
        if self.pk:
            previous = (
                type(self).objects.filter(pk=self.pk)
                .values('quantity', 'opening_quantity')
                .first()
            )

        sync_current_quantity = False
        if previous is None:
            sync_current_quantity = self.quantity == 0 and self.opening_quantity > 0
        elif (
            self.opening_quantity != previous['opening_quantity']
            and previous['quantity'] == previous['opening_quantity']
            and self.quantity == previous['quantity']
        ):
            sync_current_quantity = True

        if sync_current_quantity:
            self.quantity = self.opening_quantity
            if update_fields is not None:
                kwargs['update_fields'] = set(update_fields) | {'quantity'}

        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.product.name}: {self.quantity} in stock'


class ProductImage(models.Model):
    product = models.ForeignKey(Product, related_name='images', on_delete=models.CASCADE)
    image = models.ImageField(
        upload_to='products/gallery/',
        help_text=(
            'Recommended image size: 1200 × 1200 px (square, 1:1 aspect ratio). '
            'Use WebP format and keep each image under 100 KB for faster loading. '
            'Images are contained in square frames on desktop and mobile.'
        ),
    )
    caption = models.CharField(max_length=120, blank=True, default='')
    is_primary = models.BooleanField(default=False)
    ordering = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['ordering', 'created_at']

    def __str__(self):
        return f'{self.product.name} image'


class ProductHighlight(models.Model):
    ICON_CHOICES = [
        ('truck', 'Delivery'),
        ('cash', 'Money / payment'),
        ('check-mark', 'Check mark'),
        ('check-circle', 'Verified / quality'),
        ('shield-check', 'Secure / protected'),
        ('battery', 'Battery'),
        ('arrow-repeat', 'Returns'),
        ('headset', 'Customer support'),
        ('box', 'Product / packaging'),
        ('star', 'Featured'),
        ('heart', 'Customer favorite'),
        ('lightning', 'Fast / powerful'),
        ('leaf', 'Eco-friendly'),
        ('droplet', 'Water resistant'),
        ('tools', 'Durable / serviceable'),
    ]

    product = models.ForeignKey(
        Product,
        related_name='highlights',
        on_delete=models.CASCADE,
    )
    icon = models.CharField(
        max_length=16,
        choices=ICON_CHOICES,
        default='check-mark',
        help_text='Choose a built-in icon. It is displayed as an inline SVG.',
    )
    text = models.CharField(max_length=180)
    ordering = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['ordering', 'pk']

    def __str__(self):
        return f'{self.product.name}: {self.text}'




class Order(models.Model):
    PAYMENT_METHOD_CHOICES = [
        ('cod', 'Cash on Delivery'),
        ('bKash', 'bKash'),
        ('nagad', 'Nagad'),
    ]

    DELIVERY_AREA_CHOICES = [
        ('inside_dhaka', 'Inside Dhaka'),
        ('outside_dhaka', 'Outside Dhaka'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'),
        ('processing', 'Processing'),
        ('shipped', 'Shipped'),
        ('delivered', 'Delivered'),
        ('returned', 'Returned'),
        ('cancelled', 'Cancelled'),
    ]

    full_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True, null=True)
    address = models.TextField()
    city = models.CharField(max_length=80, blank=True, default='')
    thana = models.CharField(max_length=100, blank=True, default='')
    zilla = models.CharField(max_length=100, blank=True, default='')
    delivery_area = models.CharField(max_length=20, choices=DELIVERY_AREA_CHOICES, default='inside_dhaka')
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default='cod')
    payment_number = models.CharField(max_length=30, blank=True, default='')
    transaction_reference = models.CharField(max_length=100, blank=True, default='')
    payment_amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    note = models.TextField(blank=True)
    fulfillment_note = models.TextField(blank=True, default='')
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cash_discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    order_number = models.PositiveBigIntegerField(blank=True, null=True, unique=True, editable=False)
    pathao_city_id = models.PositiveIntegerField(blank=True, null=True)
    pathao_zone_id = models.PositiveIntegerField(blank=True, null=True)
    pathao_area_id = models.PositiveIntegerField(blank=True, null=True)
    pathao_consignment_id = models.CharField(max_length=100, blank=True, default='')
    pathao_order_status = models.CharField(max_length=80, blank=True, default='')
    pathao_error = models.TextField(blank=True, default='')
    pathao_customer_check_enabled = models.BooleanField(default=True)
    pathao_customer_history = models.JSONField(blank=True, default=dict)
    pathao_customer_history_checked_at = models.DateTimeField(blank=True, null=True)
    pathao_customer_history_error = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    delivered_at = models.DateTimeField(blank=True, null=True)
    facebook_purchase_event_sent_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def display_order_number(self):
        return self.order_number or self.pk

    def save(self, *args, **kwargs):
        if self._state.adding and self.order_number is None:
            using = kwargs.get('using') or self._state.db or 'default'
            with transaction.atomic(using=using):
                site_settings = (
                    SiteSettings.objects.using(using)
                    .select_for_update()
                    .order_by('pk')
                    .first()
                )
                last_order_number = (
                    type(self)._base_manager.using(using)
                    .aggregate(max_number=models.Max('order_number'))['max_number']
                )
                configured_next_number = (
                    site_settings.next_order_number if site_settings else 1001
                )
                self.order_number = max(
                    configured_next_number,
                    (last_order_number or configured_next_number - 1) + 1,
                )
                super().save(*args, **kwargs)
                if site_settings:
                    site_settings.next_order_number = self.order_number + 1
                    site_settings.save(
                        using=using,
                        update_fields=['next_order_number'],
                    )
            return
        super().save(*args, **kwargs)

    def __str__(self):
        return f'Order {self.display_order_number} - {self.full_name}'


class PathaoShipment(models.Model):
    environment = models.CharField(max_length=10, blank=True, default='')
    order = models.OneToOneField(
        Order,
        related_name='pathao_shipment',
        on_delete=models.CASCADE,
    )
    consignment_id = models.CharField(max_length=100)
    merchant_order_id = models.CharField(max_length=100, unique=True)
    order_status = models.CharField(max_length=80, blank=True, default='')
    delivery_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
    )
    message = models.CharField(max_length=400, blank=True, default='')
    response_type = models.CharField(max_length=30, blank=True, default='')
    response_code = models.PositiveIntegerField(blank=True, null=True)
    info_error = models.TextField(blank=True, default='')
    status_checked_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Pathao shipment {self.consignment_id} for order {self.merchant_order_id}'


class PathaoStatusSyncRun(models.Model):
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='queued')
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name='pathao_status_sync_runs',
    )
    total_shipments = models.PositiveIntegerField(default=0)
    checked_shipments = models.PositiveIntegerField(default=0)
    failed_shipments = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['status'],
                condition=models.Q(status__in=['queued', 'running']),
                name='one_active_pathao_status_sync_run',
            ),
        ]

    def __str__(self):
        return f'Pathao status sync {self.pk} ({self.status})'


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name='items', on_delete=models.CASCADE)
    product = models.ForeignKey(
        Product,
        related_name='order_items',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    variant = models.ForeignKey(
        ProductVariant,
        related_name='order_items',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    product_name = models.CharField(max_length=200, default='')
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    selected_size = models.CharField(max_length=20, blank=True, default='')
    selected_color = models.CharField(max_length=50, blank=True, default='')

    def save(self, *args, **kwargs):
        if self.product_id and self.variant_id is None:
            self.variant = ProductVariant.objects.filter(
                product_id=self.product_id,
                size=self.selected_size,
                color=self.selected_color,
            ).first()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.product_name} x {self.quantity}'


class StaffCallLog(models.Model):
    order = models.ForeignKey(Order, related_name='call_logs', on_delete=models.CASCADE)
    staff_member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name='staff_call_logs',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    note = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'staff call log'
        verbose_name_plural = 'staff call logs'

    def __str__(self):
        return (
            f'Call log for order #{self.order.display_order_number} '
            f'at {self.created_at:%Y-%m-%d %H:%M}'
        )


class SiteSettings(models.Model):
    brand_name = models.CharField(max_length=100, default='BDBazaar')
    brand_area = models.CharField(max_length=120, blank=True, default='Bangladesh')
    inside_dhaka_charge = models.DecimalField(max_digits=10, decimal_places=2, default=60)
    outside_dhaka_charge = models.DecimalField(max_digits=10, decimal_places=2, default=120)
    next_order_number = models.PositiveBigIntegerField(
        default=1001,
        help_text='The next staff-visible order number. It increases automatically when an order is created.',
    )
    logo = models.ImageField(upload_to='branding/', blank=True, null=True)
    nav_bg_color = models.CharField(max_length=20, default='#ffffff')
    nav_text_color = models.CharField(max_length=20, default='#111827')
    accent_color = models.CharField(max_length=20, default='#1d4ed8')
    search_placeholder = models.CharField(max_length=80, default='Search products...')
    meta_title = models.CharField(
        max_length=120,
        default='BDBazaar | Online Shopping in Bangladesh',
        help_text='Default title for pages without a more specific title. Aim for 50–60 characters so it is less likely to be cut off in search results.',
    )
    meta_description = models.CharField(
        max_length=255,
        default='Shop quality electronics, home essentials, fashion, and beauty products online in Bangladesh.',
        help_text='Default search snippet. Aim for 150–160 characters; Google may shorten or replace it.',
    )
    home_meta_title = models.CharField(
        max_length=120,
        blank=True,
        default='',
        help_text='Optional homepage title override. Aim for 50–60 characters.',
    )
    home_meta_description = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Optional homepage search snippet override. Aim for 150–160 characters.',
    )
    categories_meta_title = models.CharField(
        max_length=120,
        blank=True,
        default='',
        help_text='Optional category directory title override. Aim for 50–60 characters.',
    )
    categories_meta_description = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Optional category directory search snippet override. Aim for 150–160 characters.',
    )
    shop_meta_title = models.CharField(
        max_length=120,
        blank=True,
        default='',
        help_text='Optional shop page title override. Aim for 50–60 characters.',
    )
    shop_meta_description = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Optional shop page search snippet override. Aim for 150–160 characters.',
    )
    og_title = models.CharField(
        max_length=120,
        default='BDBazaar | Bangladesh Ecommerce Store',
        help_text='Social sharing title. Aim for about 50–60 characters.',
    )
    og_description = models.CharField(
        max_length=255,
        default='Shop trusted essentials, gadgets, and home upgrades across Bangladesh.',
        help_text='Social sharing summary. Aim for about 150–200 characters.',
    )
    og_image = models.ImageField(upload_to='seo/', blank=True, null=True)
    google_site_verification = models.CharField(max_length=255, blank=True, default='')
    twitter_handle = models.CharField(
        max_length=30,
        blank=True,
        default='',
        help_text='Optional X/Twitter handle, including @.',
    )
    search_engine_indexing = models.BooleanField(
        default=True,
        help_text='Turn off to add noindex metadata and block crawling while the site is private or being prepared.',
    )
    canonical_url = models.URLField(
        blank=True,
        default='',
        help_text='Your public site origin, such as https://www.example.com, without a trailing slash. Leave blank until your domain is live.',
    )
    ga_measurement_id = models.CharField(max_length=40, blank=True, default='')
    facebook_pixel_id = models.CharField(max_length=40, blank=True, default='')
    facebook_conversions_api_access_token = models.CharField(max_length=500, blank=True, default='')
    PATHAO_ENVIRONMENT_CHOICES = [
        ('sandbox', 'Sandbox / testing'),
        ('production', 'Production / live'),
    ]
    pathao_environment = models.CharField(
        max_length=10,
        choices=PATHAO_ENVIRONMENT_CHOICES,
        default='production',
        help_text='Pathao production receives new shipments.',
    )
    pathao_sandbox_client_id = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text='Client ID issued for your Pathao Courier API application. Use Sandbox credentials while testing.',
    )
    pathao_sandbox_client_secret = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text='Sandbox client secret. This is masked in admin and kept when left blank.',
    )
    pathao_sandbox_username = models.CharField(
        max_length=254,
        blank=True,
        default='',
        help_text='Sandbox merchant account username authorized for API access.',
    )
    pathao_sandbox_password = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text='Sandbox merchant account password. This is masked in admin and kept when left blank.',
    )
    pathao_sandbox_base_url = models.URLField(
        blank=True,
        default='https://courier-api-sandbox.pathao.com',
        help_text='Pathao Sandbox API base URL.',
    )
    pathao_sandbox_store_id = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text='Optional Sandbox Store ID. Leave blank to use the only/default active store.',
    )
    pathao_production_client_id = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text='Client ID issued for your Pathao production API application.',
    )
    pathao_production_client_secret = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text='Production client secret. This is masked in admin and kept when left blank.',
    )
    pathao_production_username = models.CharField(
        max_length=254,
        blank=True,
        default='',
        help_text='Production Pathao merchant account username authorized for API access.',
    )
    pathao_production_password = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text='Production merchant account password. This is masked in admin and kept when left blank.',
    )
    pathao_production_base_url = models.URLField(
        blank=True,
        default='https://api-hermes.pathao.com',
        help_text='Pathao production API base URL. Requests here can create live deliveries.',
    )
    pathao_production_store_id = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text='Optional production Store ID. Leave blank to use the only/default active store.',
    )
    pathao_grant_type = models.CharField(
        max_length=30,
        default='password',
        help_text='OAuth grant type used by the Pathao token endpoint (usually password).',
    )
    pathao_item_weight = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=Decimal('0.50'),
        help_text='Default parcel weight in kilograms sent as item_weight. Set this to the typical packed parcel weight.',
    )
    bkash_payment_number = models.CharField(max_length=30, blank=True, default='')
    nagad_payment_number = models.CharField(max_length=30, blank=True, default='')
    about_text = models.TextField(blank=True, default='Fast and trusted shopping for everyday essentials, gadgets, and home upgrades across Bangladesh.')
    phone = models.CharField(max_length=30, default='+880 1700-000000')
    email = models.EmailField(default='support@bdbazaar.com')
    address = models.CharField(max_length=255, default='Dhaka, Bangladesh')
    copyright_text = models.CharField(max_length=255, default='© 2026 BDBazaar. All rights reserved.')
    facebook_url = models.URLField(blank=True, default='')
    instagram_url = models.URLField(blank=True, default='')
    twitter_url = models.URLField(blank=True, default='')
    youtube_url = models.URLField(blank=True, default='')
    whatsapp_number = models.CharField(
        max_length=15,
        blank=True,
        default='',
        validators=[
            RegexValidator(
                regex=r'^\d{8,15}$',
                message='Enter 8–15 digits, including the country code, without +, spaces, or punctuation.',
            ),
        ],
        help_text='Include the country code using digits only, for example 8801712345678.',
    )

    class Meta:
        verbose_name = 'Site settings'
        verbose_name_plural = 'Site settings'

    def __str__(self):
        return self.brand_name


class Banner(models.Model):
    title = models.CharField(max_length=120)
    subtitle = models.CharField(max_length=220, blank=True)
    button_text = models.CharField(max_length=40, default='Shop now')
    button_url = models.CharField(max_length=255, default='/shop/')
    image = models.ImageField(upload_to='banners/', blank=True, null=True)
    is_active = models.BooleanField(default=True)
    ordering = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['ordering', 'created_at']

    def __str__(self):
        return self.title
