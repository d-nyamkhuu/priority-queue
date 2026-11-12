# syntax=docker/dockerfile:1
# pqbench load generator: only httpx and PyYAML. Analysis (pandas/matplotlib)
# runs on your machine with `pip install 'pqbench[analysis]'`.
FROM python:3.15-alpine
# Install from a bind mount, so the source never lands in a layer, and without
# bytecode (pip's own included). /work holds ./results, owned by the non-root user.
RUN --mount=type=bind,target=/src,rw PYTHONDONTWRITEBYTECODE=1 pip install --no-cache-dir --no-compile /src \
 && install -d -o 65532 /work
WORKDIR /work
USER 65532
ENTRYPOINT ["pqbench"]
CMD ["--help"]
