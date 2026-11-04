from django.db import migrations


DEFAULT_META_DESCRIPTION = (
    'Shop quality electronics, home essentials, fashion, and beauty products online in Bangladesh.'
)


def replace_placeholder_seo_settings(apps, schema_editor):
    SiteSettings = apps.get_model('shop', 'SiteSettings')
    SiteSettings.objects.filter(
        meta_description='Demo ecommerce store for Bangladesh shoppers.'
    ).update(meta_description=DEFAULT_META_DESCRIPTION)
    SiteSettings.objects.filter(
        canonical_url='https://bdbazaar.example.com'
    ).update(canonical_url='')


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0023_alter_sitesettings_canonical_url_and_more'),
    ]

    operations = [
        migrations.RunPython(
            replace_placeholder_seo_settings,
            migrations.RunPython.noop,
        ),
    ]
