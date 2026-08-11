"""Assert every per-DB tool/chat service in docker-compose.yml ships with a
`SERVICE_PORT` value (either as a build arg or as an environment variable).

Background: `Dockerfile.service` uses `ARG SERVICE_PORT` then exports it as
`ENV SERVICE_PORT=${SERVICE_PORT}` and binds uvicorn to it. When a service is
recreated from an older image whose build skipped the arg, the container has
no SERVICE_PORT, uvicorn fails to bind, the healthcheck times out, and the
symptom looks like a runtime crash. See the `stale_image_env_gotcha` memory.

This lint catches that drift at PR time: it walks docker-compose.yml, picks
every `biochirp_*_tool` / `biochirp_*_chat` service whose Dockerfile is the
shared `Dockerfile.service`, and refuses any service whose build args AND
environment block both lack `SERVICE_PORT`.

Exit code 0 on success, 1 on the first violation (so the CI log surfaces the
exact service name).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "docker-compose.yml"
SHARED_DOCKERFILE_SUFFIX = "Dockerfile.service"


def _has_service_port(service: dict) -> bool:
    build = service.get("build") or {}
    if isinstance(build, dict):
        args = build.get("args") or {}
        if isinstance(args, dict) and "SERVICE_PORT" in {str(k) for k in args}:
            return True
        if isinstance(args, list) and any(
            str(a).split("=", 1)[0] == "SERVICE_PORT" for a in args
        ):
            return True
    env = service.get("environment") or {}
    if isinstance(env, dict) and "SERVICE_PORT" in {str(k) for k in env}:
        return True
    if isinstance(env, list) and any(
        str(e).split("=", 1)[0] == "SERVICE_PORT" for e in env
    ):
        return True
    return False


def _uses_shared_dockerfile(service: dict) -> bool:
    build = service.get("build")
    if not isinstance(build, dict):
        return False
    dockerfile = str(build.get("dockerfile") or "")
    return dockerfile.endswith(SHARED_DOCKERFILE_SUFFIX)


def main() -> int:
    if not COMPOSE_FILE.exists():
        print(f"::error::{COMPOSE_FILE} not found")
        return 1
    data = yaml.safe_load(COMPOSE_FILE.read_text()) or {}
    services = data.get("services") or {}
    violations: list[str] = []
    checked = 0
    for name, spec in services.items():
        if not isinstance(spec, dict):
            continue
        if not _uses_shared_dockerfile(spec):
            continue
        checked += 1
        if not _has_service_port(spec):
            violations.append(name)
    if violations:
        for name in violations:
            print(
                f"::error file=docker-compose.yml::service {name!r} uses the shared "
                "Dockerfile.service but declares no SERVICE_PORT (build arg or env). "
                "Add `args: { SERVICE_PORT: \"<port>\" }` under build, or set "
                "SERVICE_PORT under environment. See memory/stale_image_env_gotcha.md."
            )
        return 1
    print(f"SERVICE_PORT lint OK ({checked} services checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
