import click
from pipeline import run_pipeline
from config import build_config_from_args

@click.command()
@click.option("--protocol", required=True)
@click.option("--seed-dir", required=True)
@click.option("--rfc-path", required=True)
def main(protocol: str, seed_dir: str, rfc_path: str):
    build_config_from_args(protocol, seed_dir, rfc_path)
    run_pipeline()

if __name__ == "__main__":
    main()