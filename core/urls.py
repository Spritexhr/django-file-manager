from django.urls import path
from . import views

urlpatterns = [
    path('', views.file_manager, name='file_manager_root'),
    path('folder/<int:folder_id>/', views.file_manager, name='file_manager_folder'),
    path('delete/file/<int:file_id>/', views.delete_file, name='delete_file'),
    path('delete/folder/<int:folder_id>/', views.delete_folder, name='delete_folder'),
    path('delete/bulk/', views.bulk_delete, name='bulk_delete'),
]