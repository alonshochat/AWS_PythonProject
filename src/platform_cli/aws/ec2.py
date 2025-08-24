# src/platform_cli/aws/ec2.py

from typing import Optional, Dict, List, Tuple
import getpass
import traceback
import os
import sys
import stat
import re

import click
import boto3
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    EndpointConnectionError,
    ParamValidationError,
)

from platform_cli.config import build_tag_list

ALLOWED_INSTANCE_TYPES = {"t3.micro", "t2.small"}
_ID_RE = re.compile(r"^i-[a-f0-9]{8,}$", re.IGNORECASE)


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


def _resolve_instance_name(default_name: str, provided_name: Optional[str], no_prompt: bool) -> str:
    """
    Decide final instance Name:
      - if provided_name is given, use it
      - else if no_prompt or stdin not a tty, use default_name
      - else prompt once with default shown
    """
    if provided_name:
        return provided_name.strip()
    try:
        if no_prompt or not sys.stdin.isatty():
            return default_name
        value = click.prompt("Instance name", default=default_name, show_default=True)
        return (value or default_name).strip()
    except Exception:
        return default_name


def _prompt_key_pair(
    session: boto3.Session,
    region: Optional[str],
    owner: str,
    project: Optional[str],
    env: Optional[str],
    no_prompt: bool,
) -> Optional[str]:
    """
    Interactive key-pair resolver:
      - if no_prompt or non-tty: return None (no KeyName)
      - else prompt for a key name (blank for none)
      - if name exists -> use it
      - if not found -> ask to create; if yes, prompt for type + save path, create & tag, save PEM; return name
    """
    effective_region = _effective_region(session, region)
    ec2c = session.client("ec2", region_name=effective_region)

    # CI-safe / non-interactive -> no key
    if no_prompt or not sys.stdin.isatty():
        return None

    try:
        key_name = click.prompt("EC2 key pair name (leave blank for none)", default="", show_default=False).strip()
    except Exception:
        return None

    if not key_name:
        return None

    # Exists?
    try:
        ec2c.describe_key_pairs(KeyNames=[key_name])
        click.echo(f"Using existing key pair: {key_name} (region={effective_region})")
        return key_name
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code != "InvalidKeyPair.NotFound":
            raise

    # Not found -> offer to create
    if not click.confirm(f"Key pair '{key_name}' not found. Create it now?", default=True):
        click.echo("Proceeding without a key pair.")
        return None

    # Choose type
    key_type = click.prompt(
        "Key type",
        type=click.Choice(["ed25519", "rsa"], case_sensitive=False),
        default="ed25519",
        show_choices=True,
        show_default=True,
    ).lower()

    save_default = os.path.expanduser(f"~/.ssh/{key_name}.pem")
    save_path = click.prompt("Save private key PEM to", default=save_default, show_default=True)

    try:
        resp_kp = ec2c.create_key_pair(
            KeyName=key_name,
            KeyType=key_type,  # API expects 'ed25519' or 'rsa'
            TagSpecifications=[{
                "ResourceType": "key-pair",
                "Tags": build_tag_list(owner, project, env),
            }],
        )
        pem = resp_kp["KeyMaterial"]
        _safe_write_pem(save_path, pem)
        click.echo(f"Generated key pair '{key_name}' and saved PEM to {save_path}")
        return key_name
    except ClientError as ce:
        click.echo(f"AWS error (create_key_pair): {ce}", err=True)
        raise


def _run_instance(
    session: boto3.Session,
    region: Optional[str],
    ami_id: str,
    instance_type: str,
    owner: str,
    project: Optional[str],
    env: Optional[str],
    key_name: Optional[str] = None,
    *,
    resolved_name: Optional[str] = None,
) -> Dict[str, str]:
    """Run a single instance with tags + Name (and optional KeyName)."""
    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    tags = build_tag_list(owner, project, env)
    name_value = resolved_name or f"{owner}-{(project or 'cli')}-{instance_type}"
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
# ID/Name resolution utilities
# -----------------------------

