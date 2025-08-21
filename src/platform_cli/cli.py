import click
from platform_cli.aws.ec2 import ec2
from platform_cli.aws.s3 import s3
from platform_cli.aws.route53 import route53   # NEW

@click.group()
def cli():
    """Platform CLI. Use --help to see commands."""
    pass

cli.add_command(ec2)
cli.add_command(s3)
cli.add_command(route53)  # NEW

if __name__ == "__main__":
    cli()
