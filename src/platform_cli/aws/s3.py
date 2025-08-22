# src/platform_cli/aws/s3.py

from typing import Optional, Dict
import getpass
import traceback
import os
import mimetypes
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
def s3():
    """S3 commands."""
    pass


# -----------------------------
# Helpers
# -----------------------------

def _session_from(profile: Optional[str]):
    return boto3.Session(profile_name=profile) if profile else boto3.Session()


def _effective_region(session: boto3.Session, region: Optional[str]) -> str:
    # Prefer CLI option, then profile default, then sane default
    return region or session.region_name or "us-east-1"


def _bucket_has_cli_tag(client, bucket_name: str) -> bool:
    """Return True if bucket is tagged with CreatedBy == DEFAULT_TAGS['CreatedBy']"""
    try:
        resp = client.get_bucket_tagging(Bucket=bucket_name)
        tagset = {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}
        return tagset.get("CreatedBy") == DEFAULT_TAGS["CreatedBy"]
    except ClientError:
        return False


# -----------------------------
# Commands
# -----------------------------

@s3.command("list")
@click.option("--profile", default=None, help="AWS profile")
@click.option("--owner", default=None, help="Filter by Owner tag (optional)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def list_buckets(profile, owner, debug):
    """List S3 buckets created by this CLI (tagged CreatedBy=project-cli)."""
    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        raise SystemExit(2)

    s3c = session.client("s3", region_name=_effective_region(session, None))

    try:
        resp = s3c.list_buckets()
        found = False
        for b in resp.get("Buckets", []):
            name = b["Name"]
            try:
                tags_resp = s3c.get_bucket_tagging(Bucket=name)
                tags = {t["Key"]: t["Value"] for t in tags_resp.get("TagSet", [])}
            except ClientError:
                tags = {}

            if tags.get("CreatedBy") != DEFAULT_TAGS["CreatedBy"]:
                continue
            if owner and tags.get("Owner") != owner:
                continue

            found = True
            click.echo(name)

        if not found:
            click.echo("No buckets found (CreatedBy=project-cli).")

    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or use --profile.", err=True)
        raise SystemExit(2)
    except EndpointConnectionError:
        click.echo("ERROR: could not reach S3 endpoint. Check your network/region.", err=True)
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (list_buckets): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)


