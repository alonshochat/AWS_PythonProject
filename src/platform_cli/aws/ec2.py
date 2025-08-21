# src/platform_cli/aws/ec2.py

from typing import Optional, Dict, List

import click
import boto3
import traceback
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    EndpointConnectionError,
    ParamValidationError,
)

from platform_cli.config import DEFAULT_TAGS, build_tag_list

ALLOWED_INSTANCE_TYPES = {"t3.micro", "t2.small"}


@click.group()
def ec2():
    """EC2 commands."""
    pass


# -----------------------------
# Helpers
# -----------------------------

def _session_from(profile: Optional[str]):
    """Create a boto3 Session from a named profile or default environment."""
    if profile:
        return boto3.Session(profile_name=profile)
    return boto3.Session()


def _count_running_cli_instances(session: boto3.Session, region: Optional[str]) -> int:
    """Count running instances created by this CLI (tag CreatedBy=platform-cli)."""
    client = session.client("ec2", region_name=region)
    filters = [
        {"Name": "tag:CreatedBy", "Values": [DEFAULT_TAGS["CreatedBy"]]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ]
    resp = client.describe_instances(Filters=filters)
    count = 0
    for r in resp.get("Reservations", []):
        count += len(r.get("Instances", []))
    return count


def _resolve_latest_ami(session: boto3.Session, region: Optional[str], os_name: str) -> str:
    """
    Resolve latest AMI via public SSM parameter names.
    os_name: 'amzn' (Amazon Linux 2) or 'ubuntu'
    """
    ssm = session.client("ssm", region_name=region)
    candidates: List[str] = []
    if os_name == "ubuntu":
        # prefer 24.04 LTS; fallback to 22.04 LTS
        candidates = [
            "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
            "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id",
        ]
    else:
        # Amazon Linux 2 (gp3 then gp2)
        candidates = [
            "/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp3",
            "/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2",
        ]

    last_err: Optional[Exception] = None
    for name in candidates:
        try:
            val = ssm.get_parameter(Name=name)["Parameter"]["Value"]
            return val
        except ClientError as e:
            last_err = e
            continue
    raise RuntimeError(f"Could not resolve AMI via SSM (tried {candidates}): {last_err}")


def _run_instance(
    session: boto3.Session,
    region: Optional[str],
    ami_id: str,
    instance_type: str,
    owner: str,
    project: Optional[str],
    env: Optional[str],
    dry_run: bool,
) -> Dict[str, str]:
    """Run a single instance with tags. Supports EC2 DryRun."""
    client = session.client("ec2", region_name=region)
    tags = build_tag_list(owner, project, env)
    try:
        resp = client.run_instances(
            ImageId=ami_id,
            InstanceType=instance_type,
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
            DryRun=dry_run,
        )
    except ClientError as e:
        # DryRunOperation means permissions would allow it in a real call
        if "DryRunOperation" in str(e) and dry_run:
            return {"InstanceId": "(dry-run)", "ImageId": ami_id}
        raise
    if dry_run:
        return {"InstanceId": "(dry-run)", "ImageId": ami_id}
    inst = resp["Instances"][0]
    return {"InstanceId": inst["InstanceId"], "ImageId": ami_id}


# -----------------------------
# Commands
# -----------------------------

@ec2.command("list")
@click.option("--profile", default=None, help="AWS profile to use (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--owner", default=None, help="Filter by Owner tag (optional)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def list_instances(profile, region, owner, debug):
    """List EC2 instances created by this CLI (tagged CreatedBy=platform-cli)."""
    # 1) Session & region resolution (safe defaults)
    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    effective_region = region or session.region_name or "us-east-1"
    client = session.client("ec2", region_name=effective_region)

    # 2) Build filters (tagged by this CLI, optional owner)
    filters = [{"Name": "tag:CreatedBy", "Values": [DEFAULT_TAGS["CreatedBy"]]}]
    if owner:
        filters.append({"Name": "tag:Owner", "Values": [owner]})

    # 3) Call AWS with pagination & robust errors
    try:
        paginator = client.get_paginator("describe_instances")
        pages = paginator.paginate(Filters=filters)

        found = False
        for page in pages:
            for r in page.get("Reservations", []):
                for i in r.get("Instances", []):
                    found = True
                    inst_id = i.get("InstanceId", "?")
                    state = (i.get("State") or {}).get("Name", "?")
                    itype = i.get("InstanceType", "?")
                    click.echo(f"{inst_id}\t{state}\t{itype}")

        if not found:
            click.echo("No instances found (tag CreatedBy=platform-cli).")

    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or use --profile.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except EndpointConnectionError as e:
        click.echo(
            f"ERROR: Could not reach endpoint for region '{effective_region}'. "
            f"Check your --region. Detail: {e}",
            err=True,
        )
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except ParamValidationError as e:
        click.echo(f"ERROR: Invalid parameter(s) for describe_instances: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)


@ec2.command("create")
@click.option("--profile", default=None, help="AWS profile (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--owner", required=True, help="Owner tag value")
@click.option("--project", default=None, help="Project tag")
@click.option("--env", default=None, help="Environment tag")
@click.option(
    "--instance-type",
    "instance_type",
    type=click.Choice(sorted(ALLOWED_INSTANCE_TYPES)),
    required=True,
    help="Allowed: t3.micro, t2.small",
)
@click.option(
    "--os",
    "os_name",
    type=click.Choice(["amzn", "ubuntu"]),
    default="amzn",
    help="Base image: amzn (Amazon Linux 2) or ubuntu",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=False,
    help="Show what would happen without calling AWS",
)
def create_instance(profile, region, owner, project, env, instance_type, os_name, dry_run):
    """
    Create a single EC2 instance with required tags and safety checks:
    - instance type whitelist
    - max 2 running instances created by this CLI
    - resolve latest AMI via SSM
    """
    # Local validation (extra guard; click.Choice already restricts values)
    if instance_type not in ALLOWED_INSTANCE_TYPES:
        click.echo(f"ERROR: instance_type must be one of {sorted(ALLOWED_INSTANCE_TYPES)}", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    # Enforce running-instance cap
    try:
        running = _count_running_cli_instances(session, region)
    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or set AWS_PROFILE.", err=True)
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (describe_instances): {e}", err=True)
        raise SystemExit(2)

    if running >= 2:
        click.echo(f"Limit reached: {running} running instances created by platform-cli (max 2).", err=True)
        raise SystemExit(2)

    # Resolve AMI
    try:
        ami_id = _resolve_latest_ami(session, region, os_name)
    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or set AWS_PROFILE.", err=True)
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"AMI resolution failed: {e}", err=True)
        raise SystemExit(2)

    # Create instance (or dry-run)
    try:
        result = _run_instance(
            session=session,
            region=region,
            ami_id=ami_id,
            instance_type=instance_type,
            owner=owner,
            project=project,
            env=env,
            dry_run=dry_run,
        )
    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or set AWS_PROFILE.", err=True)
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"EC2 RunInstances failed: {e}", err=True)
        raise SystemExit(2)

    if dry_run:
        click.echo("[DRY-RUN] Would create instance:")
        click.echo(f"[DRY-RUN]   ImageId={ami_id}")
        click.echo(f"[DRY-RUN]   InstanceType={instance_type}")
        click.echo(
            f"[DRY-RUN]   Tags=CreatedBy={DEFAULT_TAGS['CreatedBy']}, "
            f"Owner={owner}, Project={project}, Environment={env}"
        )
        return

    click.echo(f"Instance created: {result['InstanceId']} (ImageId={result['ImageId']})")
