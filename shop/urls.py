from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('categories/', views.categories, name='categories'),
    path('shop/search-suggestions/', views.search_suggestions, name='search_suggestions'),
    path('shop/', views.shop, name='shop'),
    path('product/<slug:slug>/', views.product_detail, name='product_detail'),
    path('cart/', views.cart, name='cart'),
    path('cart/add/<slug:slug>/', views.add_to_cart, name='add_to_cart'),
    path('buy-now/<slug:slug>/', views.buy_now, name='buy_now'),
    path('cart/update/<slug:slug>/', views.update_cart, name='update_cart'),
    path('cart/remove/<slug:slug>/', views.remove_from_cart, name='remove_from_cart'),
    path('checkout/', views.checkout, name='checkout'),
    path('order/success/<int:order_id>/', views.order_success, name='order_success'),
    path('sitemap.xml', views.sitemap_view, name='sitemap'),
    path('robots.txt', views.robots_txt, name='robots_txt'),
    path('staff-login/', views.staff_login, name='staff_login'),
    path('staff-logout/', views.staff_logout, name='staff_logout'),
    path('super-admin-dashboard/', views.super_admin_dashboard, name='super_admin_dashboard'),
    path('admin-dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('staff-dashboard/', views.staff_dashboard, name='staff_dashboard'),
    path(
        'staff-dashboard/pathao/status-sync/',
        views.start_pathao_status_sync,
        name='staff_start_pathao_status_sync',
    ),
    path('staff-dashboard/inventory/', views.staff_inventory, name='staff_inventory'),
    path(
        'staff-dashboard/products/<int:product_id>/variants/',
        views.update_product_variants,
        name='staff_update_product_variants',
    ),
    path('staff-dashboard/orders/export/', views.export_staff_orders, name='staff_export_orders'),
    path('staff-dashboard/products/<int:product_id>/restock/', views.restock_product, name='staff_restock_product'),
    path('staff-dashboard/order/<int:order_id>/', views.staff_order_detail, name='staff_order_detail'),
    path('staff-dashboard/order/<int:order_id>/calls/', views.staff_call_log, name='staff_call_log'),
    path(
        'staff-dashboard/order/<int:order_id>/pathao/',
        views.staff_pathao_order,
        name='staff_pathao_order',
    ),
    path(
        'staff-dashboard/order/<int:order_id>/pathao/create/',
        views.create_pathao_shipment,
        name='staff_create_pathao_shipment',
    ),
    path('staff-dashboard/order/<int:order_id>/invoice/', views.download_order_invoice, name='staff_order_invoice'),
    path('admin-dashboard/order/<int:order_id>/status/', views.update_order_status, name='update_order_status'),
]
