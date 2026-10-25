"""Real, disposable PG/role/migration/static/HTTPS smoke; no production credentials.

Run after docker build: python3 -m deploy.ci.container_smoke fishsite:ci
All resources have a random prefix. Never reads .env or connects to the VPS.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


def main():
    image = sys.argv[1]
    prefix = "fishsite-smoke-" + uuid.uuid4().hex[:10]
    folder = Path(".test-deployment") / prefix
    folder.mkdir(parents=True, mode=0o700)
    containers = []
    volume = prefix + "-static"
    sha = "a" * 40
    environment = {k: os.environ[k] for k in ("HOME", "PATH", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG") if k in os.environ}

    def run(*args, input=None, check=True):
        result = subprocess.run(args, input=input, text=True, capture_output=True, env=environment, timeout=180)
        if check and result.returncode:
            raise RuntimeError(f"Synthetic command failed: {args[0:3]}\n{result.stderr[-3000:]}")
        return result

    def container(name, *args):
        containers.append(name)
        return run("docker", "run", "--name", name, *args)

    def app_environment(role="fishsite_owner"):
        values = {
            "FISHSITE_SKIP_DOTENV": "1", "DJANGO_SECRET_KEY": "synthetic-deployment-only-key",
            "FISHSITE_RELEASE_SHA": sha, "DJANGO_ALLOWED_HOSTS": "localhost",
            "DJANGO_DB_HOST": "db", "DJANGO_DB_NAME": "fishsite", "DJANGO_DB_USER": role,
            "DJANGO_DB_PASSWORD": "synthetic-db-only", "DJANGO_TRUST_X_FORWARDED_PROTO": "true",
            "DJANGO_SECURE_SSL_REDIRECT": "true", "DJANGO_SESSION_COOKIE_SECURE": "true",
            "DJANGO_CSRF_COOKIE_SECURE": "true", "MOYSKLAD_ENVIRONMENT": "test",
            "NOTIFICATION_BACKEND": "fishsite.test_safety.NoopNotificationBackend",
        }
        return [item for key, value in values.items() for item in ("-e", key + "=" + value)]

    try:
        run("docker", "network", "create", prefix)
        run("docker", "volume", "create", volume)
        container(prefix + "-db", "-d", "--network", prefix, "--network-alias", "db",
                  "--tmpfs", "/var/lib/postgresql", "-e", "POSTGRES_PASSWORD=synthetic-admin-only", "postgres:18.6")
        for _ in range(60):
            if run("docker", "exec", prefix + "-db", "pg_isready", "-U", "postgres", check=False).returncode == 0:
                break
            time.sleep(1)
        run("docker", "exec", "-i", prefix + "-db", "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", input=r"""
CREATE ROLE fishsite_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD 'synthetic-db-only';
CREATE ROLE fishsite_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD 'synthetic-db-only';
CREATE DATABASE fishsite OWNER fishsite_owner;
\connect fishsite
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO fishsite_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE fishsite_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fishsite_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE fishsite_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO fishsite_runtime;
""")
        # The real migration entry point must reject runtime credentials before DDL.
        rejected = run("docker", "run", "--rm", "--network", prefix, *app_environment("fishsite_runtime"),
                       image, "python", "-m", "deploy.container", "migrate", check=False)
        if rejected.returncode == 0 or "schema-owner role" not in rejected.stderr:
            raise RuntimeError("Migration role guard did not reject runtime credentials")
        # Initial schema creation is separate from updating an existing production
        # DB; historic data/destructive migrations are not an automatic upgrade.
        run("docker", "run", "--rm", "--network", prefix, *app_environment(), image,
            "python", "manage.py", "migrate", "--noinput")
        run("docker", "run", "--rm", "--network", prefix, *app_environment(), image,
            "python", "-m", "deploy.container", "migrate")
        run("docker", "run", "--rm", "--network", prefix, *app_environment(), image,
            "python", "manage.py", "shell", "-c",
            "from catalog.models import Category, Product; "
            "c=Category.objects.create(name='Synthetic fish', slug='synthetic-fish'); "
            "Product.objects.create(category=c, name='Synthetic product', slug='synthetic-product', price_per_kg='100.00', is_published=True)")
        run("docker", "run", "--rm", "--network", prefix, *app_environment(), "-v", volume + ":/app/staticfiles",
            image, "python", "-m", "deploy.container", "static")
        container(prefix + "-web", "-d", "--network", prefix, "--network-alias", "web",
                  *app_environment("fishsite_runtime"), "-v", volume + ":/app/staticfiles:ro", image)
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost", "-keyout", str(folder / "key.pem"), "-out", str(folder / "cert.pem"))
        (folder / "nginx.conf").write_text("""
events {}
http {
  server {
    listen 443 ssl;
    ssl_certificate /test/cert.pem;
    ssl_certificate_key /test/key.pem;
    location /static/ { alias /static/; }
    location / {
      proxy_pass http://web:8000;
      proxy_connect_timeout 1s;
      proxy_read_timeout 2s;
      proxy_set_header Host localhost;
      proxy_set_header X-Forwarded-Proto https;
    }
  }
}
""")
        container(prefix + "-nginx", "-d", "--network", prefix, "-p", "127.0.0.1::443",
                  "-v", str(folder.resolve()) + ":/test:ro", "-v", volume + ":/static:ro",
                  "nginx:stable-alpine", "nginx", "-c", "/test/nginx.conf", "-g", "daemon off;")
        mapping = run("docker", "port", prefix + "-nginx", "443/tcp").stdout.strip()
        port = int(mapping.rsplit(":", 1)[1])
        context = ssl.create_default_context(cafile=str(folder / "cert.pem"))
        def get(path):
            with urllib.request.urlopen(f"https://localhost:{port}" + path, context=context, timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError("Expected exact 200")
                return response.read()
        for attempt in range(30):
            try:
                if json.loads(get("/healthz/")) != {"status": "ok", "sha": sha}:
                    raise RuntimeError("Wrong release identity")
                break
            except (OSError, urllib.error.URLError):
                if attempt == 29:
                    raise
                time.sleep(1)
        for path in ("/", "/catalog/categories/synthetic-fish/"):
            if b"</html>" not in get(path).lower():
                raise RuntimeError("Missing HTML")
        static = json.loads(get("/static/release.json"))
        if static["sha"] != sha or hashlib.sha256(get("/static/" + static["asset"])).hexdigest() != static["sha256"]:
            raise RuntimeError("Static release checksum mismatch")
        # Inject unavailability only in this synthetic stack, then prove recovery.
        run("docker", "stop", prefix + "-web")
        try:
            get("/healthz/")
        except urllib.error.HTTPError as error:
            if error.code not in {502, 504}:
                raise
        else:
            raise RuntimeError("Unavailable application was accepted as healthy")
        run("docker", "start", prefix + "-web")
        run("docker", "exec", prefix + "-nginx", "nginx", "-c", "/test/nginx.conf", "-s", "reload")
        for attempt in range(30):
            try:
                if json.loads(get("/healthz/"))["sha"] != sha:
                    raise RuntimeError("Recovery returned the wrong SHA")
                break
            except (OSError, urllib.error.URLError):
                if attempt == 29:
                    raise
                time.sleep(1)
        print("Synthetic PostgreSQL roles, migrations, HTTPS, pages, static, outage and recovery passed")
    finally:
        for name in reversed(containers):
            run("docker", "rm", "-f", "-v", name, check=False)
        run("docker", "volume", "rm", volume, check=False)
        run("docker", "network", "rm", prefix, check=False)
        shutil.rmtree(folder)


if __name__ == "__main__":
    main()
