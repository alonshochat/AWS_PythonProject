# Project CLI – AWS Resource Manager

## Overview
This project is a custom **AWS CLI** built in Python using [Click](https://click.palletsprojects.com/).  
It provides a safe, streamlined interface for working with **AWS EC2, S3, and Route53** services, while enforcing strict tagging policies for ownership and cleanup.

Every resource created through this CLI is automatically tagged with:
- `CreatedBy = project-cli`
- `Owner` (taken from `--owner`, defaults to the system username)
- `Project` (from `--project`)
- `Environment` (from `--env`)

### Why?
The enforced tagging ensures:
- **Accountability** – always know who created what
- **Cost visibility** – resources are grouped by project and environment
- **Safety** – prevents accidental deletion of resources not managed by this tool

---
## 🚀 Features

### EC2
- **Create** instances (`ec2 create <os> <instance_type>`)
  - Includes prompts for instance name, key pair generation
- **List** instances created by this CLI
- **Start** and **Stop** instances
- **Terminate** instances (safe, tag-scoped)
- **Generate key pairs** (`ec2 create <os> <instance_type>` auto-generates via prompt and saves a key pair)
- **Describe** instance details (IPs, tags, launch time)

### S3
- **Create** buckets (tagged + unique name validation)
  - choose visibility (public/private) via prompt
- **Upload** files
- **Empty** files from buckets
- **List** buckets (with size and object count)
- **Delete** buckets (safe, tag-scoped)

### Route53
- **List** hosted zones
- **List** records in a zone
- **Create**, **update**, and **delete** DNS records (A, CNAME, TXT, etc.)
- Safe operations — only tagged records are managed

### Global Status Command
- `project-cli status` – shows a summary of all resources created by this CLI across all services and regions.

---

## 🛠️ Requirements

- **Python 3.12+** and **Git** installed on your system
- AWS CLI installed (for `aws configure`)
- **AWS account** with valid credentials
  - Configure with `aws configure` or by using AWS profiles (`~/.aws/credentials`)
- **Dependencies** (installed automatically with `pip install -e .`):
  - `boto3` – AWS SDK for Python
  - `click` – for building the CLI
  - `botocore` – low-level AWS service definitions
- (Optional) **Virtual environment** recommended:
  ```
  python -m venv venv
  source venv/bin/activate   # Linux / Mac
  venv\Scripts\activate      # Windows

---

## 📦 Installation and Setup

Clone the repo and install in **editable mode**:

```
# clone repo:
git clone https://github.com/alonshochat/AWS_PythonProject.git
cd AWS_PythonProject-master

# install dependencies and the CLI:
pip install -e .

# install extra tools for development (tests, formatters, etc.):
pip install -r package_requirements.txt

# Verify installation
project-cli --help

# Set up AWS credentials (requires AWS CLI installed):
aws configure

```

---

## 🛠️ Usage

### After installation, you can use the CLI as follows (Examples):
* Each service has its own subcommands.
* Use `--help` for detailed usage of each command.
* Use `--examples` to see example commands.

### EC2
```
project-cli ec2 create ubuntu t3.micro --region us-east-1
project-cli ec2 list 
project-cli ec2 start (id or name from list)
project-cli ec2 stop (id or name from list)
project-cli ec2 describe (id or name from list), or --all for all
project-cli ec2 terminate (id or name from list)
```

### S3
```
project-cli s3 create my-bucket
project-cli s3 list
project-cli s3 upload my-bucket ./file.txt
project-cli s3 empty my-bucket 
project-cli s3 delete my-bucket
```

### Route53
```
# Zones
project-cli route53 list-zones
project-cli route53 create-zone example.com
project-cli route53 delete-zone ZONE_ID --yes

# Records
project-cli route53 list-records ZONE_ID
project-cli route53 create-record ZONE_ID NAME TYPE VALUE [TTL]
project-cli route53 update-record ZONE_ID NAME TYPE VALUE [TTL]

# Delete record (pick one)
project-cli route53 delete-record ZONE_ID NAME TYPE --auto
project-cli route53 delete-record ZONE_ID NAME TYPE VALUE [TTL]
project-cli route53 delete-record ZONE_ID NAME TXT "value" --value-only

```

---
## 🏷️ Tags
### All resources created by this CLI are tagged with:

- `CreatedBy = project-cli`
- `Owner` (from `--owner`, defaults to your username)
- `Project` (from `--project`)
- `Environment` (from `--env`)

This ensures clear ownership, cost tracking, and safe automation.

---

## 🧹 Cleanup

To avoid unexpected AWS costs, remove all resources created by this CLI when you’re done:

### EC2
```
# List all project-cli instances
project-cli ec2 list

# Terminate an instance
project-cli ec2 terminate (id or name from list)

# (Repeat for all instances)
``` 
### S3
```
# List all buckets
project-cli s3 list

# Delete/Empty objects from a bucket
project-cli s3 empty my-bucket

# Delete the bucket itself
project-cli s3 delete my-bucket

# (Repeat for all buckets)
```
### Route53
```
# List records
project-cli route53 list-zones
project-cli route53 list-records ZONE_ID

# Delete a record (pick one of the modes)
project-cli route53 delete-record ZONE_ID NAME TYPE --auto
# or
project-cli route53 delete-record ZONE_ID NAME TYPE VALUE [TTL]
# or
project-cli route53 delete-record ZONE_ID NAME TXT "value" --value-only

# Delete zone (must have no custom records)
project-cli route53 delete-zone ZONE_ID --yes

```
## Always double-check your AWS console to ensure all resources are deleted.

---

## ⚠️ Caution
- This tool **only manages resources it created** (tagged with `CreatedBy = project-cli`). It will not affect other resources in your AWS account.
- Always review commands before executing, especially delete/terminate operations.
- Ensure you have the necessary permissions in your AWS IAM policy to create, list, and delete the resources managed by this CLI.

---
