from django.contrib import admin
from .models import Folder, File

# Register your models here.

@admin.register(Folder)
class FolderAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'created_by', 'created_at')
    list_filter = ('created_at', 'created_by')
    search_fields = ('name',)
    readonly_fields = ('created_at',)
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('parent', 'created_by')

@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    list_display = ('get_filename', 'folder', 'uploaded_by', 'uploaded_at')
    list_filter = ('uploaded_at', 'uploaded_by', 'folder')
    search_fields = ('file', 'folder__name')
    readonly_fields = ('uploaded_at',)
    
    def get_filename(self, obj):
        return obj.file.name.split('/')[-1] if obj.file else ''
    get_filename.short_description = '文件名'
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('folder', 'uploaded_by')