@s3.command("create", context_settings=dict(help_option_names=["-h", "--help"]))
# put --examples BEFORE arguments so it can short-circuit without NAME
@click.option("--examples", is_flag=True, help="Show usage examples and exit")
@click.argument("name", required=False)                       # bucket name positional
@click.argument("visibility", required=False)                 # 'private' (default) or 'public'
@click.option("--profile", default=None, help="AWS profile")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--owner", default=getpass.getuser(), show_default=True, help="Owner tag value")
@click.option("--project", default=None, help="Project tag")
@click.option("--env", default=None, help="Environment tag")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def create_bucket(examples, name, visibility, profile, region, owner, project, env, debug):
    """
    Create an S3 bucket with safe defaults and tagging.

    You must choose visibility: PRIVATE (default) or PUBLIC (prompted).
    PRIVATE = fully blocked public access + default SSE-S3 encryption.
    PUBLIC  = disables public access block and attaches a public-read bucket policy.

    Arguments:
      NAME        bucket name
      VISIBILITY  private|public  (optional, defaults to private)
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli s3 create my-unique-bucket-123 private --region us-east-1\n"
            "  project-cli s3 create public-static-site-bkt public --region us-east-1\n"
            "  project-cli s3 create course-demo-bucket        # defaults to private\n"
        )
        return

    if not name:
        click.echo("ERROR: Missing required NAME argument.\nTry 'project-cli s3 create -h' for help.", err=True)
        raise SystemExit(2)

    vis = (visibility or "private").lower()
    if vis not in ("private", "public"):
        click.echo("ERROR: visibility must be 'private' or 'public' (or omit for private).", err=True)
        raise SystemExit(2)
    if vis == "public":
        click.confirm(
            "This will make the bucket PUBLIC (readable by everyone). Continue?",
            abort=True
        )

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("s3", region_name=effective_region)

    # Create bucket (special-case for us-east-1 with no LocationConstraint)
    create_kwargs: Dict[str, object] = {"Bucket": name}
    if effective_region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": effective_region}

    try:
        client.create_bucket(**create_kwargs)
    except ClientError as e:
        click.echo(f"AWS error (create_bucket): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    # Default encryption (SSE-S3) for both modes
    try:
        client.put_bucket_encryption(
            Bucket=name,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        )
    except ClientError as e:
        click.echo(f"WARNING: bucket created but default encryption failed: {e}", err=True)

    # Tag bucket
    try:
        client.put_bucket_tagging(
            Bucket=name,
            Tagging={"TagSet": build_tag_list(owner, project, env)},
        )
    except ClientError as e:
        click.echo(f"WARNING: bucket created but tagging failed: {e}", err=True)

    # Visibility config
    if vis == "private":
        # Block public access
        try:
            client.put_public_access_block(
                Bucket=name,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )
        except ClientError as e:
            click.echo(f"WARNING: failed to apply public access block: {e}", err=True)

        click.echo(f"Bucket created (PRIVATE) and tagged: {name} (region={effective_region})")

    else:  # public
        # Disable public access block and attach a read-only policy
        try:
            client.put_public_access_block(
                Bucket=name,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": False,
                    "IgnorePublicAcls": False,
                    "BlockPublicPolicy": False,
                    "RestrictPublicBuckets": False,
                },
            )
        except ClientError as e:
            click.echo(f"WARNING: failed to disable public access block: {e}", err=True)

        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicReadGetObject",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{name}/*"],
                }
            ],
        }
        try:
            client.put_bucket_policy(Bucket=name, Policy=json.dumps(policy))
        except ClientError as e:
            click.echo(f"WARNING: failed to attach public-read policy: {e}", err=True)

        click.echo(f"Bucket created (PUBLIC) and tagged: {name} (region={effective_region})")


@s3.command("upload", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--examples", is_flag=True, help="Show usage examples and exit")
@click.argument("bucket", required=False)
@click.argument("filepath", required=False, type=click.Path(exists=True, dir_okay=False, readable=True))
@click.argument("key", required=False)
@click.option("--profile", default=None, help="AWS profile")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def upload_object(examples, bucket, filepath, key, profile, region, debug):
    """
    Upload a local FILE to S3 BUCKET at KEY (defaults to the filename if omitted).

    Only allowed for buckets created by this CLI (tag CreatedBy=project-cli).
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli s3 upload my-bucket ./app.zip uploads/app.zip --region us-east-1\n"
            "  project-cli s3 upload my-bucket ./index.html                      # key=index.html\n"
        )
        return

    # Argument checks (so --examples can be called without args)
    if not bucket or not filepath:
        click.echo("ERROR: Missing required arguments BUCKET and FILE.\nTry 'project-cli s3 upload -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    client = session.client("s3", region_name=effective_region)

    if not _bucket_has_cli_tag(client, bucket):
        click.echo(
            f"ERROR: Bucket '{bucket}' is not tagged CreatedBy={DEFAULT_TAGS['CreatedBy']}. "
            "Upload is refused.",
            err=True,
        )
        raise SystemExit(2)

    object_key = key or os.path.basename(filepath)

    extra_args: Dict[str, str] = {}
    ctype, _ = mimetypes.guess_type(object_key)
    if ctype:
        extra_args["ContentType"] = ctype

    try:
        if extra_args:
            client.upload_file(filepath, bucket, object_key, ExtraArgs=extra_args)
        else:
            client.upload_file(filepath, bucket, object_key)
        click.echo(f"Uploaded {filepath} -> s3://{bucket}/{object_key} (region={effective_region})")
    except NoCredentialsError:
        click.echo("ERROR: No AWS credentials. Run `aws configure` or use --profile.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except ClientError as e:
        click.echo(f"AWS error (upload_file): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)


@s3.command("delete", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--examples", is_flag=True, help="Show usage examples and exit")
@click.argument("bucket", required=False)
@click.option("--profile", default=None, help="AWS profile")
@click.option("--region", default=None, help="AWS region (e.g., us-east-1)")
@click.option("--force", is_flag=True, help="Permanently delete all objects (and versions) before removing the bucket")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--debug/--no-debug", default=False, help="Show full traceback on errors")
def delete_bucket(examples, bucket, profile, region, force, yes, debug):
    """
    Delete an S3 bucket created by this CLI (tagged CreatedBy=project-cli).

    By default the bucket must be EMPTY. Use --force to delete ALL objects and
    object versions first. This action is irreversible.
    """
    if examples:
        click.echo(
            "Examples:\n"
            "  project-cli s3 delete my-bucket --region us-east-1\n"
            "  project-cli s3 delete my-bucket --force --yes\n"
            "  project-cli s3 delete my-bucket --profile myprofile\n"
        )
        return

    if not bucket:
        click.echo("ERROR: Missing required BUCKET.\nTry 'project-cli s3 delete -h' for help.", err=True)
        raise SystemExit(2)

    try:
        session = _session_from(profile)
    except ProfileNotFound:
        click.echo("ERROR: profile not found. Use --profile or set AWS_PROFILE.", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)

    effective_region = _effective_region(session, region)
    s3c = session.client("s3", region_name=effective_region)
    s3r = session.resource("s3", region_name=effective_region)

    # Ensure it's a CLI bucket
    if not _bucket_has_cli_tag(s3c, bucket):
        click.echo(
            f"Refusing to delete '{bucket}': not tagged CreatedBy={DEFAULT_TAGS['CreatedBy']}.",
            err=True
        )
        raise SystemExit(2)

    # Confirm
    if not yes:
        msg = "This will DELETE the bucket"
        msg += " and ALL contents (versions!)" if force else " (must be empty)"
        msg += f": {bucket}. Continue?"
        click.confirm(msg, abort=True)

    # If --force, purge objects/versions
    if force:
        try:
            b = s3r.Bucket(bucket)
            # Attempt to delete versions; if versioning off, fallback to objects
            try:
                b.object_versions.delete()
            except Exception:
                b.objects.all().delete()
        except ClientError as e:
            click.echo(f"AWS error while purging objects: {e}", err=True)
            if debug:
                traceback.print_exc()
            raise SystemExit(2)

    # Attempt bucket delete
    try:
        s3c.delete_bucket(Bucket=bucket)
        click.echo(f"Bucket deleted: {bucket} (region={effective_region})")
    except ClientError as e:
        click.echo(f"AWS error (delete_bucket): {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        if debug:
            traceback.print_exc()
        raise SystemExit(2)
