from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponseRedirect

from .facebook import (
    FacebookPurchaseEventError,
    send_order_purchase_event,
    send_facebook_test_purchase_event,
)
from .inventory import sync_product_stock
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
    StaffCallLog,
    Size,
    Color,
    SiteSettings,
    Stock,
)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'is_active', 'created_at')
    prepopulated_fields = {'slug': ('name',)}
    list_filter = ('is_active',)
    search_fields = ('name', 'slug')


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 4
    fields = ('image', 'caption', 'is_primary', 'ordering')
    verbose_name = 'Product gallery image'
    verbose_name_plural = 'Product images'


class ProductHighlightInline(admin.TabularInline):
    model = ProductHighlight
    extra = 3
    fields = ('icon', 'text', 'ordering')
    verbose_name = 'Product highlight'
    verbose_name_plural = 'Product highlights'


class ProductVariantInlineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return

        combinations = []
        for form in self.forms:
            if self.can_delete and self._should_delete_form(form):
                continue
            if not form.instance.pk and not form.has_changed():
                continue
            combinations.append((
                form.cleaned_data.get('size', ''),
                form.cleaned_data.get('color', ''),
            ))

        if not combinations:
            raise ValidationError(
                'Add at least one size/color variant with its price.'
            )
        if len(combinations) != len(set(combinations)):
            raise ValidationError('Each size/color combination can only be added once.')


class ProductVariantInlineForm(forms.ModelForm):
    size = forms.ChoiceField(
        required=False,
        help_text='Choose a size, or leave blank if this product has no size option.',
    )
    color = forms.ChoiceField(
        required=False,
        help_text='Choose a color, or leave blank if this product has no color option.',
    )

    class Meta:
        model = ProductVariant
        fields = (
            'size',
            'color',
            'price',
            'compare_at_price',
            'opening_quantity',
        )


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    formset = ProductVariantInlineFormSet
    form = ProductVariantInlineForm
    extra = 1
    can_delete = True
    fields = (
        'size',
        'color',
        'price',
        'compare_at_price',
        'opening_quantity',
        'quantity',
        'restocked_quantity',
    )
    readonly_fields = ('quantity', 'restocked_quantity')
    verbose_name = 'Size/color variant'
    verbose_name_plural = 'Variant pricing and stock'

    def get_formset(self, request, obj=None, **kwargs):
        size_choices = [('', '---------'), *(
            (option.name, option.name)
            for option in Size.objects.order_by('name')
        )]
        color_choices = [('', '---------'), *(
            (option.name, option.name)
            for option in Color.objects.order_by('name')
        )]

        class ProductOptionsVariantForm(ProductVariantInlineForm):
            def __init__(self, *args, **form_kwargs):
                super().__init__(*args, **form_kwargs)
                self.fields['size'].choices = size_choices
                self.fields['color'].choices = color_choices

        kwargs['form'] = ProductOptionsVariantForm
        return super().get_formset(request, obj, **kwargs)


@admin.register(Size)
class SizeAdmin(admin.ModelAdmin):
    search_fields = ('name',)


