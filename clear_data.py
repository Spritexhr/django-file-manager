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
from django.core.management import call_command
from django.db import connection

def table_exists(table_name):
    """检查表是否存在"""
    return table_name in connection.introspection.table_names()

def clear_all_data():
    """清除所有用户数据、文件和文件夹"""
    print("开始清理数据...")
    
    # 检查是否需要运行迁移
    if not table_exists('core_file'):
        print("数据库表不存在，正在运行迁移...")
        call_command('migrate')
    
    # Flush clears application/auth/session data and resets sequences while
    # deliberately preserving django_migrations. Removing migration history
    # without dropping tables makes the next migrate fail with "table exists".
    print("清空数据库记录并重置序列...")
    call_command('flush', interactive=False, verbosity=0)
    print("数据库记录已清空")
    
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
            raise
    else:
        print("媒体目录不存在")
    
    print("数据清理完成！所有用户数据已清除。")

if __name__ == "__main__":
    clear_all_data()
