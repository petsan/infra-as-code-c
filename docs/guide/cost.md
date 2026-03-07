# Cost Estimation

The `--dry-run` flag includes an estimated monthly AWS cost breakdown per
environment.

```bash
infra-gen services.yaml --dry-run
```

## Pricing Model

Costs are calculated using fixed on-demand prices for `us-east-1`:

| Instance Type | Monthly Cost | Provisioned For |
|---------------|-------------|-----------------|
| `t3.micro` | $7.49 | One per replica (from `env_overrides`) |
| `db.t3.micro` | $12.25 | One per service with `db_type != none` |
| `cache.t3.micro` | $11.52 | One per service with `cache != none` |

## Calculation

For each service in each environment:

```
cost = (replicas x $7.49)
     + ($12.25 if db_type != none)
     + ($11.52 if cache != none)
```

## Example Output

```
=== Estimated Monthly AWS Costs ===

  DEV:
    api-gateway                    $   19.01/mo
    auth-service                   $   31.26/mo
    inventory-service              $   19.74/mo
    notification-service           $    7.49/mo
    order-service                  $   31.26/mo
    product-catalog                $   31.26/mo
                                     --------
    Subtotal                       $  140.02/mo

  STAGING:
    ...
    Subtotal                       $  177.47/mo

  PROD:
    ...
    Subtotal                       $  237.39/mo

  GRAND TOTAL                      $  554.88/mo
```

## Worked Example

For `auth-service` in the `dev` environment:

- `replicas: 1` -> 1 x $7.49 = $7.49
- `db_type: postgres` -> $12.25
- `cache: redis` -> $11.52
- **Total: $31.26/mo**

For the same service in `prod` with `replicas: 3`:

- `replicas: 3` -> 3 x $7.49 = $22.47
- `db_type: postgres` -> $12.25
- `cache: redis` -> $11.52
- **Total: $46.24/mo**

## Limitations

These estimates are for planning purposes only.  They do **not** include:

- Data transfer costs
- Storage IOPS
- Reserved Instance or Savings Plan discounts
- NAT Gateway charges
- ALB hourly costs and LCU charges
- CloudWatch, S3, or DynamoDB costs for state management
- Multi-AZ RDS or ElastiCache replication
