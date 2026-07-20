from django.db import migrations, models


def normalize_positions(apps, schema_editor):
    database = schema_editor.connection.alias
    Folder = apps.get_model('core', 'Folder')
    File = apps.get_model('core', 'File')

    counters = {}
    for folder in Folder.objects.using(database).order_by(
        'created_by_id', 'parent_id', 'position', 'id'
    ):
        key = (folder.created_by_id, folder.parent_id)
        counters[key] = counters.get(key, 0) + 1
        Folder.objects.using(database).filter(pk=folder.pk).update(
            position=counters[key]
        )

    counters = {}
    for file_obj in File.objects.using(database).order_by(
        'uploaded_by_id', 'folder_id', 'position', 'id'
    ):
        key = (file_obj.uploaded_by_id, file_obj.folder_id)
        counters[key] = counters.get(key, 0) + 1
        File.objects.using(database).filter(pk=file_obj.pk).update(
            position=counters[key]
        )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_add_position'),
    ]

    operations = [
        migrations.RunPython(normalize_positions, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name='folder',
            options={'ordering': ['position', 'id']},
        ),
        migrations.AlterModelOptions(
            name='file',
            options={'ordering': ['position', 'id']},
        ),
        migrations.AddConstraint(
            model_name='folder',
            constraint=models.CheckConstraint(
                condition=~models.Q(id=models.F('parent_id')),
                name='folder_cannot_parent_itself',
            ),
        ),
        migrations.AddConstraint(
            model_name='folder',
            constraint=models.UniqueConstraint(
                fields=('created_by', 'position'),
                condition=models.Q(parent__isnull=True),
                name='uniq_root_folder_position',
            ),
        ),
        migrations.AddConstraint(
            model_name='folder',
            constraint=models.UniqueConstraint(
                fields=('created_by', 'parent', 'position'),
                condition=models.Q(parent__isnull=False),
                name='uniq_child_folder_position',
            ),
        ),
        migrations.AddConstraint(
            model_name='file',
            constraint=models.UniqueConstraint(
                fields=('uploaded_by', 'position'),
                condition=models.Q(folder__isnull=True),
                name='uniq_root_file_position',
            ),
        ),
        migrations.AddConstraint(
            model_name='file',
            constraint=models.UniqueConstraint(
                fields=('uploaded_by', 'folder', 'position'),
                condition=models.Q(folder__isnull=False),
                name='uniq_child_file_position',
            ),
        ),
    ]