def _resolve_name_to_ids(session: boto3.Session, region: Optional[str], name: str) -> List[str]:
    """Resolve a Name tag to instance IDs (scoped to CreatedBy=project-cli)."""
    effective_region = _effective_region(session, region)
    ec2c = session.client("ec2", region_name=effective_region)
    filters = [
        {"Name": "tag:CreatedBy", "Values": ["project-cli"]},
        {"Name": "tag:Name", "Values": [name]},
    ]
    resp = ec2c.describe_instances(Filters=filters)
    ids: List[str] = []
    for r in resp.get("Reservations", []):
        for i in r.get("Instances", []):
            ids.append(i["InstanceId"])
    return ids


def _resolve_tokens_to_instance_ids(session: boto3.Session, region: Optional[str], tokens: List[str]) -> Tuple[List[str], List[str], Dict[str, List[str]]]:
    """
    Accept a mixed list of tokens (instance IDs or Name tag values) and return:
      - resolved_ids: List[str]
      - not_found_names: List[str]
      - name_map: Dict[str, List[str]]  (name -> ids matched)
    All lookups are scoped to CreatedBy=project-cli.
    """
    ids: List[str] = []
    names: List[str] = []
    for t in tokens:
        if _ID_RE.match(t):
            ids.append(t)
        else:
            names.append(t)

    name_map: Dict[str, List[str]] = {}
    not_found: List[str] = []

    for nm in names:
        matched_ids = _resolve_name_to_ids(session, region, nm)
        if matched_ids:
            name_map[nm] = matched_ids
            ids.extend(matched_ids)
        else:
            not_found.append(nm)

    # De-dupe while preserving order
    seen = set()
    resolved_ids: List[str] = []
    for iid in ids:
        if iid not in seen:
            resolved_ids.append(iid)
            seen.add(iid)

    return resolved_ids, not_found, name_map


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
# name / interaction options:
@click.option("--name", default=None, help="Instance Name tag; if omitted, you will be prompted with a default.")
@click.option("--no-prompt", is_flag=True, help="Disable interactive prompts (CI-safe); use defaults (no key).")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on error")
def create_instance(os, instance_type, examples, profile, region, owner, project, env, name, no_prompt, debug):
    """
    Create an EC2 instance with safeguards:

    - Only t3.micro or t2.small
    - Hard cap: <= 2 running instances created by this CLI
    - Latest AMI via SSM (Ubuntu or Amazon Linux 2)
    - Interactive Name prompt (default: owner-project-instanceType)
    - Interactive Key Pair prompt (or none in --no-prompt mode)
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli ec2 create amazon-linux t3.micro --region us-east-1\n"
            "  project-cli ec2 create ubuntu t2.small --owner alice --project demo --env dev\n"
            "  project-cli ec2 create ubuntu t3.micro --profile myprofile\n"
            "  project-cli ec2 create amazon-linux t2.small --name my-api           # skip name prompt\n"
            "  project-cli ec2 create ubuntu t3.micro --no-prompt              # CI-safe; uses default name, NO key\n"
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

    # Resolve final Name (prompt/default/explicit)
    default_name = f"{owner}-{(project or 'cli')}-{instance_type}"
    final_name = _resolve_instance_name(default_name, name, no_prompt)

    # Interactive key-pair selection / create (or none in no-prompt)
    key_to_use: Optional[str] = None
    try:
        key_to_use = _prompt_key_pair(session, region, owner, project, env, no_prompt)
    except ClientError as e:
        click.echo(f"AWS error during key pair handling: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Key pair setup failed: {e}", err=True)
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
            resolved_name=final_name,
        )
        click.echo(
            f"Created {result['InstanceId']} ({instance_type}) "
            f"AMI={result['ImageId']} Name={result['Name']}"
            + (f" KeyName={key_to_use}" if key_to_use else " (no key)")
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
@click.argument("instance", required=False)  # ID or Name
@click.option("--profile", default=None, help="AWS profile to use (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def start_instance(examples, instance, profile, region, debug):
    """
    Start an EC2 instance (ID or Name, but only if tagged CreatedBy=project-cli).
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli ec2 start i-0123456789abcdef0 --region us-east-1\n"
            "  project-cli ec2 start my-api --profile myprofile                # by Name tag\n"
        )
        return

    if not instance:
        click.echo("Missing required argument INSTANCE (ID or Name).\nTry 'project-cli ec2 start -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    # Resolve to exactly one ID
    if _ID_RE.match(instance):
        target_ids = [instance]
        note = ""
    else:
        ids = _resolve_name_to_ids(session, region, instance)
        if not ids:
            click.echo(f"No instances found with Name='{instance}' (CreatedBy=project-cli).", err=True)
            raise SystemExit(2)
        if len(ids) > 1:
            click.echo(f"Name '{instance}' matched multiple instances: {' '.join(ids)}", err=True)
            click.echo("Please specify an exact instance ID.", err=True)
            raise SystemExit(2)
        target_ids = ids
        note = f" (resolved from Name='{instance}')"

    # Validate tag CreatedBy=project-cli
    try:
        resp = client.describe_instances(InstanceIds=target_ids)
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
        client.start_instances(InstanceIds=target_ids)
        click.echo(f"Start initiated for {target_ids[0]}{note} (region={effective_region})")
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
@click.argument("instance", required=False)  # ID or Name
@click.option("--profile", default=None, help="AWS profile to use (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--force", is_flag=True, help="Force stop (equivalent to hard power off)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def stop_instance(examples, instance, profile, region, force, debug):
    """
    Stop an EC2 instance (ID or Name, only if tagged CreatedBy=project-cli).
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli ec2 stop i-0123456789abcdef0 --region us-east-1\n"
            "  project-cli ec2 stop my-api --profile myprofile                 # by Name tag\n"
            "  project-cli ec2 stop --force i-0123456789abcdef0\n"
        )
        return

    if not instance:
        click.echo("Missing required argument INSTANCE (ID or Name).\nTry 'project-cli ec2 stop -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    # Resolve to exactly one ID
    if _ID_RE.match(instance):
        target_ids = [instance]
        note = ""
    else:
        ids = _resolve_name_to_ids(session, region, instance)
        if not ids:
            click.echo(f"No instances found with Name='{instance}' (CreatedBy=project-cli).", err=True)
            raise SystemExit(2)
        if len(ids) > 1:
            click.echo(f"Name '{instance}' matched multiple instances: {' '.join(ids)}", err=True)
            click.echo("Please specify an exact instance ID.", err=True)
            raise SystemExit(2)
        target_ids = ids
        note = f" (resolved from Name='{instance}')"

    # Validate tag CreatedBy=project-cli
    try:
        resp = client.describe_instances(InstanceIds=target_ids)
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
        client.stop_instances(InstanceIds=target_ids, Force=force)
        click.echo(f"Stop initiated for {target_ids[0]}{note} (region={effective_region})")
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
@click.argument("instance_ids", nargs=-1, required=False)  # IDs or Names (mixed)
@click.option("--profile", default=None, help="AWS profile to use (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def terminate_instances(examples, instance_ids, profile, region, yes, debug):
    """
    Terminate one or more EC2 instances (only if tagged CreatedBy=project-cli).

    Accepts either instance IDs (i-...) or Name tag values. Names are resolved
    to IDs scoped to CreatedBy=project-cli.
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli ec2 terminate i-0123456789abcdef0 --region us-east-1\n"
            "  project-cli ec2 terminate my-api --profile myprofile             # by Name tag\n"
            "  project-cli ec2 terminate api-a api-b i-0abc...                   # mix names & IDs\n"
            "  project-cli ec2 terminate --yes my-api                            # no prompt\n"
        )
        return

    if not instance_ids:
        click.echo("Missing required argument(s) INSTANCE_ID_OR_NAME...", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    # Resolve tokens (IDs or names) → IDs
    try:
        tokens = list(instance_ids)
        resolved_ids, not_found_names, name_map = _resolve_tokens_to_instance_ids(session, region, tokens)
    except ClientError as e:
        click.echo(f"AWS error resolving names: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    if not resolved_ids:
        if not_found_names:
            click.echo("No instances resolved from provided names: " + ", ".join(not_found_names), err=True)
        else:
            click.echo("No valid instance IDs provided.", err=True)
        raise SystemExit(2)

    # Feedback on name resolution (non-fatal)
    for nm, ids_for_nm in name_map.items():
        if len(ids_for_nm) > 1:
            click.echo(f"Note: name '{nm}' matched multiple instances: {' '.join(ids_for_nm)}")

    # Validate all resolved IDs are allowed and tagged properly
    allowed_ids: List[str] = []
    try:
        resp = client.describe_instances(InstanceIds=resolved_ids)
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
        click.echo("No terminable instances among the resolved IDs.", err=True)
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
@click.argument("instance", required=False)  # ID or Name (ignored with --all)
@click.option("--all", "show_all", is_flag=True, help="Show all instances created by you (Owner=<your user>).")
@click.option("--owner", default=None, help="Owner tag to filter with --all (default: current username).")
@click.option("--profile", default=None, help="AWS profile to use (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def describe_instance(examples, instance, show_all, owner, profile, region, debug):
    """
    Show details for EC2 instances created by this CLI.

    Modes:
      - Single instance by **ID or Name** (default)
      - `--all` -> list all instances for the given Owner (default: current user)
    """
    if examples:
        me = getpass.getuser()
        click.echo(
            "Examples:\n"
            "  project-cli ec2 describe i-0123456789abcdef0 --region us-east-1\n"
            "  project-cli ec2 describe my-api --profile myprofile              # by Name tag\n"
            f"  project-cli ec2 describe --all                                   # Owner={me}\n"
            "  project-cli ec2 describe --all --owner alice\n"
        )
        return

    # --all mode
    if show_all:
        if instance:
            click.echo("ERROR: Do not pass INSTANCE together with --all.", err=True)
            raise SystemExit(2)

        try:
            session = _session_from(profile)
        except ProfileNotFound:
            click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
            raise SystemExit(2)

        effective_region = _effective_region(session, region)
        client = session.client("ec2", region_name=effective_region)

        owner_to_use = owner or getpass.getuser()
        filters = [
            {"Name": "tag:CreatedBy", "Values": ["project-cli"]},
            {"Name": "tag:Owner", "Values": [owner_to_use]},
        ]

        try:
            paginator = client.get_paginator("describe_instances")
            pages = paginator.paginate(Filters=filters)

            found = False
            for page in pages:
                for r in page.get("Reservations", []):
                    for i in r.get("Instances", []):
                        found = True
                        iid   = i.get("InstanceId", "-")
                        state = i.get("State", {}).get("Name", "-")
                        itype = i.get("InstanceType", "-")
                        az    = i.get("Placement", {}).get("AvailabilityZone", "-")
                        name  = next((t["Value"] for t in i.get("Tags", []) if t.get("Key") == "Name"), "")
                        click.echo(f"{iid}\t{state}\t{itype}\t{az}\t{name}")
            if not found:
                click.echo(f"No instances found for Owner={owner_to_use} (CreatedBy=project-cli).")

        except (NoCredentialsError, EndpointConnectionError) as e:
            click.echo(f"ERROR: {e}", err=True)
            raise SystemExit(2)
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
        return

    # Single instance mode (ID or Name)
    if not instance:
        click.echo("Missing required argument INSTANCE (ID or Name), or use --all.\nTry 'project-cli ec2 describe -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("ec2", region_name=effective_region)

    # Resolve to exactly one ID
    if _ID_RE.match(instance):
        target_ids = [instance]
        note = ""
    else:
        ids = _resolve_name_to_ids(session, region, instance)
        if not ids:
            click.echo(f"No instances found with Name='{instance}' (CreatedBy=project-cli).", err=True)
            raise SystemExit(2)
        if len(ids) > 1:
            click.echo(f"Name '{instance}' matched multiple instances: {' '.join(ids)}", err=True)
            click.echo("Please specify an exact instance ID.", err=True)
            raise SystemExit(2)
        target_ids = ids
        note = f" (resolved from Name='{instance}')"

    try:
        resp = client.describe_instances(InstanceIds=target_ids)
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

    click.echo(f"InstanceId:   {iid}{note}")
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
