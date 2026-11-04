from django.db import migrations, models
import django.db.models.deletion


def migrate_stock_data(apps, schema_editor):
    database = schema_editor.connection.alias
    Product = apps.get_model('shop', 'Product')
    Stock = apps.get_model('shop', 'Stock')
    Restock = apps.get_model('shop', 'InventoryRestock')
    restock_totals = dict(
        Restock.objects.using(database)
        .values('product_id')
        .annotate(total=models.Sum('quantity'))
        .values_list('product_id', 'total')
    )

    Stock.objects.using(database).bulk_create(
        Stock(
            product_id=product.pk,
            quantity=product.stock_quantity,
            opening_quantity=product.initial_stock_quantity,
            restocked_quantity=restock_totals.get(product.pk, 0) or 0,
        )
        for product in Product.objects.using(database).only(
            'pk',
            'stock_quantity',
            'initial_stock_quantity',
        ).iterator()
    )


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0028_product_delete_history'),
    ]

    operations = [
        migrations.CreateModel(
            name='Stock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity', models.PositiveIntegerField(default=0)),
                ('opening_quantity', models.PositiveIntegerField(default=0)),
                ('restocked_quantity', models.PositiveIntegerField(default=0)),
                ('product', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='stock', to='shop.product')),
            ],
            options={
                'ordering': ['product__name'],
            },
        ),
        migrations.RunPython(migrate_stock_data, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='product',
            name='initial_stock_quantity',
        ),
        migrations.RemoveField(
            model_name='product',
            name='stock_quantity',
        ),
        migrations.DeleteModel(
            name='InventoryRestock',
        ),
    ]
