# src/platform_cli/aws/ec2.py

from typing import Optional, Dict, List
import getpass
import traceback

import click
import boto3
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


def _effective_region(session: boto3.Session, region: Optional[str]) -> str:
    """Pick a safe region: CLI option > profile default > us-east-1."""
    return region or session.region_name or "us-east-1"


def _count_running_cli_instances(session: boto3.Session, region: Optional[str]) -> int:
    """Count running instances created by this CLI (tag CreatedBy=project-cli)."""
    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)
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
    effective_region = _effective_region(session, region)
    ssm = session.client("ssm", region_name=effective_region)
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
) -> Dict[str, str]:
    """Run a single instance with tags."""
    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    tags = build_tag_list(owner, project, env)
    # Add a human-friendly Name so it shows in the EC2 console
    name_value = f"{owner}-{(project or 'cli')}-{instance_type}"
    tags.append({"Key": "Name", "Value": name_value})

    resp = client.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
    )
    inst = resp["Instances"][0]
    return {"InstanceId": inst["InstanceId"], "ImageId": ami_id, "Name": name_value}


# -----------------------------
# Commands
# -----------------------------

@ec2.command("list")
@click.option("--profile", default=None, help="AWS profile to use (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--owner", default=None, help="Filter by Owner tag (optional)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def list_instances(profile, region, owner, debug):
    """List EC2 instances created by this CLI (tagged CreatedBy=project-cli)."""
    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    filters = [{"Name": "tag:CreatedBy", "Values": [DEFAULT_TAGS["CreatedBy"]]}]
    if owner:
        filters.append({"Name": "tag:Owner", "Values": [owner]})

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
                    tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                    name = tags.get("Name", "(no-name)")
                    click.echo(f"{inst_id}\t{state}\t{itype}\t{name}")

        if not found:
            click.echo("No instances found (tag CreatedBy=project-cli).")

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


@ec2.command("create", context_settings=dict(help_option_names=["-h", "--help"]))
# Put --examples BEFORE positionals so it can short-circuit without args
@click.option("--examples", is_flag=True, help="Show usage examples and exit")
@click.argument("os_name", required=False, type=click.Choice(["amazon-linux", "ubuntu"], case_sensitive=False))
@click.argument("instance_type", required=False, type=click.Choice(sorted(ALLOWED_INSTANCE_TYPES)))
@click.option("--profile", default=None, help="AWS profile (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--owner", default=getpass.getuser(), show_default=True, help="Owner tag value")
@click.option("--project", default=None, help="Project tag")
@click.option("--env", default=None, help="Environment tag")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def create_instance(examples, os_name, instance_type, profile, region, owner, project, env, debug):
    """
    Create a single EC2 instance with required tags and safety checks.

    POSITIONAL:
      OS_NAME         amazon-linux|ubuntu
      INSTANCE_TYPE   t3.micro|t2.small
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli ec2 create amazon-linux t3.micro --region us-east-1\n"
            "  project-cli ec2 create ubuntu t2.small --owner alice --project demo --env dev\n"
        )
        return

    if not os_name or not instance_type:
        click.echo("ERROR: Missing required arguments OS_NAME and INSTANCE_TYPE.\nTry 'project-cli ec2 create -h' for help.", err=True)
        raise SystemExit(2)

    os_norm = os_name.lower()
    if os_norm == "amazon-linux":
        os_norm = "amzn"

    if instance_type not in ALLOWED_INSTANCE_TYPES:
        click.echo(f"ERROR: instance_type must be one of {sorted(ALLOWED_INSTANCE_TYPES)}", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    effective_region = _effective_region(session, region)

    try:
        running = _count_running_cli_instances(session, effective_region)
    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or set AWS_PROFILE.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (describe_instances): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    if running >= 2:
        click.echo(f"Limit reached: {running} running instances created by project-cli (max 2).", err=True)
        raise SystemExit(2)

    try:
        ami_id = _resolve_latest_ami(session, effective_region, os_norm)
    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or set AWS_PROFILE.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"AMI resolution failed: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    try:
        result = _run_instance(
            session=session,
            region=effective_region,
            ami_id=ami_id,
            instance_type=instance_type,
            owner=owner,
            project=project,
            env=env,
        )
    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or set AWS_PROFILE.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"EC2 RunInstances failed: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    click.echo(f"Instance created: {result['InstanceId']} (ImageId={result['ImageId']}) Name={result['Name']}")


# --- EC2 START (strict) ---
@ec2.command("start", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--examples", is_flag=True, help="Show usage examples and exit")
@click.argument("instance_id", required=False)
@click.option("--profile", default=None, help="AWS profile (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def start_instance(examples, instance_id, profile, region, debug):
    """
    Start an EC2 instance by ID, but ONLY if it was created by this CLI.

    POSITIONAL:
      INSTANCE_ID   e.g., i-0123456789abcdef0
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli ec2 start i-0123456789abcdef0 --region us-east-1\n"
            "  project-cli ec2 start i-0abc... --profile myprofile\n"
        )
        return

    if not instance_id:
        click.echo("ERROR: Missing required INSTANCE_ID.\nTry 'project-cli ec2 start -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    try:
        desc = client.describe_instances(InstanceIds=[instance_id])
        all_tags = {t["Key"]: t["Value"]
                    for r in desc.get("Reservations", [])
                    for i in r.get("Instances", [])
                    for t in i.get("Tags", [])}
        if all_tags.get("CreatedBy") != DEFAULT_TAGS["CreatedBy"]:
            click.echo("ERROR: Instance is not tagged CreatedBy=project-cli. Refusing to start.", err=True)
            raise SystemExit(2)

        client.start_instances(InstanceIds=[instance_id])
        click.echo(f"Start initiated for {instance_id} (region={effective_region})")

    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or use --profile.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (start_instances): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)


