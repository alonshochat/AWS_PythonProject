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
- **Create** instances (`ec2 create <os> <instance_type> [--key <keyname>]`)
- **List** instances created by this CLI
- **Terminate** instances
- **Generate key pairs** (`ec2 create ... --key <name>` automatically creates or reuses a key)
- (Optional) **Describe** instance details (IPs, tags, launch time)

### S3
- **Create** buckets (tagged + unique name validation)
- **Upload** files
- **Empty** files from buckets
- **List** buckets and contents
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

### EC2
```
project-cli ec2 create ubuntu t3.micro --region us-east-1
project-cli ec2 list 
project-cli ec2 terminate i-0123456789abcdef0 (id or name from list)
```

### S3
```
project-cli s3 create my-bucket --region us-east-1
project-cli s3 upload my-bucket ./file.txt
project-cli s3 empty my-bucket 
project-cli s3 delete my-bucket
```

### Route53
```
project-cli route53 list-zones
project-cli route53 list-records Z123456ABCDEFG
project-cli route53 create-record Z123456ABCDEFG --type A --name test.example.com --value 1.2.3.4 --ttl 300
project-cli route53 delete-record Z123456ABCDEFG --type A --name test.example.com --value
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
project-cli ec2 list --region us-east-1

# Terminate an instance
project-cli ec2 terminate i-0123456789abcdef0 --region us-east-1

# (Repeat for all instances)
``` 
### S3
```
# List all buckets
project-cli s3 list --region us-east-1

# Delete objects from a bucket
project-cli s3 delete-object my-bucket file.txt --region us-east-1

# Delete the bucket itself
project-cli s3 delete-bucket my-bucket --region us-east-1

# (Repeat for all buckets)
```
### Route53
```
# List records
project-cli route53 records Z123456ABCDEFG --region us-east-1

# Delete a record
project-cli route53 delete-record Z123456ABCDEFG --type A --name test.example.com --value 1.2.3.4 --ttl 300

# (Repeat for all records)
```
## Always double-check your AWS console to ensure all resources are deleted.

---

## ⚠️ Caution
- This tool **only manages resources it created** (tagged with `CreatedBy = project-cli`). It will not affect other resources in your AWS account.
- Always review commands before executing, especially delete/terminate operations.
- Ensure you have the necessary permissions in your AWS IAM policy to create, list, and delete the resources managed by this CLI.

---
