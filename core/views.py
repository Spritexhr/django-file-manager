import shutil
import os
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from .models import Folder, File
from .forms import FileUploadForm, FolderForm
from django.conf import settings

@login_required
def file_manager(request, folder_id=None):
    # Get current folder or root
    if folder_id:
        current_folder = get_object_or_404(Folder, id=folder_id, created_by=request.user)
        # Build breadcrumbs for navigation
        breadcrumbs = []
        parent = current_folder
        while parent:
            breadcrumbs.append(parent)
            parent = parent.parent
        breadcrumbs.reverse()
    else:
        current_folder = None
        breadcrumbs = []

    # Handle Forms
    if request.method == 'POST':
        upload_form = FileUploadForm(request.POST, request.FILES)
        folder_form = FolderForm(request.POST)

        # Handle file uploads
        if 'upload_file' in request.POST:
            if upload_form.is_valid():
                for f in upload_form.cleaned_data['files']:
                    File.objects.create(
                        file=f,
                        folder=current_folder,
                        uploaded_by=request.user
                    )
                return redirect(request.path)
            for error in upload_form.non_field_errors():
                messages.error(request, error)

        # Handle folder creation
        if 'create_folder' in request.POST:
            if folder_form.is_valid():
                new_folder = folder_form.save(commit=False)
                new_folder.parent = current_folder
                new_folder.created_by = request.user
                new_folder.save()
                return redirect(request.path)
            for field, errs in folder_form.errors.items():
                for err in errs:
                    messages.error(request, err)
    else:
        upload_form = FileUploadForm()
        folder_form = FolderForm()

    # Get content for the current folder
    if current_folder:
        subfolders = current_folder.subfolders.filter(created_by=request.user)
        files = current_folder.files.filter(uploaded_by=request.user)
    else: # Root directory
        subfolders = Folder.objects.filter(parent__isnull=True, created_by=request.user)
        files = File.objects.filter(folder__isnull=True, uploaded_by=request.user)

    # Get disk capacity 💾
    total, used, free = shutil.disk_usage(settings.MEDIA_ROOT)
    disk_capacity = {
        'total': f"{total / (1024**3):.2f} GB",
        'used': f"{used / (1024**3):.2f} GB",
        'free': f"{free / (1024**3):.2f} GB",
        'percent_used': f"{(used / total) * 100:.2f}"
    }

    context = {
        'current_folder': current_folder,
        'subfolders': subfolders,
        'files': files,
        'upload_form': upload_form,
        'folder_form': folder_form,
        'breadcrumbs': breadcrumbs,
        'disk_capacity': disk_capacity
    }
    return render(request, 'core/file_manager.html', context)

@login_required
def delete_file(request, file_id):
    """删除文件"""
    if request.method == 'POST':
        file_obj = get_object_or_404(File, id=file_id, uploaded_by=request.user)
        
        # 获取文件所在的文件夹ID，用于重定向
        folder_id = file_obj.folder.id if file_obj.folder else None
        
        # 删除物理文件
        if file_obj.file and os.path.exists(file_obj.file.path):
            os.remove(file_obj.file.path)
        
        # 删除数据库记录
        file_obj.delete()
        
        messages.success(request, f'文件 "{file_obj.file.name.split("/")[-1]}" 已成功删除')
        
        # 返回JSON响应（用于AJAX请求）
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': True, 'message': '文件删除成功'})
        
        # 返回重定向到正确的文件夹页面
        if folder_id:
            return redirect('file_manager_folder', folder_id=folder_id)
        else:
            return redirect('file_manager_root')
    
    return redirect('file_manager_root')

@login_required
def delete_folder(request, folder_id):
    """删除文件夹（递归删除所有子文件夹和文件）"""
    if request.method == 'POST':
        folder_obj = get_object_or_404(Folder, id=folder_id, created_by=request.user)
        folder_name = folder_obj.name
        
        # 获取父文件夹ID，用于重定向
        parent_folder_id = folder_obj.parent.id if folder_obj.parent else None
        
        # 递归删除文件夹及其内容
        delete_folder_recursive(folder_obj)
        
        messages.success(request, f'文件夹 "{folder_name}" 及其所有内容已成功删除')
        
        # 返回JSON响应（用于AJAX请求）
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': True, 'message': '文件夹删除成功'})
        
        # 返回重定向到父文件夹页面
        if parent_folder_id:
            return redirect('file_manager_folder', folder_id=parent_folder_id)
        else:
            return redirect('file_manager_root')
    
    return redirect('file_manager_root')

def delete_folder_recursive(folder):
    """递归删除文件夹及其所有内容"""
    # 删除文件夹中的所有文件
    for file_obj in folder.files.all():
        if file_obj.file and os.path.exists(file_obj.file.path):
            os.remove(file_obj.file.path)
        file_obj.delete()
    
    # 递归删除子文件夹
    for subfolder in folder.subfolders.all():
        delete_folder_recursive(subfolder)
    
    # 删除文件夹本身
    folder.delete()