from django.db import migrations, models


def select_production(apps, schema_editor):
    site_settings = apps.get_model('shop', 'SiteSettings')
    site_settings.objects.using(schema_editor.connection.alias).all().update(
        pathao_environment='production',
        pathao_production_base_url='https://api-hermes.pathao.com',
    )


def select_sandbox(apps, schema_editor):
    site_settings = apps.get_model('shop', 'SiteSettings')
    site_settings.objects.using(schema_editor.connection.alias).all().update(
        pathao_environment='sandbox',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0054_remove_sitesettings_pathao_status_sync_time'),
    ]

    operations = [
        migrations.RunPython(select_production, select_sandbox),
        migrations.AlterField(
            model_name='sitesettings',
            name='pathao_environment',
            field=models.CharField(
                choices=[('sandbox', 'Sandbox / testing'), ('production', 'Production / live')],
                default='production',
                help_text='Pathao production receives new shipments.',
                max_length=10,
            ),
        ),
    ]