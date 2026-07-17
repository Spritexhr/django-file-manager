import core.models
from django.db import migrations, models


def backfill_original_names(apps, schema_editor):
    File = apps.get_model('core', 'File')
    pending = []
    for file_obj in File.objects.all().only('id', 'file').iterator(chunk_size=1000):
        storage_name = str(file_obj.file or '').replace('\\', '/')
        file_obj.original_name = storage_name.rsplit('/', 1)[-1]
        pending.append(file_obj)
        if len(pending) >= 1000:
            File.objects.bulk_update(pending, ['original_name'], batch_size=1000)
            pending.clear()
    if pending:
        File.objects.bulk_update(pending, ['original_name'], batch_size=1000)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_add_position'),
    ]

    operations = [
        migrations.AlterField(
            model_name='file',
            name='file',
            field=models.FileField(max_length=1024, upload_to=core.models.File.get_upload_path),
        ),
        migrations.AddField(
            model_name='file',
            name='content_type',
            field=models.CharField(
                blank=True,
                default='application/octet-stream',
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='file',
            name='original_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='file',
            name='size',
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_original_names, migrations.RunPython.noop),
    ]
