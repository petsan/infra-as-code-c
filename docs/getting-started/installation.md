# Installation

## Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| **Python** | >= 3.10 | Runtime for infra-gen |
| **pip** | any recent | Package installer |
| **Terraform** | >= 1.5 | To apply the generated `.tf.json` files |
| **kubectl** | >= 1.27 | To apply the generated Kubernetes YAML |
| **AWS CLI** | >= 2.0 | To provision S3 state buckets and DynamoDB lock tables |

!!! note
    Terraform, kubectl, and the AWS CLI are only needed to **apply** the generated files.  infra-gen itself only requires Python and PyYAML.

## Install from Source (recommended for development)

```bash
# Clone the repository
git clone https://github.com/your-org/infra-gen.git
cd infra-gen

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Linux / macOS
# .venv\Scripts\activate    # Windows

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Verify
infra-gen --version
```

## Install from pip

```bash
pip install infra-gen
```

## Install the Man Page

The repository includes a Unix man page at `man/infra-gen.1`.  To install it system-wide:

```bash
# Copy to the local man directory
sudo cp man/infra-gen.1 /usr/local/share/man/man1/

# Rebuild the man database
sudo mandb

# Test
man infra-gen
```

Alternatively, read it directly without installing:

```bash
man ./man/infra-gen.1
```

## Build the Documentation Site

The documentation is built with [MkDocs](https://www.mkdocs.org/) and the
[Material for MkDocs](https://squidfunnel.github.io/mkdocs-material/) theme.

```bash
# Install docs dependencies
pip install mkdocs mkdocs-material mkdocstrings[python]

# Serve locally with live reload
mkdocs serve

# Build static HTML into site/
mkdocs build
```

## Run Tests

```bash
pip install pytest
python -m pytest tests/ -v
```

## AWS Prerequisites for Applying Generated Terraform

Before running `terraform init` on the generated files, create the S3 state
bucket and DynamoDB lock table for each environment:

```bash
for ENV in dev staging prod; do
  # Create state bucket
  aws s3api create-bucket \
    --bucket "terraform-state-${ENV}" \
    --region us-east-1

  # Enable versioning
  aws s3api put-bucket-versioning \
    --bucket "terraform-state-${ENV}" \
    --versioning-configuration Status=Enabled

  # Create lock table
  aws dynamodb create-table \
    --table-name "terraform-locks-${ENV}" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region us-east-1
done
```

## Kubernetes Prerequisites for Applying Generated Manifests

Create the namespaces that correspond to the target environments:

```bash
for ENV in dev staging prod; do
  kubectl create namespace "${ENV}" --dry-run=client -o yaml | kubectl apply -f -
done
```
