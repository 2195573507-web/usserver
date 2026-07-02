# ServerHub rollback

Latest backup pointer: `/root/serverhub-migration/latest-backup.txt`.

Current migration backup: `/root/serverhub-migration/backup/20260621-172856`.

Rollback root entry code:

```bash
BACKUP=/root/serverhub-migration/backup/20260621-172856
cp -a "$BACKUP/server-home/main.py" /opt/server-home/main.py
cp -a "$BACKUP/server-home/services.yml" /opt/server-home/services.yml
systemctl restart server-home.service
```

Nginx was not changed in this step. If later Nginx changes are made, restore from `$BACKUP/nginx/` only after `nginx -t` validation.
