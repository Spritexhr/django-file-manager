# Samba 文件共享

本项目通过一个独立的 Samba 容器提供 SMB 文件访问。Samba 与 Django `web`
容器挂载同一个 Docker `media` 卷；Django 本身仍使用普通
`FileSystemStorage`，不包含 Python SMB 客户端，也不会在默认启动时监听 445。

Django 文件下载现在统一走登录鉴权的 `/download/<id>/` 路由，项目不再提供
`/media/` 直出。升级时必须同时检查 nginx/OpenResty 等反向代理，删除任何
`location /media/`、`alias` 或其他直接暴露 media 卷的旧配置，否则会绕过
Django 的用户权限校验。

## 重要的账户与权限边界

- Samba 使用一套独立的 `SAMBA_USER` / `SAMBA_PASSWORD`。
- 该账户可以看到并修改**整个 media 卷**，因此也能看到所有 Django 用户的
  `user_<id>` 目录。当前方案不提供按 Django 用户隔离的 SMB 权限。
- Samba 账户不是 Django 账户。Django 只保存不可逆密码哈希，无法把现有
  Django 密码同步给 Samba；不要为两边复用同一个密码。
- 把 SMB 权限交给某个人，等同于授予其整个上传文件卷的读写权限。

## 启用

复制环境变量示例并设置至少 12 个字符的独立密码：

```bash
cp .env.example .env
# 编辑 .env 中的 SAMBA_USER 和 SAMBA_PASSWORD
```

随后显式启用 `smb` profile：

```bash
docker compose --profile smb up --build -d
docker compose --profile smb ps
docker compose logs samba
```

不带 `--profile smb` 的普通 `docker compose up` 不会启动 Samba。

默认共享名是 `files`：

- Windows：`\\127.0.0.1\files`
- macOS Finder：`smb://127.0.0.1/files`
- Linux：`smbclient //127.0.0.1/files -U filemanager`

Samba 禁用了 NetBIOS 发现，因此客户端需要直接使用主机名或 IP 地址。

## 网络暴露

Compose 默认设置为：

```env
SAMBA_BIND_ADDRESS=127.0.0.1
SAMBA_PORT=445
```

这意味着只有 Docker 宿主机本机可以连接。若确实需要局域网访问，应把
`SAMBA_BIND_ADDRESS` 改成宿主机的**指定局域网 IP**，并同时使用主机防火墙或
VPN 限制 TCP 445 的来源。不要把 445 直接暴露到公网，也不建议使用
`0.0.0.0` 作为方便性的默认值。

Windows 宿主机常由系统文件共享服务占用 445，Docker 此时会绑定失败。SMB
文件管理器通常也不能在 UNC 地址中指定自定义端口，因此生产环境更适合使用
一台端口 445 未被占用的 Linux 主机或独立内网 IP。

## 协议与安全默认值

`deploy/samba/smb.conf` 当前配置：

- 协议协商范围为 SMB 2.1 至 SMB 3.1.1；SMB1 和 NetBIOS 已禁用。
- 所有 SMB2/3 消息强制签名。
- `files` 共享强制原生 SMB3 传输加密，因此实际文件访问要求现代 SMB3
  客户端；仅支持 SMB2 的客户端会被拒绝。
- 禁止 guest 和匿名枚举。
- 启用 macOS `fruit`、`streams_xattr` 与 Finder 元数据兼容。
- 不跟随共享目录中的符号链接。

加密在共享级别使用 `smb encrypt = required` 强制执行，不应为了兼容旧客户端
而降级。现代 Windows、macOS 和 Samba 客户端均支持 SMB3 加密。

## 文件布局、旧数据迁移与 Django 同步

Samba 展示与 Django 文件夹祖先链对应的物理目录树。新上传路径为：

```text
media/
  user_<django-user-id>/
    根目录文件.ext
    父文件夹/
      子文件夹/
        filename.ext
```

通过 SMB 创建、移动或重命名目录时，后续同步会使用这棵物理树协调 Django 的
`Folder` 和 `File` 记录。Samba 账户仍可进入所有 `user_<id>` 根目录。

升级前创建的旧文件可能仍位于扁平的 `user_<id>/<filename>` 路径。部署新版本并
完成数据库迁移后，先执行只读预览：

```bash
docker compose exec web python manage.py materialize_storage
```

确认移动计划和冲突处理符合预期，再实际写入：

```bash
docker compose exec web python manage.py materialize_storage --apply
```

迁移前应同时备份数据库和 media 卷；不要跳过 dry-run。迁移期间避免 Django 和
SMB 客户端继续写入同一目录。

通过 SMB 完成一批外部改动后，可显式同步到 Django：

```bash
docker compose exec web python manage.py sync_samba
```

默认同步用于发现/协调物理树，不主动清理数据库中缺少对应文件的记录。确认物理
删除也应反映到 Django 后，才使用：

```bash
docker compose exec web python manage.py sync_samba --prune
```

`--prune` 具有删除性，执行前应停止并发写入并做好备份。

默认情况下：

```env
DJANGO_FILESYSTEM_SYNC_ON_BROWSE=false
```

此时 Samba 新增的文件不会在页面浏览时自动生成 Django 记录，应运行
`sync_samba`。确实需要即时导入时可设为 `true`，由文件管理页在用户浏览时协调
其物理目录与数据库记录。该开关不改变 Samba 账户能读取整个卷的事实，也不能
替代大批量改动后的显式同步命令。

为了避免页面同步到尚未写完的内容，SMB 客户端应先写入临时名称，完成并关闭
文件后再在同一目录原子重命名为最终文件名。不要通过 SMB 创建符号链接、设备
文件或绕过应用允许的文件类型规则。

## 共享卷权限

Samba 容器默认用 UID/GID 1000 创建非 root 账户，并在启动时给共享卷设置组
权限和默认 ACL：

```env
SAMBA_UID=1000
SAMBA_GID=1000
```

默认 ACL 让 Django 后续创建的文件仍可由 Samba 账户修改。UID/GID 应保持稳定，
且不能与 Samba 镜像中的其他系统账户冲突。首次处理包含大量文件的卷时，递归
修复 ACL 可能使 Samba 启动时间变长。Django 同时使用 `0660` 文件权限和
`2770` 目录权限，避免网页上传后把 ACL 的组写权限重新收窄。

## 运维命令

验证配置和进程：

```bash
docker compose --profile smb exec samba testparm -s
docker compose --profile smb exec samba smbcontrol smbd ping
```

修改账户或密码后重建容器内的 Samba 账户数据库：

```bash
docker compose --profile smb up -d --force-recreate samba
```

停止 SMB，但保留 Django 与 media 数据：

```bash
docker compose --profile smb stop samba
```

排查连接问题时检查：宿主机 445 是否已占用、`SAMBA_BIND_ADDRESS` 是否正确、
客户端是否支持 SMB2/3、密码是否满足长度要求，以及主机防火墙是否允许来源 IP。

## 备份与一致性

Samba 和 Django 可同时写同一个卷，但文件操作与 SQLite 事务并非一个原子事务。
备份时应同时备份 `db_data` 和 `media` 两个卷；只备份其中之一可能得到无法对应
的数据库记录或孤立文件。执行恢复或大批量 SMB 移动/删除前，建议先停止 `web`
和 `samba`，完成后再一起启动。
