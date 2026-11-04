from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0044_alter_sitesettings_pathao_store_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='sitesettings',
            name='pathao_grant_type',
            field=models.CharField(
                default='password',
                help_text='OAuth grant type used by the Pathao token endpoint. Sandbox setup uses password.',
                max_length=30,
            ),
        ),
    ]
