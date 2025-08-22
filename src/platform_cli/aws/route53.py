# src/platform_cli/aws/route53.py

from typing import Optional
import getpass
import traceback
from uuid import uuid4
import json

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


@click.group()
def route53():
    """Route53 commands."""
    pass


# -----------------------------
# Helpers
# -----------------------------

def _session_from(profile: Optional[str]):
    return boto3.Session(profile_name=profile) if profile else boto3.Session()

def _r53_client(session: boto3.Session):
    # Route53 is a global service (no region argument)
    return session.client("route53")

def _zone_is_cli_owned(client, zone_id: str) -> bool:
    """Return True if hosted zone has CreatedBy == DEFAULT_TAGS['CreatedBy']"""
    try:
        resp = client.list_tags_for_resource(ResourceType="hostedzone", ResourceId=zone_id)
        tags = {t["Key"]: t["Value"] for t in resp.get("ResourceTagSet", {}).get("Tags", [])}
        return tags.get("CreatedBy") == DEFAULT_TAGS["CreatedBy"]
    except ClientError:
        return False


# -----------------------------
# Commands
# -----------------------------

@route53.command("list-zones")
@click.option("--profile", default=None, help="AWS profile")
@click.option("--owner", default=None, help="Filter by Owner tag")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def list_zones(profile, owner, debug):
    """List hosted zones created by this CLI (tagged CreatedBy=project-cli)."""
    try:
        session = _session_from(profile)
        client = _r53_client(session)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    try:
        # List hosted zones, then filter by tags
        paginator = client.get_paginator("list_hosted_zones")
        found_any = False
        for page in paginator.paginate():
            for hz in page.get("HostedZones", []):
                zone_id = hz["Id"].split("/")[-1]
                # Fetch tags and filter
                try:
                    t = client.list_tags_for_resource(ResourceType="hostedzone", ResourceId=zone_id)
                    tags = {x["Key"]: x["Value"] for x in t.get("ResourceTagSet", {}).get("Tags", [])}
                except ClientError:
                    tags = {}

                if tags.get("CreatedBy") != DEFAULT_TAGS["CreatedBy"]:
                    continue
                if owner and tags.get("Owner") != owner:
                    continue

                found_any = True
                name = hz.get("Name", "").rstrip(".")
                priv = "PRIVATE" if hz.get("Config", {}).get("PrivateZone") else "PUBLIC"
                click.echo(f"{zone_id}\t{name}\t{priv}")

        if not found_any:
            click.echo("No CLI-created hosted zones found (CreatedBy=project-cli).")

    except (EndpointConnectionError, ParamValidationError, ClientError) as e:
        click.echo(f"AWS error (Route53): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)


@route53.command("create-zone", context_settings=dict(help_option_names=["-h", "--help"]))
# put --examples BEFORE positionals so it can short-circuit without args
@click.option("--examples", is_flag=True, help="Show usage examples and exit")
@click.argument("name", required=False)  # DNS name (e.g., example.com)
@click.option("--profile", default=None, help="AWS profile")
@click.option("--owner", default=getpass.getuser(), show_default=True, help="Owner tag")
@click.option("--project", default=None, help="Project tag")
@click.option("--env", default=None, help="Environment tag")
@click.option("--comment", default="created by project-cli", show_default=True, help="Zone comment")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def create_zone(examples, name, profile, owner, project, env, comment, debug):
    """Create a PUBLIC hosted zone and tag it."""
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli route53 create-zone example.com\n"
            "  project-cli route53 create-zone example.com --owner alice --project demo --env dev\n"
            "  project-cli route53 create-zone example.com --profile myprofile\n"
        )
        return

    if not name:
        click.echo("ERROR: Missing required NAME.\nTry 'project-cli route53 create-zone -h' for help.", err=True)
        raise SystemExit(2)

    if not name.endswith("."):
        name += "."

    try:
        session = _session_from(profile)
        client = _r53_client(session)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    try:
        resp = client.create_hosted_zone(
            Name=name,
            CallerReference=str(uuid4()),
            HostedZoneConfig={"Comment": comment, "PrivateZone": False},
        )
        zone_id = resp["HostedZone"]["Id"].split("/")[-1]
    except (NoCredentialsError, ClientError) as e:
        click.echo(f"AWS error (create_hosted_zone): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    # Tag the zone
    try:
        client.change_tags_for_resource(
            ResourceType="hostedzone",
            ResourceId=zone_id,
            AddTags=[{"Key": t["Key"], "Value": t["Value"]} for t in build_tag_list(owner, project, env)],
        )
    except ClientError as e:
        click.echo(f"WARNING: zone created but tagging failed: {e}", err=True)

    click.echo(f"Hosted zone created: {zone_id} {name}")


@route53.command("create-record", context_settings=dict(help_option_names=["-h", "--help"]))
# --examples FIRST so it can run without args
@click.option("--examples", is_flag=True, help="Show usage examples and exit")
@click.argument("zone_id", required=False)
@click.argument("name", required=False)
@click.argument("rtype", required=False, type=click.Choice(["A", "AAAA", "CNAME", "TXT"], case_sensitive=False))
@click.argument("value", required=False)
@click.argument("ttl", type=int, required=False, default=300)
@click.option("--profile", default=None, help="AWS profile")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def create_record(examples, zone_id, name, rtype, value, ttl, profile, debug):
    """Create/Upsert a DNS record in a hosted zone (only if zone was created by this CLI)."""
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli route53 create-record Z123ABCDEF www.example.com A 203.0.113.10 300\n"
            "  project-cli route53 create-record Z123ABCDEF api CNAME target.example.com. 60\n"
            '  project-cli route53 create-record Z123ABCDEF txt TXT "hello world" 300\n'
        )
        return

    if not (zone_id and name and rtype and value):
        click.echo("ERROR: Missing arguments.\nTry 'project-cli route53 create-record -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
        client = _r53_client(session)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    # Validate zone ownership
    if not _zone_is_cli_owned(client, zone_id):
        click.echo("Refusing to modify records: zone is not tagged CreatedBy=project-cli.", err=True)
        raise SystemExit(2)

    # Normalize
    record_name = name if name.endswith(".") else name + "."
    rtype = rtype.upper()
    if rtype == "TXT":
        # Ensure TXT values are quoted
        record_value = json.dumps(value) if not (value.startswith('"') and value.endswith('"')) else value
    else:
        record_value = value

    try:
        resp = client.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Comment": "project-cli create-record",
                "Changes": [
                    {
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": record_name,
                            "Type": rtype,
                            "TTL": ttl,
                            "ResourceRecords": [{"Value": record_value}],
                        },
                    }
                ],
            },
        )
        change_id = resp["ChangeInfo"]["Id"].split("/")[-1]
        click.echo(f"Record upserted ({rtype} {ttl}s): {record_name} value={value} change={change_id}")

    except (NoCredentialsError, ClientError) as e:
        click.echo(f"AWS error (change_resource_record_sets): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)


# -----------------------------
# New commands to complete exam requirements
# -----------------------------

@route53.command("list-records", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--examples", is_flag=True, help="Show usage examples and exit")
@click.argument("zone_id", required=False)
@click.option("--profile", default=None, help="AWS profile")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def list_records(examples, zone_id, profile, debug):
    """List DNS records for a CLI-created hosted zone."""
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli route53 list-records Z123ABCDEF\n"
        )
        return

    if not zone_id:
        click.echo("ERROR: Missing ZONE_ID.\nTry 'project-cli route53 list-records -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
        client = _r53_client(session)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    # Ensure zone is ours
    if not _zone_is_cli_owned(client, zone_id):
        click.echo("Refusing to list records: zone is not tagged CreatedBy=project-cli.", err=True)
        raise SystemExit(2)

    try:
        paginator = client.get_paginator("list_resource_record_sets")
        for page in paginator.paginate(HostedZoneId=zone_id):
            for rrset in page.get("ResourceRecordSets", []):
                name = rrset.get("Name")
                rtype = rrset.get("Type")
                ttl = rrset.get("TTL", "-")
                vals = ",".join([r["Value"] for r in rrset.get("ResourceRecords", [])]) if rrset.get("ResourceRecords") else "-"
                click.echo(f"{name}\t{rtype}\t{ttl}\t{vals}")
    except ClientError as e:
        click.echo(f"AWS error (list_resource_record_sets): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)


@route53.command("update-record", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--examples", is_flag=True, help="Show usage examples and exit")
@click.argument("zone_id", required=False)
@click.argument("name", required=False)
@click.argument("rtype", required=False, type=click.Choice(["A", "AAAA", "CNAME", "TXT"], case_sensitive=False))
@click.argument("value", required=False)
@click.argument("ttl", type=int, required=False, default=300)
@click.option("--profile", default=None, help="AWS profile")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def update_record(examples, zone_id, name, rtype, value, ttl, profile, debug):
    """Update a DNS record (only in CLI-created zones). Equivalent to explicit UPSERT."""
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli route53 update-record Z123ABCDEF www.example.com A 198.51.100.5 300\n"
        )
        return

    if not (zone_id and name and rtype and value):
        click.echo("ERROR: Missing arguments.\nTry 'project-cli route53 update-record -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
        client = _r53_client(session)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    if not _zone_is_cli_owned(client, zone_id):
        click.echo("Refusing to update: zone is not tagged CreatedBy=project-cli.", err=True)
        raise SystemExit(2)

    record_name = name if name.endswith(".") else name + "."
    rtype = rtype.upper()
    if rtype == "TXT":
        record_value = json.dumps(value) if not (value.startswith('"') and value.endswith('"')) else value
    else:
        record_value = value

    try:
        resp = client.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Comment": "project-cli update-record",
                "Changes": [{
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": record_name,
                        "Type": rtype,
                        "TTL": ttl,
                        "ResourceRecords": [{"Value": record_value}],
                    },
                }],
            },
        )
        change_id = resp["ChangeInfo"]["Id"].split("/")[-1]
        click.echo(f"Record updated ({rtype} {ttl}s): {record_name} value={value} change={change_id}")
    except (NoCredentialsError, ClientError) as e:
        click.echo(f"AWS error (change_resource_record_sets): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)


@route53.command("delete-record", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--examples", is_flag=True, help="Show usage examples and exit")
@click.argument("zone_id", required=False)
@click.argument("name", required=False)
@click.argument("rtype", required=False, type=click.Choice(["A", "AAAA", "CNAME", "TXT"], case_sensitive=False))
@click.argument("value", required=False)
@click.argument("ttl", type=int, required=False, default=300)
@click.option("--profile", default=None, help="AWS profile")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def delete_record(examples, zone_id, name, rtype, value, ttl, profile, yes, debug):
    """Delete a DNS record (only in CLI-created zones)."""
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli route53 delete-record Z123ABCDEF www.example.com A 203.0.113.10 300\n"
        )
        return

    if not (zone_id and name and rtype and value):
        click.echo("ERROR: Missing arguments.\nTry 'project-cli route53 delete-record -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
        client = _r53_client(session)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    if not _zone_is_cli_owned(client, zone_id):
        click.echo("Refusing to delete: zone is not tagged CreatedBy=project-cli.", err=True)
        raise SystemExit(2)

    record_name = name if name.endswith(".") else name + "."
    rtype = rtype.upper()
    if rtype == "TXT":
        record_value = json.dumps(value) if not (value.startswith('"') and value.endswith('"')) else value
    else:
        record_value = value

    if not yes:
        click.confirm(f"Delete record {record_name} {rtype} {ttl}s value={value} ?", abort=True)

    try:
        resp = client.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Comment": "project-cli delete-record",
                "Changes": [{
                    "Action": "DELETE",
                    "ResourceRecordSet": {
                        "Name": record_name,
                        "Type": rtype,
                        "TTL": ttl,
                        "ResourceRecords": [{"Value": record_value}],
                    },
                }],
            },
        )
        change_id = resp["ChangeInfo"]["Id"].split("/")[-1]
        click.echo(f"Record delete requested: {record_name} {rtype} change={change_id}")
    except (NoCredentialsError, ClientError) as e:
        click.echo(f"AWS error (change_resource_record_sets): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