# --- EC2 STOP (strict) ---
@ec2.command("stop", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--examples", is_flag=True, help="Show usage examples and exit")
@click.argument("instance_id", required=False)
@click.option("--profile", default=None, help="AWS profile (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--force", is_flag=True, help="Force stop (equivalent to hard power-off)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def stop_instance(examples, instance_id, profile, region, force, debug):
    """
    Stop an EC2 instance by ID, but ONLY if it was created by this CLI.

    POSITIONAL:
      INSTANCE_ID   e.g., i-0123456789abcdef0
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli ec2 stop i-0123456789abcdef0 --region us-east-1\n"
            "  project-cli ec2 stop i-0abc... --force\n"
        )
        return

    if not instance_id:
        click.echo("ERROR: Missing required INSTANCE_ID.\nTry 'project-cli ec2 stop -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    try:
        desc = client.describe_instances(InstanceIds=[instance_id])
        all_tags = {t["Key"]: t["Value"]
                    for r in desc.get("Reservations", [])
                    for i in r.get("Instances", [])
                    for t in i.get("Tags", [])}
        if all_tags.get("CreatedBy") != DEFAULT_TAGS["CreatedBy"]:
            click.echo("ERROR: Instance is not tagged CreatedBy=project-cli. Refusing to stop.", err=True)
            raise SystemExit(2)

        client.stop_instances(InstanceIds=[instance_id], Force=bool(force))
        click.echo(f"Stop initiated for {instance_id} (region={effective_region}, force={bool(force)})")

    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or use --profile.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (stop_instances): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)


# --- EC2 TERMINATE (strict + confirm) ---
@ec2.command("terminate", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--examples", is_flag=True, help="Show usage examples and exit")
@click.argument("instance_ids", nargs=-1, required=False)  # allow multiple IDs
@click.option("--profile", default=None, help="AWS profile (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--yes", is_flag=True, help="Do not prompt for confirmation")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def terminate_instances(examples, instance_ids, profile, region, yes, debug):
    """
    Terminate one or more EC2 instances, but ONLY if they were created by this CLI.

    POSITIONAL:
      INSTANCE_IDS  one or more IDs, e.g. i-0123 i-0456
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli ec2 terminate i-0123456789abcdef0 --region us-east-1\n"
            "  project-cli ec2 terminate i-0123 i-0456 --profile myprofile\n"
            "  project-cli ec2 terminate --yes i-0abc...                       # no prompt\n"
        )
        return

    if not instance_ids:
        click.echo("ERROR: Missing required INSTANCE_IDS.\nTry 'project-cli ec2 terminate -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    # Validate tags: only allow instances with CreatedBy=project-cli
    try:
        desc = client.describe_instances(InstanceIds=list(instance_ids))
    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or use --profile.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (describe_instances): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    allowed_ids = []
    blocked_ids = []
    for r in desc.get("Reservations", []):
        for i in r.get("Instances", []):
            iid = i.get("InstanceId")
            tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
            if tags.get("CreatedBy") == DEFAULT_TAGS["CreatedBy"]:
                allowed_ids.append(iid)
            else:
                blocked_ids.append(iid)

    if blocked_ids:
        click.echo(
            "ERROR: The following instances are NOT tagged CreatedBy=project-cli and will NOT be terminated:\n  "
            + "  ".join(blocked_ids),
            err=True,
        )

    if not allowed_ids:
        click.echo("Nothing to terminate (no CLI-created instances in the provided list).")
        return

    if not yes:
        click.confirm(
            f"Terminate {len(allowed_ids)} instance(s): {' '.join(allowed_ids)} ?",
            abort=True
        )

    try:
        resp = client.terminate_instances(InstanceIds=allowed_ids)
        states = [
            f"{c['InstanceId']}:{c['CurrentState']['Name']}"
            for c in resp.get("TerminatingInstances", [])
        ]
        click.echo(f"Terminate requested (region={effective_region}): " + " ".join(states))
    except ClientError as e:
        click.echo(f"AWS error (terminate_instances): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
