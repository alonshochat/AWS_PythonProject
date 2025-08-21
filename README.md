# Project CLI – AWS Automation Tool

## Overview
A Python CLI built with [Click](https://click.palletsprojects.com/) to manage **AWS EC2, S3, and Route53** resources.  
All resources created are automatically **tagged** with:
- `CreatedBy = project-cli`
- `Owner` (from `--owner`, defaults to your username)
- `Project` (from `--project`)
- `Environment` (from `--env`)

This ensures clear ownership, cost tracking, and safe automation.

---

## Quick Start (Step-by-Step)

1. **Clone the repo**  
   ```bash
   git clone https://github.com/alonshochat/AWS_PythonProject.git
   cd AWS_PythonProject
