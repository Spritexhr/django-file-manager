from django.urls import path
from . import views

urlpatterns = [
    # Root folder view
    path('', views.file_manager, name='file_manager_root'),
    # Nested folder view
    path('folder/<int:folder_id>/', views.file_manager, name='file_manager_folder'),
    # Delete file
    path('delete/file/<int:file_id>/', views.delete_file, name='delete_file'),
    # Delete folder
    path('delete/folder/<int:folder_id>/', views.delete_folder, name='delete_folder'),
]