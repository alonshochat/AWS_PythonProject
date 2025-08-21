# src/platform_cli/aws/route53.py

import click
import boto3
import traceback
from uuid import uuid4
from typing import Optional, Dict
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


def _session_from(profile: Optional[str]):
    return boto3.Session(profile_name=profile) if profile else boto3.Session()


def _get_route53_client(session: boto3.Session):
    # Route53 is a global service; region is not required
    return session.client("route53")


@route53.command("list-zones")
@click.option("--profile", default=None, help="AWS profile (falls back to AWS_PROFILE)")
@click.option("--owner", default=None, help="Filter by Owner tag (optional)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def list_zones(profile, owner, debug):
    """
    List hosted zones created by this CLI:
    - Filters by CreatedBy=platform-cli
    - Optional filter by Owner=<owner>
    """
    try:
        session = _session_from(profile)
        client = _get_route53_client(session)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    try:
        # paginate through hosted zones
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

        # Filter by our tags (CreatedBy=platform-cli [+ optional Owner])
        shown_any = False
        for z in zones:
            zone_id_full = z["Id"]            # e.g. "/hostedzone/Z123ABC..."
            zone_id = zone_id_full.split("/")[-1]
            name = z.get("Name", "(no-name)")

            try:
                t = client.list_tags_for_resource(
                    ResourceType="hostedzone",
                    ResourceId=zone_id,
                )
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
            click.echo("No CLI-created hosted zones found (tag CreatedBy=platform-cli).")

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


@route53.command("create-zone")
@click.option("--profile", default=None, help="AWS profile (falls back to AWS_PROFILE)")
@click.option("--name", required=True, help="DNS name of the public hosted zone (e.g., example.com)")
@click.option("--owner", required=True, help="Owner tag value")
@click.option("--project", default=None, help="Project tag")
@click.option("--env", default=None, help="Environment tag")
@click.option("--comment", default="created by platform-cli", help="Optional comment on hosted zone")
@click.option("--dry-run/--no-dry-run", default=False, help="Show what would happen without calling AWS")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def create_zone(profile, name, owner, project, env, comment, dry_run, debug):
    """
    Create a **public** hosted zone and tag it with CreatedBy, Owner, Project, Environment.
    (Private hosted zones / VPCs are out of scope for this assignment.)
    """
    # Normalize the zone name to end with a dot, as AWS usually returns with trailing dot
    zone_name = name if name.endswith(".") else name + "."

    try:
        session = _session_from(profile)
        client = _get_route53_client(session)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    if dry_run:
        tags_preview = ", ".join([f"{t['Key']}={t['Value']}" for t in build_tag_list(owner, project, env)])
        click.echo("[DRY-RUN] Would create hosted zone:")
        click.echo(f"[DRY-RUN]   Name={zone_name}")
        click.echo(f"[DRY-RUN]   PublicZone=True  Comment={comment!r}")
        click.echo(f"[DRY-RUN]   Tags={tags_preview}")
        return

    try:
        # CallerReference must be unique per account
        resp = client.create_hosted_zone(
            Name=zone_name,
            CallerReference=str(uuid4()),
            HostedZoneConfig={"Comment": comment, "PrivateZone": False},
        )
        zone_id_full = resp["HostedZone"]["Id"]
        zone_id = zone_id_full.split("/")[-1]

        # Apply tags (Route53 uses a separate tagging API)
        client.change_tags_for_resource(
            ResourceType="hostedzone",
            ResourceId=zone_id,
            AddTags=[
                {"Key": t["Key"], "Value": t["Value"]}
                for t in build_tag_list(owner, project, env)
            ],
        )

        click.echo(f"Hosted zone created: {zone_id}\t{name}")

    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or use --profile.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (create_hosted_zone or tagging): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
