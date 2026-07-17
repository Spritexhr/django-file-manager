from django.contrib import admin

from .file_operations import delete_file_record, delete_folder_tree
from .models import Folder, File

# Register your models here.

@admin.register(Folder)
class FolderAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'created_by', 'created_at')
    list_filter = ('created_at', 'created_by')
    search_fields = ('name',)
    readonly_fields = ('name', 'parent', 'created_by', 'position', 'created_at')

    def has_add_permission(self, request):
        # Folder creation must also materialize a shared directory; the web
        # workflow and sync command enforce that invariant.
        return False
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('parent', 'created_by')

    def delete_model(self, request, obj):
        delete_folder_tree(obj)

    def delete_queryset(self, request, queryset):
        for obj in list(queryset):
            delete_folder_tree(obj)

@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    list_display = ('get_filename', 'size', 'folder', 'uploaded_by', 'uploaded_at')
    list_filter = ('uploaded_at', 'uploaded_by', 'folder')
    search_fields = ('file', 'folder__name')
    readonly_fields = (
        'file', 'folder', 'uploaded_by', 'original_name', 'size',
        'content_type', 'position', 'uploaded_at',
    )

    def has_add_permission(self, request):
        # Uploads need metadata capture and storage compensation on failure.
        return False
    
    def get_filename(self, obj):
        return obj.original_name or (obj.file.name.split('/')[-1] if obj.file else '')
    get_filename.short_description = '文件名'
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('folder', 'uploaded_by')

    def delete_model(self, request, obj):
        delete_file_record(obj)

    def delete_queryset(self, request, queryset):
        for obj in list(queryset):
            delete_file_record(obj)
