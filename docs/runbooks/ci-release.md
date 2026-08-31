# CI and immutable image release

The `ci` workflow is the only image publisher. Pull requests and `main` pushes run formatting,
linting, strict typing, the complete test suite against PostgreSQL, both controlled benchmarks,
dependency audit, Prometheus rule validation, a container smoke test, and a HIGH/CRITICAL image
scan. A `main` push publishes only after both gate jobs pass.

Hosted quality checks use the Ubuntu runner's system Python 3.12 because uv does not publish every
CPython patch build for every platform. The runtime image remains pinned separately to Python
3.12.14 by digest, so the shipped interpreter is reproducible.

Controlled quality metrics, hashes, indexes, and policy decisions must reproduce exactly. Timing
is hardware-dependent, so both independent runs must satisfy the release contract's 500 ms p95
ceiling instead of matching a developer laptop within a fixed millisecond delta.

Published images use only `ghcr.io/<owner>/<repository>:sha-<40-character-commit>` for deployment.
The workflow emits a registry-backed GitHub artifact attestation and signs the immutable digest
with Sigstore keyless signing. Deployments must resolve and store that digest; mutable tags are
not release inputs.

## Required repository rule

Configure a GitHub ruleset for `main` that:

- requires pull requests and at least one approval;
- requires `quality` and `container` status checks to pass and be current;
- blocks force pushes and branch deletion;
- requires conversation resolution; and
- prevents bypass, including administrators, except an audited break-glass role.

Until this ruleset and one green hosted run are independently visible, T16 is not considered
fully proven even when every local check passes.

The production deploy job is skipped until the repository variable `PUBLIC_HOSTNAME` is set. This
allows a new repository to prove and publish its immutable image without targeting absent
infrastructure. Adding the variable activates the protected production-environment path, whose
runtime secret and kubeconfig checks continue to fail closed.

## Verification

Verify an image before deployment:

```sh
cosign verify --certificate-identity-regexp '^https://github.com/.+/.github/workflows/ci.yml@refs/heads/main$' --certificate-oidc-issuer https://token.actions.githubusercontent.com IMAGE@DIGEST
gh attestation verify IMAGE@DIGEST --repo OWNER/REPOSITORY
```

If any gate or publication step fails, no deployable digest is handed to the rollout workflow;
the running application and MLflow policy aliases remain unchanged.
