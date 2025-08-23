# src/platform_cli/cli.py

from typing import Optional
import traceback

import click
import boto3
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    EndpointConnectionError,
)

from platform_cli.aws.ec2 import ec2
from platform_cli.aws.s3 import s3
from platform_cli.aws.route53 import route53
from platform_cli.config import DEFAULT_TAGS


@click.group()
def cli():
    """Platform CLI. Use --help to see commands."""
    pass


# -----------------------------
# Helpers for status
# -----------------------------

def _session_from(profile: Optional[str]):
    return boto3.Session(profile_name=profile) if profile else boto3.Session()

def _effective_region(session: boto3.Session, region: Optional[str]) -> str:
    # Prefer CLI option, then profile default, then sane default
    return region or session.region_name or "us-east-1"

def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n/1024:.1f} KB"
    if n < 1024**3:
        return f"{n/1024**2:.1f} MB"
    return f"{n/1024**3:.1f} GB"


# -----------------------------
# Global status command
# -----------------------------

@cli.command("status", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--profile", default=None, help="AWS profile (defaults to env/shared config)")
@click.option("--region", default=None, help="AWS region for EC2/S3 calls (default: us-east-1 if unset)")
@click.option("--owner", default=None, help="Filter by Owner tag (optional)")
@click.option("--deep/--no-deep", default=False, show_default=True,
              help="Deep scan (S3: object counts/size, R53: record counts). May take longer.")
@click.option("--debug/--no-debug", default=False, help="Show traceback on errors")
def status(profile, region, owner, deep, debug):
    """
    Show a cross-service summary of resources created by this CLI (CreatedBy=project-cli).

    - EC2: counts by state (running/pending/stopped/other) + up to 10 running examples
    - S3: number of CLI buckets; with --deep, totals objects & bytes
    - Route53: number of CLI hosted zones; with --deep, record counts per zone
    """
    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    eff_region = _effective_region(session, region)

    # ---- EC2 ----
    ec2_ok = True
    ec2_running = ec2_pending = ec2_stopped = ec2_other = 0
    ec2_running_examples = []  # list of (id, name, type)
    try:
        ec2c = session.client("ec2", region_name=eff_region)
        filters = [{"Name": "tag:CreatedBy", "Values": [DEFAULT_TAGS["CreatedBy"]]}]
        if owner:
            filters.append({"Name": "tag:Owner", "Values": [owner]})
        paginator = ec2c.get_paginator("describe_instances")
        for page in paginator.paginate(Filters=filters):
            for res in page.get("Reservations", []):
                for inst in res.get("Instances", []):
                    state = (inst.get("State", {}) or {}).get("Name", "")
                    if state == "running":
                        ec2_running += 1
                        if len(ec2_running_examples) < 10:
                            name = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), "")
                            ec2_running_examples.append((inst.get("InstanceId"), name, inst.get("InstanceType")))
                    elif state == "pending":
                        ec2_pending += 1
                    elif state == "stopped":
                        ec2_stopped += 1
                    else:
                        ec2_other += 1
    except NoCredentialsError:
        ec2_ok = False
        click.echo("EC2: ERROR: No AWS credentials.", err=True)
    except EndpointConnectionError:
        ec2_ok = False
        click.echo(f"EC2: ERROR: cannot reach endpoint in region '{eff_region}'.", err=True)
    except ClientError as e:
        ec2_ok = False
        click.echo(f"EC2: AWS error: {e}", err=True)
        if debug:
            traceback.print_exc()

    # ---- S3 ----
    s3_ok = True
    s3_bucket_count = 0
    s3_total_objects = 0
    s3_total_bytes = 0
    try:
        s3c = session.client("s3", region_name=eff_region)
        resp = s3c.list_buckets()
        for b in resp.get("Buckets", []):
            name = b["Name"]
            # tag check
            try:
                t = s3c.get_bucket_tagging(Bucket=name)
                tags = {x["Key"]: x["Value"] for x in t.get("TagSet", [])}
            except ClientError:
                continue
            if tags.get("CreatedBy") != DEFAULT_TAGS["CreatedBy"]:
                continue
            if owner and tags.get("Owner") != owner:
                continue

            s3_bucket_count += 1

            if deep:
                # count objects & bytes
                try:
                    paginator = s3c.get_paginator("list_objects_v2")
                    for page in paginator.paginate(Bucket=name):
                        for obj in page.get("Contents", []) or []:
                            s3_total_objects += 1
                            s3_total_bytes += obj.get("Size", 0)
                except ClientError:
                    pass
    except NoCredentialsError:
        s3_ok = False
        click.echo("S3: ERROR: No AWS credentials.", err=True)
    except ClientError as e:
        s3_ok = False
        click.echo(f"S3: AWS error: {e}", err=True)
        if debug:
            traceback.print_exc()

    # ---- Route53 ----
    r53_ok = True
    r53_zone_count = 0
    r53_record_count = 0
    try:
        r53 = session.client("route53")  # global
        paginator = r53.get_paginator("list_hosted_zones")
        for page in paginator.paginate():
            for hz in page.get("HostedZones", []):
                zone_id = hz["Id"].split("/")[-1]
                try:
                    tag_resp = r53.list_tags_for_resource(ResourceType="hostedzone", ResourceId=zone_id)
                    tags = {x["Key"]: x["Value"] for x in tag_resp.get("ResourceTagSet", {}).get("Tags", [])}
                except ClientError:
                    continue
                if tags.get("CreatedBy") != DEFAULT_TAGS["CreatedBy"]:
                    continue
                if owner and tags.get("Owner") != owner:
                    continue

                r53_zone_count += 1

                if deep:
                    try:
                        rp = r53.get_paginator("list_resource_record_sets")
                        for p in rp.paginate(HostedZoneId=zone_id):
                            r53_record_count += len(p.get("ResourceRecordSets", []))
                    except ClientError:
                        pass
    except NoCredentialsError:
        r53_ok = False
        click.echo("Route53: ERROR: No AWS credentials.", err=True)
    except ClientError as e:
        r53_ok = False
        click.echo(f"Route53: AWS error: {e}", err=True)
        if debug:
            traceback.print_exc()

    # ---- Summary ----
    active = ((ec2_running + ec2_pending) > 0) or (s3_bucket_count > 0) or (r53_zone_count > 0)
    click.echo("")
    click.echo("=== project-cli status ===")
    click.echo(f"Profile: {profile or '(default)'}   Region: {eff_region}   Owner filter: {owner or '(none)'}")
    click.echo(f"Active resources present: {'YES' if active else 'NO'}")
    click.echo("")

    # EC2 line
    if ec2_ok:
        total = ec2_running + ec2_pending + ec2_stopped + ec2_other
        click.echo(f"EC2: running={ec2_running} pending={ec2_pending} stopped={ec2_stopped} other={ec2_other} total={total}")
        if ec2_running_examples:
            click.echo("  Running examples (up to 10):")
            for iid, name, itype in ec2_running_examples:
                nm = f" Name={name}" if name else ""
                click.echo(f"   - {iid} ({itype}){nm}")
    else:
        click.echo("EC2: unavailable (see errors above)")

    # S3 line
    if s3_ok:
        if deep:
            click.echo(f"S3: buckets={s3_bucket_count} total_objects={s3_total_objects} total_size={_fmt_bytes(s3_total_bytes)}")
        else:
            click.echo(f"S3: buckets={s3_bucket_count}  (use --deep for object/size totals)")
    else:
        click.echo("S3: unavailable (see errors above)")

    # Route53 line
    if r53_ok:
        if deep:
            click.echo(f"Route53: zones={r53_zone_count} total_records={r53_record_count}")
        else:
            click.echo(f"Route53: zones={r53_zone_count}  (use --deep to count records)")
    else:
        click.echo("Route53: unavailable (see errors above)")

    click.echo("")


# Register subcommands
cli.add_command(ec2)
cli.add_command(s3)
cli.add_command(route53)


if __name__ == "__main__":
    cli()
