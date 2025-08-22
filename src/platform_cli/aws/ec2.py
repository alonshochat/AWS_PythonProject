# src/platform_cli/aws/ec2.py

from typing import Optional, Dict, List
import getpass
import traceback
import os
import stat

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
    """Count running/pending instances tagged CreatedBy=project-cli (hard cap enforcer)."""
    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)
    paginator = client.get_paginator("describe_instances")
    filters = [
        {"Name": "tag:CreatedBy", "Values": ["project-cli"]},
        {"Name": "instance-state-name", "Values": ["pending", "running"]},
    ]
    pages = paginator.paginate(Filters=filters)

    count = 0
    for r in pages:
        for res in r.get("Reservations", []):
            count += len(res.get("Instances", []))
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
        except Exception as e:
            last_err = e

    if last_err:
        raise last_err
    raise RuntimeError("Failed to resolve latest AMI")


def _safe_write_pem(filepath: str, key_material: str):
    """
    Write a PEM file with safe permissions. Overwrites if file exists.
    """
    out = os.path.abspath(os.path.expanduser(filepath))
    folder = os.path.dirname(out)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)

    with open(out, "w", encoding="utf-8") as f:
        f.write(key_material)
    try:
        os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except Exception:
        pass  # best effort on non-POSIX


def _run_instance(
    session: boto3.Session,
    region: Optional[str],
    ami_id: str,
    instance_type: str,
    owner: str,
    project: Optional[str],
    env: Optional[str],
    key_name: Optional[str] = None,
) -> Dict[str, str]:
    """Run a single instance with tags + Name (and optional KeyName)."""
    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    tags = build_tag_list(owner, project, env)
    name_value = f"{owner}-{(project or 'cli')}-{instance_type}"
    tags.append({"Key": "Name", "Value": name_value})

    run_args = dict(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
    )
    if key_name:
        run_args["KeyName"] = key_name

    resp = client.run_instances(**run_args)
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

    filters = [{"Name": "tag:CreatedBy", "Values": ["project-cli"]}]
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
                    name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), "")
                    click.echo(
                        f"{i['InstanceId']}\t{i['State']['Name']}\t{i['InstanceType']}\t{name}"
                    )

        if not found:
            click.echo("No instances found (CreatedBy=project-cli)")

    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Configure with a profile or role.", err=True)
        raise SystemExit(2)
    except EndpointConnectionError:
        click.echo(
            f"ERROR: could not reach EC2 endpoint in region '{effective_region}'. "
            f"Check your --region. Default is '{_effective_region(session, None)}'.",
            err=True,
        )
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (list_instances): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)


