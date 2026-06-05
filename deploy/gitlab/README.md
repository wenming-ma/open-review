# GitLab Deployment

Optional GitLab container assets live here so the repository root only keeps core service artifacts.

Default ports:
- Web UI: `8929`
- SSH: `2224`

Example:

```bash
cd deploy/gitlab
GITLAB_HOST=localhost docker-compose up -d
```

To pin a local test version or use a preloaded image:

```bash
GITLAB_HOST=localhost GITLAB_IMAGE=gitlab/gitlab-ce:15.11.13-ce.0 docker-compose up -d
```

If the Docker Compose v2 plugin is installed, `docker compose up -d` also works. On hosts that only have
the legacy standalone binary, use `docker-compose` as shown above.

After first boot, wait for `http://localhost:8929/-/health` to become healthy, then read the initial root password:

```bash
docker-compose exec gitlab cat /etc/gitlab/initial_root_password
```

Use `http://localhost:8929` as both `GITLAB_API_URL` and `GITLAB_EXTERNAL_URL` for local Open Review tests unless
the GitLab container is exposed through a different host name.

## Docker Proxy

Image pulls are performed by the Docker daemon, so shell-level `HTTP_PROXY` is not enough. Configure the daemon with
the local proxy and restart Docker:

```bash
PROXY_URL=http://172.16.21.51:10808
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf >/dev/null <<EOF
[Service]
Environment="HTTP_PROXY=${PROXY_URL}"
Environment="HTTPS_PROXY=${PROXY_URL}"
Environment="NO_PROXY=localhost,127.0.0.1,*.local,10.*,172.16.*,172.17.*,172.18.*,172.19.*,172.2*,172.30.*,172.31.*,192.168.*"
EOF
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "max-concurrent-downloads": 1
}
EOF
sudo systemctl daemon-reload
sudo systemctl restart docker
docker info | sed -n '/Proxy:/,/Registry Mirrors:/p'
```

If `/etc/docker/daemon.json` already exists, merge the `max-concurrent-downloads` field instead of replacing the file.
Lowering concurrent downloads helps large images such as `gitlab/gitlab-ce` when the local proxy resets parallel layer
connections.
