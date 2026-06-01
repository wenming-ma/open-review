# GitLab Deployment

Optional GitLab container assets live here so the repository root only keeps core service artifacts.

Default ports:
- Web UI: `8929`
- SSH: `2224`

Example:

```bash
cd deploy/gitlab
GITLAB_HOST=34.42.232.242 sudo docker compose up -d
```
