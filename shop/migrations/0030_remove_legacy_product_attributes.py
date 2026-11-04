from django.db import migrations


def migrate_legacy_attributes(apps, schema_editor):
    database = schema_editor.connection.alias
    Product = apps.get_model('shop', 'Product')
    Size = apps.get_model('shop', 'Size')
    Color = apps.get_model('shop', 'Color')

    for product in Product.objects.using(database).iterator():
        for model, relation_name, field_name in (
            (Size, 'sizes', 'size'),
            (Color, 'colors', 'color'),
        ):
            raw_values = getattr(product, field_name) or ''
            values = {
                value.strip()
                for value in raw_values.replace(';', ',').replace('\n', ',').split(',')
                if value.strip()
            }
            relation = getattr(product, relation_name)
            for value in values:
                option, _ = model.objects.using(database).get_or_create(name=value)
                relation.add(option)


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0029_stock_model'),
    ]

    operations = [
        migrations.RunPython(migrate_legacy_attributes, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='product',
            name='size',
        ),
        migrations.RemoveField(
            model_name='product',
            name='color',
        ),
    ]
