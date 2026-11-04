from django.db import migrations, models
import django.db.models.deletion


def preserve_product_names(apps, schema_editor):
    database = schema_editor.connection.alias
    restocks = apps.get_model('shop', 'InventoryRestock')
    for receipt in restocks.objects.using(database).select_related('product').iterator():
        receipt.product_name = receipt.product.name
        receipt.save(update_fields=['product_name'])

    order_items = apps.get_model('shop', 'OrderItem')
    for item in order_items.objects.using(database).filter(product_name='').select_related('product').iterator():
        item.product_name = item.product.name
        item.save(update_fields=['product_name'])


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0027_alter_product_short_description_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='inventoryrestock',
            name='product_name',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.RunPython(preserve_product_names, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='inventoryrestock',
            name='product',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='restock_receipts',
                to='shop.product',
            ),
        ),
        migrations.AlterField(
            model_name='orderitem',
            name='product',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='order_items',
                to='shop.product',
            ),
        ),
    ]
