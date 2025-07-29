#!/usr/bin/env python
"""
数据清理脚本 - 用于发布前清除所有个人数据
"""
import os
import shutil
import sys
import django

# 添加项目路径到Python路径
project_path = os.path.join(os.path.dirname(__file__), 'file_manager_project')
sys.path.insert(0, project_path)

# 在导入任何Django模型之前，先配置Django设置
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'file_manager_project.settings')
django.setup()

from django.conf import settings
from django.db import connection
from django.core.management import call_command
from core.models import User, Folder, File

def table_exists(table_name):
    """检查表是否存在"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=%s", [table_name])
        return cursor.fetchone() is not None

def clear_all_data():
    """清除所有用户数据、文件和文件夹"""
    print("开始清理数据...")
    
    # 检查是否需要运行迁移
    if not table_exists('core_file'):
        print("数据库表不存在，正在运行迁移...")
        call_command('migrate')
    
    # 1. 删除所有文件记录
    print("删除文件记录...")
    try:
        File.objects.all().delete()
        print("文件记录已删除")
    except Exception as e:
        print(f"删除文件记录时出错: {e}")
    
    # 2. 删除所有文件夹记录
    print("删除文件夹记录...")
    try:
        Folder.objects.all().delete()
        print("文件夹记录已删除")
    except Exception as e:
        print(f"删除文件夹记录时出错: {e}")
    
    # 3. 删除所有用户（包括超级用户）
    print("删除所有用户（包括超级用户）...")
    try:
        User.objects.all().delete()
        print("所有用户已删除")
    except Exception as e:
        print(f"删除用户时出错: {e}")
    
    # 4. 删除media目录中的所有文件
    print("删除媒体文件...")
    media_root = settings.MEDIA_ROOT
    if os.path.exists(media_root):
        try:
            for item in os.listdir(media_root):
                item_path = os.path.join(media_root, item)
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                else:
                    os.remove(item_path)
            print(f"已清理媒体目录: {media_root}")
        except Exception as e:
            print(f"清理媒体目录时出错: {e}")
    else:
        print("媒体目录不存在")
    
    # 5. 重置数据库序列
    print("重置数据库序列...")
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM django_migrations WHERE app = 'core'")
            cursor.execute("DELETE FROM sqlite_sequence WHERE name IN ('core_file', 'core_folder', 'auth_user')")
        print("数据库序列已重置")
    except Exception as e:
        print(f"重置数据库序列时出错: {e}")
    
    print("数据清理完成！所有用户数据已清除。")

if __name__ == "__main__":
    clear_all_data() 