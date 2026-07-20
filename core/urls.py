from django.urls import path
from . import views

urlpatterns = [
    path('', views.file_manager, name='file_manager_root'),
    path('files/<path:folder_path>/', views.file_manager, name='file_manager_folder'),
    path('folder/<int:folder_id>/', views.legacy_folder_redirect, name='legacy_file_manager_folder'),
    path('download/<int:file_id>/', views.download_file, name='download_file'),
    path('delete/file/<int:file_id>/', views.delete_file, name='delete_file'),
    path('delete/folder/<int:folder_id>/', views.delete_folder, name='delete_folder'),
    path('delete/bulk/', views.bulk_delete, name='bulk_delete'),
    path('move/', views.move_items, name='move_items'),
    path('reorder/', views.reorder, name='reorder'),
    # User management (staff only)
    path('users/', views.user_management, name='user_management'),
    path('users/create/', views.user_create, name='user_create'),
    path('users/<int:user_id>/password/', views.user_set_password, name='user_set_password'),
    path('users/<int:user_id>/toggle-active/', views.user_toggle_active, name='user_toggle_active'),
    path('users/<int:user_id>/delete/', views.user_delete, name='user_delete'),
]
