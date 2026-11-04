from datetime import timedelta
from decimal import Decimal
import random

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from shop.models import Order, OrderItem, Product, SiteSettings


class Command(BaseCommand):
    help = 'Add sample orders with varied statuses without changing product stock.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--count',
            type=int,
            default=50,
            help='Number of sample orders to add (default: 50).',
        )
        parser.add_argument(
            '--single-item',
            action='store_true',
            help='Create each order with exactly one product and quantity 1.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        count = options['count']
        if count < 1:
            raise CommandError('--count must be at least 1.')

        products = list(
            Product.objects.filter(is_active=True)
            .select_related('category')
            .prefetch_related('variants')
            .order_by('pk')
        )
        if not products:
            raise CommandError('Add at least one active product before creating sample orders.')
        if any(product.starting_variant is None for product in products):
            raise CommandError('Every active product needs at least one priced variant before creating sample orders.')

        settings = SiteSettings.objects.first() or SiteSettings()
        statuses = [status for status, _ in Order.STATUS_CHOICES]
        delivery_areas = list(Order.DELIVERY_AREA_CHOICES)
        cities = [
            ('Dhaka', 'Dhanmondi', 'Dhaka', 'inside_dhaka'),
            ('Chattogram', 'Pahartali', 'Chattogram', 'outside_dhaka'),
            ('Khulna', 'Sonadanga', 'Khulna', 'outside_dhaka'),
            ('Sylhet', 'Zindabazar', 'Sylhet', 'outside_dhaka'),
            ('Rajshahi', 'Boalia', 'Rajshahi', 'outside_dhaka'),
        ]
        randomizer = random.Random()
        now = timezone.now()
        first_order_number = Order.objects.count() + 1
        created_order_ids = []
        single_item = options['single_item']

        for index in range(count):
            sample_number = first_order_number + index
            city, thana, zilla, delivery_area = cities[index % len(cities)]
            selected_products = randomizer.sample(
                products,
                1 if single_item else min(2, len(products)),
            )
            selected_variants = [
                product.starting_variant for product in selected_products
            ]
            item_quantities = [
                1 if single_item else randomizer.randint(1, 2)
                for _ in selected_products
            ]
            subtotal = sum(
                (
                    variant.price * quantity
                    for variant, quantity in zip(selected_variants, item_quantities)
                ),
                Decimal('0.00'),
            )
            shipping_charge = (
                Decimal('0.00')
                if any(product.free_shipping for product in selected_products)
                else (
                    settings.inside_dhaka_charge
                    if delivery_area == 'inside_dhaka'
                    else settings.outside_dhaka_charge
                )
            )
            status = statuses[index % len(statuses)]
            age_days = randomizer.randint(0, 60)
            delivered_at = now - timedelta(days=randomizer.randint(0, max(age_days, 1))) if status == 'delivered' else None
            order = Order.objects.create(
                full_name=f'Sample Customer {sample_number:03d}',
                phone=f'019{sample_number:08d}',
                email=f'sample+{sample_number:03d}@example.com',
                address=f'{sample_number} Sample Road',
                city=city,
                thana=thana,
                zilla=zilla,
                delivery_area=delivery_area,
                payment_method='cod',
                note='Sample order for dashboard testing.',
                subtotal=subtotal,
                shipping_charge=shipping_charge,
                total=subtotal + shipping_charge,
                status=status,
                delivered_at=delivered_at,
            )
            created_order_ids.append(order.pk)

            OrderItem.objects.bulk_create([
                OrderItem(
                    order=order,
                    product=product,
                    variant=variant,
                    product_name=product.name,
                    quantity=quantity,
                    unit_price=variant.price,
                    line_total=variant.price * quantity,
                    selected_size=variant.size,
                    selected_color=variant.color,
                )
                for product, variant, quantity in zip(
                    selected_products,
                    selected_variants,
                    item_quantities,
                )
            ])

            Order.objects.filter(pk=order.pk).update(
                created_at=now - timedelta(days=age_days),
            )

        self.stdout.write(self.style.SUCCESS(
            f'Added {len(created_order_ids)} sample orders and '
            f'{OrderItem.objects.filter(order_id__in=created_order_ids).count()} order items '
            'with varied statuses. Product stock was not changed.'
        ))
