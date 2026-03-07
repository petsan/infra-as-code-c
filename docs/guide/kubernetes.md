# Kubernetes Output

infra-gen produces multi-document YAML files under
`<output>/kubernetes/<env>/`, one file per service.

## Directory Structure

```
output/kubernetes/
  dev/
    api-gateway.yaml
    auth-service.yaml
    ...
  staging/
    ...
  prod/
    ...
```

## Resources per Service

Each `<service>.yaml` contains four Kubernetes resources separated by `---`
(five if the service declares secrets):

### 1. Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-gateway
  namespace: prod
  labels:
    app: api-gateway
    environment: prod
    exposure: external
```

**Pod scheduling:**

- **Anti-affinity**: pods prefer to spread across different nodes
  (`preferredDuringSchedulingIgnoredDuringExecution` with weight 100 on
  `kubernetes.io/hostname`).
- **Topology spread constraints**: maximum 2 pods per availability zone
  (`topology.kubernetes.io/zone`, `maxSkew: 2`,
  `whenUnsatisfiable: DoNotSchedule`).

**Probes:**

| Probe | Type | When | Timing |
|-------|------|------|--------|
| **Readiness** | HTTP GET | `health_check_path` is set | `initialDelay=5s`, `period=10s`, `timeout=3s`, `failureThreshold=3` |
| **Readiness** | TCP Socket | `health_check_path` is not set | same timing |
| **Liveness** | TCP Socket | **always** (never HTTP) | `initialDelay=15s`, `period=20s`, `timeout=5s`, `failureThreshold=5` |

!!! info "Liveness vs Readiness"
    Liveness probes are deliberately different from readiness probes: they
    always use TCP (even when readiness uses HTTP) and have longer intervals.
    This prevents liveness failures from cascading during slow startups.

**Resource limits:**

```yaml
resources:
  requests: { cpu: "1000m", memory: "128Mi" }
  limits:   { cpu: "1000m", memory: "256Mi" }
```

CPU values come from `env_overrides`.  Memory defaults are fixed at
128Mi request / 256Mi limit.

### 2. Service

A `ClusterIP` service exposing the container port:

```yaml
apiVersion: v1
kind: Service
spec:
  selector: { app: api-gateway }
  ports:
    - port: 8080
      targetPort: 8080
      protocol: TCP
  type: ClusterIP
```

### 3. NetworkPolicy

Enforces the internal/external distinction at the network level.

**Internal services:**

```yaml
ingress:
  - from:
      - podSelector:
          matchExpressions:
            - key: exposure
              operator: NotIn
              values: [external]
    ports:
      - port: 8080
        protocol: TCP
```

This rule rejects traffic from any pod labeled `exposure: external`,
ensuring internal services are only reachable from other internal pods and
their declared dependents.

**External services:**

```yaml
ingress:
  - from:
      - podSelector: {}    # all pods in namespace
    ports:
      - port: 8080
```

**Egress** is limited to declared dependencies plus DNS (port 53 UDP/TCP).

### 4. HorizontalPodAutoscaler

Scales on **both** CPU and memory:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
spec:
  minReplicas: 4    # from env_overrides.prod.replicas
  maxReplicas: 12   # 3x minReplicas (minimum 3)
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: { type: Utilization, averageUtilization: 70 }
    - type: Resource
      resource:
        name: memory
        target: { type: Utilization, averageUtilization: 80 }
```

### 5. Secret (when `secrets` is non-empty)

An Opaque Secret resource containing all declared secret names as keys with
base64-encoded placeholder values:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: auth-service-secrets
  namespace: prod
  labels:
    app: auth-service
    environment: prod
    exposure: internal
type: Opaque
data:
  DB_PASSWORD: Q0hBTkdFX01F        # base64("CHANGE_ME")
  OAUTH_CLIENT_SECRET: Q0hBTkdFX01F
```

The Deployment container automatically gets an `envFrom` entry:

```yaml
envFrom:
  - secretRef:
      name: auth-service-secrets
```

This injects all secret keys as environment variables.  Replace the
placeholder values before applying -- see [Secrets Vault](secrets.md) for
the full workflow.

Services without secrets do **not** receive a Secret resource or `envFrom`.

## Labels and Annotations

**Labels** (on all resources):

| Label | Value |
|-------|-------|
| `app` | Service name |
| `environment` | `dev` / `staging` / `prod` |
| `exposure` | `internal` / `external` |
| `peer-group` | Sorted peer names (only for peer services) |

**Annotations** (on Deployments):

| Annotation | Value |
|------------|-------|
| `dependency-hash` | SHA-256 of sorted dependency list |
| `last-generated` | ISO-8601 UTC timestamp |

## Applying

```bash
# Apply all manifests for an environment
kubectl apply -f output/kubernetes/prod/

# Apply a single service
kubectl apply -f output/kubernetes/prod/api-gateway.yaml
```
