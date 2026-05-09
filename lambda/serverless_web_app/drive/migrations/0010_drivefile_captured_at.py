from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('drive', '0009_drivefolder_deleted_at')]
    operations = [
        migrations.AddField(
            model_name='drivefile',
            name='captured_at',
            field=models.DateTimeField(null=True, blank=True, db_index=True),
        ),
    ]
