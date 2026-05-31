from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_alter_file_folder'),
    ]

    operations = [
        migrations.AddField(
            model_name='folder',
            name='position',
            field=models.PositiveIntegerField(db_index=True, default=0),
        ),
        migrations.AddField(
            model_name='file',
            name='position',
            field=models.PositiveIntegerField(db_index=True, default=0),
        ),
        migrations.AlterModelOptions(
            name='folder',
            options={'ordering': ['position']},
        ),
        migrations.AlterModelOptions(
            name='file',
            options={'ordering': ['position']},
        ),
    ]
