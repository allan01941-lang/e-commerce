from django.urls import reverse

from .models import SiteSettings
from .seo import build_seo_context


def get_site_settings(request):
    if not hasattr(request, '_site_settings'):
        request._site_settings = SiteSettings.objects.first()
    return request._site_settings


def cart_count(request):
    cart = request.session.get('cart', {})
    total_items = sum(int(quantity) for quantity in cart.values())
    return {'cart_count': total_items}


def site_settings(request):
    return {'site_settings': get_site_settings(request)}


def seo_metadata(request):
    return build_seo_context(request, get_site_settings(request))


def staff_navigation(request):
    user = request.user
    if not user.is_authenticated or not user.is_staff:
        return {
            'staff_navigation_label': 'Login',
            'staff_navigation_url': reverse('staff_login'),
        }

    if user.is_superuser:
        label = 'Super admin dashboard'
        url = reverse('super_admin_dashboard')
    elif user.groups.filter(name='Owner').exists():
        label = 'Owner dashboard'
        url = reverse('admin_dashboard')
    else:
        label = 'Staff dashboard'
        url = reverse('staff_dashboard')

    return {
        'staff_navigation_label': label,
        'staff_navigation_url': url,
    }
