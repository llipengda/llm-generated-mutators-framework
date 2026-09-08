import click
from core.config import build_config_from_args, load_env

@click.command()
@click.option("--protocol", required=True)
@click.option("--seed-dir", required=True)
@click.option("--rfc-path", "rfc_paths", required=True, multiple=True, help="Path(s) to RFC document(s). Can be specified multiple times for multiple RFCs.")
@click.option("--fixer", is_flag=True, default=False, help="Enable fixer generation and validation.")
@click.option(
    "--modern-sdk",
    is_flag=True,
    default=False,
    help="Use the .NET 8 pdli/llm-peach:modern-sdk backend instead of legacy Mono.",
)
def main(
    protocol: str,
    seed_dir: str,
    rfc_paths: list[str],
    fixer: bool,
    modern_sdk: bool,
):
    load_env()
    build_config_from_args(
        protocol,
        seed_dir,
        list(rfc_paths),
        fixer=fixer,
        modern_sdk=modern_sdk,
    )
    from pipeline.peach import PeachPipeline

    pipeline = PeachPipeline()
    pipeline()

if __name__ == "__main__":
    main()
