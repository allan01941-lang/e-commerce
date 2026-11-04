from django import forms
from django.core.exceptions import ValidationError

from .models import Order, StaffCallLog


class CheckoutForm(forms.ModelForm):
    phone = forms.RegexField(
        regex=r'^[0-9]{11}$',
        max_length=11,
        min_length=11,
        error_messages={'invalid': 'Enter an 11-digit numeric phone number.'},
        widget=forms.TextInput(attrs={
            'autocomplete': 'tel',
            'inputmode': 'numeric',
            'maxlength': '11',
            'pattern': '[0-9]{11}',
            'placeholder': 'আপনার মোবাইল নাম্বার লিখুন',
        }),
    )
    payment_method = forms.ChoiceField(
        choices=[('cod', 'Cash on Delivery')],
        widget=forms.RadioSelect,
        initial='cod',
    )
    delivery_area = forms.ChoiceField(
        choices=Order.DELIVERY_AREA_CHOICES,
        widget=forms.RadioSelect,
        initial='inside_dhaka',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].required = False

    class Meta:
        model = Order
        fields = [
            'full_name', 'phone', 'email', 'address', 'thana', 'zilla',
            'delivery_area', 'payment_method', 'note',
        ]
        widgets = {
            'full_name': forms.TextInput(attrs={'placeholder': 'আপনার নাম লিখুন'}),
            'phone': forms.TextInput(attrs={'placeholder': 'আপনার মোবাইল নাম্বার লিখুন'}),
            'email': forms.EmailInput(attrs={'placeholder': 'আপনার ইমেইল লিখুন'}),
            'thana': forms.TextInput(attrs={'placeholder': 'আপনার থানা লিখুন'}),
            'zilla': forms.TextInput(attrs={'placeholder': 'আপনার জেলা লিখুন'}),
            'address': forms.Textarea(attrs={'rows': 2, 'placeholder': 'আপনার ঠিকানা লিখুন'}),
            'note': forms.Textarea(attrs={'rows': 2, 'placeholder':'ডেলিভারি নির্দেশনা '}),
        }


class StaffOrderUpdateForm(forms.ModelForm):
    cash_discount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=0,
        required=False,
        widget=forms.NumberInput(attrs={
            'min': '0',
            'step': '0.01',
            'inputmode': 'decimal',
        }),
    )

    class Meta:
        model = Order
        fields = [
            'full_name',
            'phone',
            'email',
            'address',
            'thana',
            'zilla',
            'delivery_area',
            'status',
        ]


class StaffOrderFilterForm(forms.Form):
    start_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'staff-order-field'}),
    )
    end_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'staff-order-field'}),
    )
    status = forms.ChoiceField(
        required=False,
        choices=[('', 'All statuses'), *Order.STATUS_CHOICES],
        widget=forms.Select(attrs={'class': 'staff-order-field'}),
    )

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')
        if start_date and end_date and start_date > end_date:
            raise ValidationError('The start date must be on or before the end date.')
        return cleaned_data


class StaffCallLogForm(forms.ModelForm):
    class Meta:
        model = StaffCallLog
        fields = ['note']
        widgets = {
            'note': forms.Textarea(attrs={
                'rows': 4,
                'placeholder': 'Record the call attempt or discussion with the customer…',
            }),
        }


class StaffProductVariantForm(forms.Form):
    opening_quantity = forms.IntegerField(
        min_value=0,
        max_value=2147483647,
        required=False,
    )
    restock_quantity = forms.IntegerField(
        min_value=1,
        max_value=2147483647,
        required=False,
    )
