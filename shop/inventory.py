from django.db.models import Sum

from .models import ProductVariant, Stock


def sync_product_stock(product_id):
    totals = ProductVariant.objects.filter(product_id=product_id).aggregate(
        quantity=Sum('quantity'),
        opening_quantity=Sum('opening_quantity'),
        restocked_quantity=Sum('restocked_quantity'),
    )
    Stock.objects.filter(product_id=product_id).update(
        quantity=totals['quantity'] or 0,
        opening_quantity=totals['opening_quantity'] or 0,
        restocked_quantity=totals['restocked_quantity'] or 0,
    )