@ec2.command("create", context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument("os", required=False, type=click.Choice(["amazon-linux", "ubuntu"], case_sensitive=False))
@click.argument("instance_type", required=False, type=click.Choice(["t3.micro", "t2.small"], case_sensitive=False))
@click.option("--examples", is_flag=True, help="Show usage examples")
@click.option("--profile", default=None, help="AWS profile to use (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--owner", default=getpass.getuser(), show_default=True, help="Owner tag (defaults to current username)")
@click.option("--project", default=None, help="Project tag (optional)")
@click.option("--env", default=None, help="Environment tag (optional)")
# NEW key options:
@click.option("--key", "key_name", default=None, help="EC2 key pair name to use; if missing, it will be created and saved locally")
@click.option("--key-type", type=click.Choice(["ed25519", "rsa"], case_sensitive=False), default="ed25519", show_default=True, help="Key type when creating a new key pair")
@click.option("--save-key-to", default=None, help="Where to save a newly created private key (default: ~/.ssh/<key>.pem)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on error")
def create_instance(os, instance_type, examples, profile, region, owner, project, env, key_name, key_type, save_key_to, debug):
    """
    Create an EC2 instance with safeguards:

    - Only t3.micro or t2.small

    - Hard cap: <= 2 running instances created by this CLI

    - Latest AMI via SSM (Ubuntu or Amazon Linux 2)

    - Optional --key: use existing key or auto-create & save locally
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli ec2 create amazon-linux t3.micro --region us-east-1\n"
            "  project-cli ec2 create ubuntu t2.small --owner alice --project demo --env dev\n"
            "  project-cli ec2 create ubuntu t3.micro --profile myprofile\n"
            "  project-cli ec2 create ubuntu t3.micro --key my-dev-key --region us-east-1\n"
            "  project-cli ec2 create ubuntu t3.micro --key my-rsa --key-type rsa --save-key-to ~/.ssh/my-rsa.pem\n"
        )
        return

    if not os or not instance_type:
        click.echo(
            "Missing required arguments OS and INSTANCE_TYPE.\n"
            "Try 'project-cli ec2 create -h' for help.",
            err=True,
        )
        raise SystemExit(2)

    if instance_type not in ALLOWED_INSTANCE_TYPES:
        click.echo(f"ERROR: only {', '.join(sorted(ALLOWED_INSTANCE_TYPES))} are allowed.", err=True)
        raise SystemExit(2)

    os_key = "ubuntu" if os.lower() == "ubuntu" else "amzn"

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    # Hard cap check
    try:
        cap = _count_running_cli_instances(session, region)
    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Configure with a profile or role.", err=True)
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (describe_instances for cap check): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    if cap >= 2:
        click.echo("Instance cap reached (2 running instances). Stop/terminate one first.", err=True)
        raise SystemExit(2)

    # Resolve AMI
    try:
        ami_id = _resolve_latest_ami(session, region, os_key)
    except ClientError as e:
        click.echo(f"ERROR resolving AMI: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"ERROR resolving AMI: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    # Prepare key pair if requested
    key_to_use: Optional[str] = None
    if key_name:
        effective_region = _effective_region(session, region)
        ec2c = session.client("ec2", region_name=effective_region)
        try:
            # Does the key already exist?
            ec2c.describe_key_pairs(KeyNames=[key_name])
            key_to_use = key_name
            click.echo(f"Using existing key pair: {key_name} (region={effective_region})")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "InvalidKeyPair.NotFound":
                # Create it, tag it, save PEM
                try:
                    resp_kp = ec2c.create_key_pair(
                        KeyName=key_name,
                        KeyType=key_type.upper(),  # ED25519 or RSA
                        TagSpecifications=[{
                            "ResourceType": "key-pair",
                            "Tags": build_tag_list(owner, project, env),
                        }],
                    )
                    pem = resp_kp["KeyMaterial"]
                    out_path = save_key_to or os.path.expanduser(f"~/.ssh/{key_name}.pem")
                    _safe_write_pem(out_path, pem)
                    click.echo(f"Generated key pair '{key_name}' and saved PEM to {out_path}")
                    key_to_use = key_name
                except ClientError as ce:
                    click.echo(f"AWS error (create_key_pair): {ce}", err=True)
                    if debug:
                        traceback.print_exc()
                    raise SystemExit(2)
            else:
                click.echo(f"AWS error (describe_key_pairs): {e}", err=True)
                if debug:
                    traceback.print_exc()
                raise SystemExit(2)

    # Run instance
    try:
        result = _run_instance(
            session,
            region,
            ami_id=ami_id,
            instance_type=instance_type,
            owner=owner,
            project=project,
            env=env,
            key_name=key_to_use,
        )
        click.echo(
            f"Created {result['InstanceId']} ({instance_type}) "
            f"AMI={result['ImageId']} Name={result['Name']}"
            + (f" KeyName={key_to_use}" if key_to_use else "")
        )
    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Configure with a profile or role.", err=True)
        raise SystemExit(2)
    except EndpointConnectionError:
        effective_region = _effective_region(session, region)
        click.echo(
            f"ERROR: could not reach EC2 endpoint in region '{effective_region}'. "
            f"Check your --region. Default is '{_effective_region(session, None)}'.",
            err=True,
        )
        raise SystemExit(2)
    except ParamValidationError as e:
        click.echo(f"Parameter validation error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (run_instances): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)


# --- start/stop/terminate (restrict) ---
@ec2.command("start", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--examples", is_flag=True, help="Show usage examples")
@click.argument("instance_id", required=False)
@click.option("--profile", default=None, help="AWS profile to use (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def start_instance(examples, instance_id, profile, region, debug):
    """
    Start an EC2 instance (only if tagged CreatedBy=project-cli).
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli ec2 start i-0123456789abcdef0 --region us-east-1\n"
            "  project-cli ec2 start i-0abc... --profile myprofile\n"
        )
        return

    if not instance_id:
        click.echo("Missing required argument INSTANCE_ID.\nTry 'project-cli ec2 start -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    # Validate tag CreatedBy=project-cli
    try:
        resp = client.describe_instances(InstanceIds=[instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
        allowed = any(t["Key"] == "CreatedBy" and t["Value"] == "project-cli" for t in inst.get("Tags", []))
        if not allowed:
            click.echo("Refusing to start: instance not created by this CLI (CreatedBy!=project-cli).", err=True)
            raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (describe_instances): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    # Start
    try:
        client.start_instances(InstanceIds=[instance_id])
        click.echo(f"Start initiated for {instance_id} (region={effective_region})")

    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Configure with a profile or role.", err=True)
        raise SystemExit(2)
    except EndpointConnectionError:
        click.echo(
            f"ERROR: could not reach EC2 endpoint in region '{effective_region}'. "
            f"Check your --region. Default is '{_effective_region(session, None)}'.",
            err=True,
        )
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


@ec2.command("stop", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--examples", is_flag=True, help="Show usage examples")
@click.argument("instance_id", required=False)
@click.option("--profile", default=None, help="AWS profile to use (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--force", is_flag=True, help="Force stop (equivalent to hard power off)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def stop_instance(examples, instance_id, profile, region, force, debug):
    """
    Stop an EC2 instance (only if tagged CreatedBy=project-cli).
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli ec2 stop i-0123456789abcdef0 --region us-east-1\n"
            "  project-cli ec2 stop i-0abc... --profile myprofile\n"
            "  project-cli ec2 stop --force i-0123456789abcdef0\n"
        )
        return

    if not instance_id:
        click.echo("Missing required argument INSTANCE_ID.\nTry 'project-cli ec2 stop -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    # Validate tag CreatedBy=project-cli
    try:
        resp = client.describe_instances(InstanceIds=[instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
        allowed = any(t["Key"] == "CreatedBy" and t["Value"] == "project-cli" for t in inst.get("Tags", []))
        if not allowed:
            click.echo("Refusing to stop: instance not created by this CLI (CreatedBy!=project-cli).", err=True)
            raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (describe_instances): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    # Stop
    try:
        client.stop_instances(InstanceIds=[instance_id], Force=force)
        click.echo(f"Stop initiated for {instance_id} (region={effective_region})")

    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Configure with a profile or role.", err=True)
        raise SystemExit(2)
    except EndpointConnectionError:
        click.echo(
            f"ERROR: could not reach EC2 endpoint in region '{effective_region}'. "
            f"Check your --region. Default is '{_effective_region(session, None)}'.",
            err=True,
        )
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


@ec2.command("terminate", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--examples", is_flag=True, help="Show usage examples")
@click.argument("instance_ids", nargs=-1, required=False)
@click.option("--profile", default=None, help="AWS profile to use (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def terminate_instances(examples, instance_ids, profile, region, yes, debug):
    """
    Terminate one or more EC2 instances (only if tagged CreatedBy=project-cli).
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
        click.echo("Missing required argument(s) INSTANCE_ID...", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    # Validate all IDs are allowed and tagged properly
    allowed_ids: List[str] = []
    try:
        resp = client.describe_instances(InstanceIds=list(instance_ids))
        for r in resp.get("Reservations", []):
            for i in r.get("Instances", []):
                iid = i["InstanceId"]
                allowed = any(t["Key"] == "CreatedBy" and t["Value"] == "project-cli" for t in i.get("Tags", []))
                if allowed:
                    allowed_ids.append(iid)
                else:
                    click.echo(f"Refusing to terminate {iid}: not CreatedBy=project-cli", err=True)
    except ClientError as e:
        click.echo(f"AWS error (describe_instances): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    if not allowed_ids:
        click.echo("No terminable instances among the provided IDs.", err=True)
        raise SystemExit(2)

    # Confirmation
    if not yes:
        click.echo("About to terminate: " + " ".join(allowed_ids))
        confirm = click.prompt("Are you sure? (yes/no)", type=str, default="no")
        if confirm.strip().lower() not in {"y", "yes"}:
            click.echo("Aborted.")
            return

    # Terminate
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


@ec2.command("describe", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--examples", is_flag=True, help="Show usage examples")
@click.argument("instance_id", required=False)
@click.option("--profile", default=None, help="AWS profile to use (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def describe_instance(examples, instance_id, profile, region, debug):
    """
    Show details for an EC2 instance created by this CLI:
    - State, type, AZ, launch time
    - Public/Private IP & DNS
    - Name + all tags
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli ec2 describe i-0123456789abcdef0 --region us-east-1\n"
            "  project-cli ec2 describe i-0abc... --profile myprofile\n"
        )
        return

    if not instance_id:
        click.echo("Missing required argument INSTANCE_ID.\nTry 'project-cli ec2 describe -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    try:
        resp = client.describe_instances(InstanceIds=[instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
    except ClientError as e:
        click.echo(f"AWS error (describe_instances): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    # Enforce scope: only show instances created by this CLI
    if not any(t.get("Key") == "CreatedBy" and t.get("Value") == "project-cli" for t in inst.get("Tags", [])):
        click.echo("Refusing to describe: instance not created by this CLI (CreatedBy!=project-cli).", err=True)
        raise SystemExit(2)

    # Extract fields
    iid      = inst.get("InstanceId", "-")
    state    = inst.get("State", {}).get("Name", "-")
    itype    = inst.get("InstanceType", "-")
    az       = inst.get("Placement", {}).get("AvailabilityZone", "-")
    launch   = inst.get("LaunchTime")  # datetime
    launch_s = launch.strftime("%Y-%m-%d %H:%M:%S %Z") if hasattr(launch, "strftime") else str(launch)
    pub_ip   = inst.get("PublicIpAddress", "-")
    prv_ip   = inst.get("PrivateIpAddress", "-")
    pub_dns  = inst.get("PublicDnsName", "-")
    name_tag = next((t["Value"] for t in inst.get("Tags", []) if t.get("Key") == "Name"), "")

    click.echo(f"InstanceId:   {iid}")
    click.echo(f"State:        {state}")
    click.echo(f"Type:         {itype}")
    click.echo(f"AZ:           {az}")
    click.echo(f"LaunchTime:   {launch_s}")
    click.echo(f"Name:         {name_tag}")
    click.echo(f"PublicIP:     {pub_ip}")
    click.echo(f"PrivateIP:    {prv_ip}")
    click.echo(f"PublicDNS:    {pub_dns}")

    # Print tags (sorted by key)
    tags = sorted([(t.get('Key'), t.get('Value')) for t in inst.get("Tags", [])], key=lambda x: (x[0] or ""))
    if tags:
        click.echo("Tags:")
        for k, v in tags:
            click.echo(f"  {k}={v}")
    else:
        click.echo("Tags: (none)")
