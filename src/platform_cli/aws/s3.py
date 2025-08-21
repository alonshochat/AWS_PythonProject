import click
import boto3
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound
from platform_cli.config import DEFAULT_TAGS, build_tag_list

@click.group()
def s3():
    """S3 commands."""
    pass

@s3.command("list")
@click.option("--profile", default=None, help="AWS profile (falls back to AWS_PROFILE)")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
def list_buckets(profile, region):
    """
    List S3 buckets CREATED by this CLI (tag: CreatedBy=platform-cli).
    S3 doesn't return tags in list_buckets, so we check each bucket's tags.
    """
    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    s3_client = session.client("s3", region_name=region)
    try:
        all_buckets = s3_client.list_buckets().get("Buckets", [])
    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or set AWS_PROFILE.", err=True)
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error: {e}", err=True)
        raise SystemExit(2)

    created = []
    for b in all_buckets:
        name = b["Name"]
        # Attempt to read tags; some buckets may not have tagging
        try:
            t = s3_client.get_bucket_tagging(Bucket=name)
            tagset = {x["Key"]: x["Value"] for x in t.get("TagSet", [])}
        except ClientError:
            tagset = {}

        if tagset.get("CreatedBy") == DEFAULT_TAGS["CreatedBy"]:
            created.append(name)

    if not created:
        click.echo("No CLI-created buckets found (tag CreatedBy=platform-cli).")
    else:
        for name in created:
            click.echo(name)

@s3.command("create")
@click.option("--profile", default=None, help="AWS profile")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--name", required=True, help="Bucket name (must be globally unique)")
@click.option("--owner", required=True, help="Owner tag value")
@click.option("--project", default=None, help="Project tag")
@click.option("--env", default=None, help="Environment tag")
def create_bucket(profile, region, name, owner, project, env):
    """
    Create a PRIVATE S3 bucket with required tags.
    (Public bucket option will be added later after we verify basics.)
    """
    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    # S3 "create_bucket" is special: you must set LocationConstraint if region != us-east-1
    s3_client = session.client("s3", region_name=region)
    effective_region = region or session.region_name  # may be None; if so S3 treats as us-east-1
    create_kwargs = {"Bucket": name}
    if effective_region and effective_region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": effective_region}

    try:
        s3_client.create_bucket(**create_kwargs)
    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or set AWS_PROFILE.", err=True)
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (create_bucket): {e}", err=True)
        raise SystemExit(2)

    # Tag the bucket so our CLI can recognize it later
    try:
        s3_client.put_bucket_tagging(
            Bucket=name,
            Tagging={"TagSet": build_tag_list(owner, project, env)}
        )
    except ClientError as e:
        click.echo(f"WARNING: bucket created but tagging failed: {e}", err=True)
        # don't abort; bucket exists, but inform the user
    click.echo(f"Bucket created and tagged: {name}")
