# src/platform_cli/aws/route53.py

from typing import Optional
import getpass
import traceback
from uuid import uuid4

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
    """Route53 (DNS) commands."""
    pass


# -----------------------------
# Helpers
# -----------------------------

def _session_from(profile: Optional[str]):
    return boto3.Session(profile_name=profile) if profile else boto3.Session()

def _r53_client(session: boto3.Session):
    return session.client("route53")  # global service


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
        zones = []
        marker = None
        while True:
            kwargs = {}
            if marker:
                kwargs["Marker"] = marker
            resp = client.list_hosted_zones(**kwargs)
            zones.extend(resp.get("HostedZones", []))
            if not resp.get("IsTruncated"):
                break
            marker = resp.get("NextMarker")

        if not zones:
            click.echo("No hosted zones in account.")
            return

        shown_any = False
        for z in zones:
            zone_id = z["Id"].split("/")[-1]
            name = z.get("Name", "(no-name)")

            try:
                t = client.list_tags_for_resource(ResourceType="hostedzone", ResourceId=zone_id)
                tagset = {kv["Key"]: kv["Value"] for kv in t.get("ResourceTagSet", {}).get("Tags", [])}
            except ClientError:
                tagset = {}

            if tagset.get("CreatedBy") != DEFAULT_TAGS["CreatedBy"]:
                continue
            if owner and tagset.get("Owner") != owner:
                continue

            shown_any = True
            click.echo(f"{zone_id}\t{name}")

        if not shown_any:
            click.echo("No CLI-created hosted zones found (tag CreatedBy=project-cli).")

    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or use --profile.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
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
            "  project-cli route53 create-zone myapp.io --owner alice --project demo --env prod\n"
        )
        return

    if not name:
        click.echo("ERROR: Missing required NAME.\nTry 'project-cli route53 create-zone -h' for help.", err=True)
        raise SystemExit(2)

    zone_name = name if name.endswith(".") else name + "."

    try:
        session = _session_from(profile)
        client = _r53_client(session)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    try:
        resp = client.create_hosted_zone(
            Name=zone_name,
            CallerReference=str(uuid4()),
            HostedZoneConfig={"Comment": comment, "PrivateZone": False},
        )
        zone_id = resp["HostedZone"]["Id"].split("/")[-1]

        client.change_tags_for_resource(
            ResourceType="hostedzone",
            ResourceId=zone_id,
            AddTags=[{"Key": t["Key"], "Value": t["Value"]} for t in build_tag_list(owner, project, env)],
        )

        click.echo(f"Hosted zone created: {zone_id}\t{name}")

    except (NoCredentialsError, ClientError) as e:
        click.echo(f"AWS error (create_hosted_zone/tagging): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)


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
            "  project-cli route53 create-record Z123ABCDEF _acme-challenge TXT abcdef123456 60\n"
        )
        return

    # Argument checks (so --examples works without args)
    missing = []
    if not zone_id: missing.append("ZONE_ID")
    if not name:    missing.append("NAME")
    if not rtype:   missing.append("RTYPE")
    if not value:   missing.append("VALUE")
    if missing:
        click.echo(
            f"ERROR: Missing required argument(s): {', '.join(missing)}.\n"
            "Try 'project-cli route53 create-record -h' for help.",
            err=True,
        )
        raise SystemExit(2)

    try:
        session = _session_from(profile)
        client = _r53_client(session)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    # Enforce: only modify zones created by this CLI
    try:
        tz = client.list_tags_for_resource(ResourceType="hostedzone", ResourceId=zone_id)
        tagset = {t["Key"]: t["Value"] for t in tz.get("ResourceTagSet", {}).get("Tags", [])}
        if tagset.get("CreatedBy") != DEFAULT_TAGS["CreatedBy"]:
            click.echo("ERROR: Hosted zone is not tagged CreatedBy=project-cli. Refusing to modify.", err=True)
            raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (list_tags_for_resource): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    record_name = name if name.endswith(".") else name + "."
    rtype = rtype.upper()
    record_value = value

    if rtype == "TXT" and not (record_value.startswith('"') and record_value.endswith('"')):
        record_value = f"\"{record_value}\""

    try:
        resp = client.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Comment": "managed by project-cli",
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