@admin.register(Color)
class ColorAdmin(admin.ModelAdmin):
    search_fields = ('name',)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'category',
        'starting_price',
        'stock_quantity',
        'free_shipping',
        'is_active',
        'is_featured',
    )
    prepopulated_fields = {'slug': ('name',)}
    list_filter = ('is_active', 'is_featured', 'category', 'free_shipping')
    search_fields = (
        'name',
        'slug',
        'short_description',
        'long_description',
        'variants__size',
        'variants__color',
        'sku',
    )
    inlines = [
        ProductVariantInline,
        ProductImageInline,
        ProductHighlightInline,
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related(
            'variants',
        )

    @admin.display(description='Starting price')
    def starting_price(self, obj):
        return obj.starting_price

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        sync_product_stock(form.instance.pk)


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ('product', 'opening_quantity', 'restocked_quantity', 'quantity')
    search_fields = ('product__name', 'product__sku')
    list_select_related = ('product',)


class SiteSettingsAdminForm(forms.ModelForm):
    facebook_conversions_api_access_token = forms.CharField(
        label='Facebook Conversions API access token',
        required=False,
        strip=False,
        widget=forms.TextInput(attrs={'autocomplete': 'off'}),
        help_text=(
            'Visible to administrators with access to Site Settings. Leave blank to keep the existing token.'
        ),
    )
    facebook_test_event_code = forms.CharField(
        label='Facebook test event code',
        max_length=100,
        required=False,
        help_text='Get a code from Meta Events Manager → Test Events. This temporary code is not saved.',
    )
    pathao_production_client_secret = forms.CharField(
        label='Pathao Production client secret',
        required=False,
        strip=False,
        widget=forms.TextInput(attrs={'autocomplete': 'off'}),
        help_text='Visible to administrators with access to Site Settings.',
    )
    pathao_production_password = forms.CharField(
        label='Pathao Production API password',
        required=False,
        strip=False,
        widget=forms.TextInput(attrs={'autocomplete': 'off'}),
        help_text='Visible to administrators with access to Site Settings.',
    )

    class Meta:
        model = SiteSettings
        fields = '__all__'
        exclude = (
            'pathao_environment',
            'pathao_sandbox_client_id',
            'pathao_sandbox_client_secret',
            'pathao_sandbox_username',
            'pathao_sandbox_password',
            'pathao_sandbox_base_url',
            'pathao_sandbox_store_id',
        )

    def clean_facebook_conversions_api_access_token(self):
        token = self.cleaned_data['facebook_conversions_api_access_token']
        if not token and self.instance.pk:
            return self.instance.facebook_conversions_api_access_token
        return token

    def clean_pathao_production_client_secret(self):
        secret = self.cleaned_data['pathao_production_client_secret']
        if not secret and self.instance.pk:
            return self.instance.pathao_production_client_secret
        return secret

    def clean_pathao_production_password(self):
        password = self.cleaned_data['pathao_production_password']
        if not password and self.instance.pk:
            return self.instance.pathao_production_password
        return password


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    form = SiteSettingsAdminForm
    change_form_template = 'admin/shop/sitesettings/change_form.html'

    def save_model(self, request, obj, form, change):
        obj.pathao_environment = 'production'
        super().save_model(request, obj, form, change)

    list_display = (
        'brand_name',
        'inside_dhaka_charge',
        'outside_dhaka_charge',
    )
    fieldsets = (
        ('Store information', {
            'fields': ('brand_name', 'brand_area', 'logo', 'about_text'),
        }),
        ('Shipping and payment settings', {
            'description': 'Set delivery charges. Customer checkout accepts Cash on Delivery.',
            'fields': (
                'inside_dhaka_charge',
                'outside_dhaka_charge',
            ),
        }),
        ('Order numbering', {
            'description': (
                'Set the next number assigned to a new order. This value increments automatically; '
                'existing orders keep their current numbers.'
            ),
            'fields': ('next_order_number',),
        }),
        ('Pathao courier integration', {
            'description': (
                'Configure the production account used for live deliveries. These settings can create real '
                'deliveries. Store ID is optional: the integration automatically selects the only active store '
                'or the default active store. Credentials are visible to administrators and preserved when '
                'left blank. Staff can manually start a background status sync from the staff dashboard.'
            ),
            'fields': (
                'pathao_production_client_id',
                'pathao_production_client_secret',
                'pathao_production_username',
                'pathao_production_password',
                'pathao_production_base_url',
                'pathao_production_store_id',
                'pathao_grant_type',
                'pathao_item_weight',
            ),
        }),
        ('Appearance', {
            'fields': ('nav_bg_color', 'nav_text_color', 'accent_color', 'search_placeholder'),
        }),
        ('SEO and analytics', {
            'description': (
                'Character counts include spaces. Treat these as practical guidelines, not ranking guarantees. '
                'Product metadata uses product name and short description; '
                'home, category directory, and shop pages use these optional page-specific overrides. '
                'Search indexing can be disabled while preparing the site. '
                'Set the public canonical domain after deployment, upload a social sharing image, and enter the verification token from Google Search Console if used. '
                'Delivered-order Facebook Purchase events use the Pixel ID below. '
                'Enter a Conversions API access token here or configure '
                'FACEBOOK_CONVERSIONS_API_ACCESS_TOKEN on the server.'
            ),
            'fields': (
                'search_engine_indexing',
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
                'ga_measurement_id',
                'facebook_pixel_id',
                'facebook_conversions_api_access_token',
                'facebook_test_event_code',
            ),
        }),
        ('Contact and social links', {
            'fields': (
                'phone',
                'email',
                'address',
                'copyright_text',
                'facebook_url',
                'instagram_url',
                'twitter_url',
                'youtube_url',
                'whatsapp_number',
            ),
        }),
    )

    def response_change(self, request, obj):
        if '_send_facebook_test_event' not in request.POST:
            return super().response_change(request, obj)

        test_event_code = request.POST.get('facebook_test_event_code', '').strip()
        if not test_event_code:
            self.message_user(
                request,
                'Enter the test event code from Meta Events Manager before sending a test event.',
                level=messages.ERROR,
            )
        else:
            try:
                send_facebook_test_purchase_event(test_event_code, request)
            except FacebookPurchaseEventError as error:
                self.message_user(request, str(error), level=messages.ERROR)
            else:
                self.message_user(
                    request,
                    'Meta accepted the test Purchase event. Confirm it appears in Events Manager → Test Events.',
                    level=messages.SUCCESS,
                )
        return HttpResponseRedirect(request.path)


@admin.register(Banner)
class BannerAdmin(admin.ModelAdmin):
    list_display = ('title', 'button_text', 'is_active', 'ordering')
    list_filter = ('is_active',)
    search_fields = ('title', 'subtitle', 'button_text')

    class BannerForm(forms.ModelForm):
        class Meta:
            model = Banner
            fields = '__all__'
            help_texts = {
                'image': (
                    'Recommended image size: 1920 × 600 px (wide landscape). '
                    'Use WebP format and keep the image under 100 KB for faster loading. '
                    'Larger images are cropped to fit the banner.'
                ),
            }

    form = BannerForm


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(PathaoShipment)
class PathaoShipmentAdmin(admin.ModelAdmin):
    list_display = (
        'merchant_order_id',
        'consignment_id',
        'environment',
        'order_status',
        'delivery_fee',
        'response_code',
        'status_checked_at',
        'created_at',
    )
    search_fields = (
        'merchant_order_id',
        'consignment_id',
        'order__full_name',
        'order__phone',
    )


@admin.register(PathaoStatusSyncRun)
class PathaoStatusSyncRunAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'status',
        'total_shipments',
        'checked_shipments',
        'failed_shipments',
        'started_at',
        'completed_at',
    )
    readonly_fields = (
        'status',
        'requested_by',
        'total_shipments',
        'checked_shipments',
        'failed_shipments',
        'error_summary',
        'started_at',
        'completed_at',
        'created_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(StaffCallLog)
class StaffCallLogAdmin(admin.ModelAdmin):
    list_display = ('order', 'customer_name', 'staff_member', 'created_at', 'note_preview')
    list_filter = ('created_at', 'staff_member')
    search_fields = ('order__full_name', 'order__phone', 'staff_member__username', 'note')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'

    @admin.display(description='Customer')
    def customer_name(self, call_log):
        return call_log.order.full_name

    @admin.display(description='Note')
    def note_preview(self, call_log):
        return call_log.note[:100]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        'display_order_number', 'full_name', 'phone', 'delivery_area', 'payment_method',
        'payment_number', 'transaction_reference', 'status', 'total', 'created_at',
    )
    list_filter = ('status', 'payment_method', 'delivery_area')
    search_fields = (
        'full_name', 'phone', 'email', 'address', 'thana', 'zilla',
        'payment_number', 'transaction_reference',
    )
    inlines = [OrderItemInline]

    class Media:
        css = {
            'all': ('css/admin-order-items.css',),
        }

    @admin.display(description='Order number', ordering='order_number')
    def display_order_number(self, order):
        return f'#{order.display_order_number}'

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        try:
            sent = send_order_purchase_event(
                form.instance,
                request,
            )
        except FacebookPurchaseEventError as error:
            self.message_user(request, str(error), level=messages.ERROR)
        else:
            if sent:
                self.message_user(request, 'Facebook Purchase event sent for this confirmed order.')
